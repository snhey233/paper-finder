"""ScienceDirect — 搜索 + OA筛选 + PDF 下载（学校 VPN 通道）

通过 Playwright CDP + WebVPN 操作 ScienceDirect。
支持关键词搜索、年份筛选、Open Access 过滤、浏览器内 fetch / printToPDF 下载 PDF。

用法:
  python main.py sd "keyword | startYear endYear | count | outputDir"
  python main.py sciencedirect "keyword | startYear endYear | count | outputDir"

工作流程:
  1. 用户先通过 WebVPN 登录 ScienceDirect
  2. 脚本在搜索结果页通过 URL 参数（accessTypes=openaccess）筛选 OA
  3. 对每篇 OA 文章：
     a. 优先查找 "View PDF" 链接，fetch→base64 保存
     b. 无 PDF 链接时回退到 Page.printToPDF
  4. 失败记录自动导出 Excel

管道格式:
  第1段: 关键词
  第2段: 起止年份 (空格分隔, 如 "2025 2026")
  第3段: 数量 (默认 5)
  第4段: 输出目录 (默认 ./SD_Results)
"""

import asyncio
import os
import json
import base64
import urllib.parse

from utils import sp, log, safe_filename, ensure_output_dir, connect_playwright_async_with_timeout, FailedRecord, get_vpn_domain, check_chrome_cdp, parse_standard_args, validate_pdf


DEFAULT_COUNT = 5
DEFAULT_OUTPUT = "./SD_Results"
SD_DOMAIN = get_vpn_domain("www-sciencedirect-com-443.webvpn.upc.edu.cn")


def parse_args(args_text: str) -> dict:
    """Parse ScienceDirect parameters using standardized pipe format.

    Format: keyword | startYear endYear | count | outputDir
    """
    params = {
        "keyword": "",
        "start_year": None,
        "end_year": None,
        "count": DEFAULT_COUNT,
        "output_dir": DEFAULT_OUTPUT,
    }
    return parse_standard_args(args_text, params)


def build_search_url(params):
    """构建 ScienceDirect 搜索 URL（含年份和 OA 筛选）"""
    keyword = urllib.parse.quote(params["keyword"])
    url = f"https://{SD_DOMAIN}/search?qs={keyword}&show=25&sortBy=relevance&accessTypes=openaccess"
    if params["start_year"] and params["end_year"]:
        url += f"&date={params['start_year']}-{params['end_year']}"
    return url


async def extract_articles(page):
    """从搜索结果页提取文章列表"""
    articles = await page.evaluate("""
    () => {
        var wrapper = document.querySelector('ol.search-result-wrapper');
        if (!wrapper) return '[]';
        var items = wrapper.querySelectorAll('li');
        var results = [];
        items.forEach(function(item) {
            var link = item.querySelector('a.anchor.result-list-title-link');
            if (!link) return;
            var title = (link.textContent || '').trim();
            if (!title) return;
            var href = link.href || '';

            // Year
            var txt = item.textContent || '';
            var ym = txt.match(/20\\d{2}/);
            var year = ym ? ym[0] : '';

            // Journal
            var source_el = item.querySelector('[class*="source"]');
            var journal = source_el ? (source_el.textContent || '').trim() : '';

            // DOI — extract from link meta if available, otherwise build from PII
            var doi = '';
            var doiEl = item.querySelector('[class*="doi"], a[href*="doi.org"]');
            if (doiEl) {
                var hrefMatch = doiEl.href ? doiEl.href.match(/10\\.\\d{4,}[^&?#\\s]+/) : null;
                if (hrefMatch) doi = hrefMatch[0];
            }
            if (!doi) {
                var doi_m = href.match(/\\/article\\/([^?]+)/);
                var doi_path = doi_m ? doi_m[1] : '';
                var pii = doi_path.replace('/pii/', '').replace('/abs/', '');
                doi = 'https://doi.org/' + pii;
            }

            results.push({title: title.substring(0, 150), link: href,
                          year: year, journal: journal, doi: doi, pii: pii});
        });
        return JSON.stringify(results);
    }
    """)
    return json.loads(articles)


async def download_article(page, art, index, output_dir, failed):
    """下载单篇 OA 文章 PDF

    策略:
      1. 查找 "View PDF" 链接 → fetch → base64
      2. 回退: Page.printToPDF
    """
    log("SD", f"  [{index}] {art['title'][:50]}...")

    try:
        await page.goto(art["link"], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        # 策略 1: 查找 View PDF 链接并 fetch
        pdf_b64 = await page.evaluate("""
        async () => {
            var pdfLink = null;
            var els = document.querySelectorAll('a');
            for (var el of els) {
                var t = (el.textContent || '').trim().toLowerCase();
                var h = el.href || '';
                if (t.includes('view pdf') && h.includes('pdfft')) { pdfLink = h; break; }
                if ((t.includes('download pdf') || t.includes('save pdf')) && h) { pdfLink = h; break; }
            }

            if (pdfLink) {
                try {
                    var fetchUrl = pdfLink.startsWith('http') ? pdfLink : window.location.origin + pdfLink;
                    var resp = await fetch(fetchUrl, {credentials: 'include'});
                    if (!resp.ok) return 'HTTP' + resp.status;
                    var blob = await resp.blob();
                    if (blob.size < 2000) return 'SMALL:' + blob.size;
                    var buf = await blob.arrayBuffer();
                    var bytes = new Uint8Array(buf);
                    var bin = '';
                    for (var j = 0; j < bytes.length; j++) bin += String.fromCharCode(bytes[j]);
                    if (bin.substring(0, 5) !== '%PDF-') return 'NOT_PDF';
                    return 'data:application/pdf;base64,' + btoa(bin);
                } catch(e) { return 'ERR:' + e.message; }
            }
            return 'NO_PDF_LINK';
        }
        """)

        if pdf_b64 and pdf_b64.startswith("data:application/pdf;base64,"):
            raw = base64.b64decode(pdf_b64.split(",")[1])
            safe = safe_filename(art["title"], 80).replace(" ", "_")
            fname = f"{index:02d}_{safe}.pdf"
            fpath = os.path.join(output_dir, fname)
            with open(fpath, "wb") as f:
                f.write(raw)
            ok, msg = validate_pdf(fpath)
            if ok:
                log("SD", f"  [OK] {fname} ({len(raw)//1024} KB)")
                return True
            try:
                os.remove(fpath)
            except OSError:
                pass
            log("SD", f"  Direct PDF invalid: {msg}, trying printToPDF...")

        # 策略 2: printToPDF（ScienceDirect OA 文章在 VPN 下部分无 PDF 直链）
        log("SD", "  No direct PDF, using printToPDF...")
        cdp = await page.context.new_cdp_session(page)
        result = await cdp.send("Page.printToPDF", {
            "printBackground": True,
            "preferCSSPageSize": True,
        })
        pdf_data = result.get("data", "")
        if pdf_data and len(pdf_data) > 50000:
            raw = base64.b64decode(pdf_data)
            safe = safe_filename(art["title"], 80).replace(" ", "_")
            fname = f"{index:02d}_{safe}.pdf"
            fpath = os.path.join(output_dir, fname)
            with open(fpath, "wb") as f:
                f.write(raw)
            ok, msg = validate_pdf(fpath)
            if ok:
                log("SD", f"  [OK] {fname} via printToPDF ({len(raw)//1024} KB)")
                return True
            try:
                os.remove(fpath)
            except OSError:
                pass
            failed.add(title=art["title"], doi=art.get("doi", ""), link=art["link"],
                       source="ScienceDirect", reason=f"printToPDF PDF invalid: {msg}")
            return False
        else:
            failed.add(title=art["title"], doi=art.get("doi", ""), link=art["link"],
                       source="ScienceDirect", reason=f"printToPDF returned insufficient data ({len(pdf_data) if pdf_data else 0} bytes)")
            return False

    except Exception as e:
        failed.add(title=art["title"], doi=art.get("doi", ""), link=art["link"],
                   source="ScienceDirect", reason=str(e)[:80])
        log("SD", f"  [ERROR] {str(e)[:60]}")
        return False


async def main_async(args_text: str):
    """异步主入口"""
    params = parse_args(args_text)
    keyword = params["keyword"]
    count = params["count"]
    output_dir = ensure_output_dir(params["output_dir"])

    if not keyword:
        log("SD", "Keyword required.")
        log("SD", 'Usage: python main.py sd "keyword | startYear endYear | count | outputDir"')
        return

    log("SD", f"Query: {keyword} | Year: {params['start_year']}-{params['end_year']} | Count: {count} | Output: {output_dir}")
    log("SD", "Important: Make sure you are logged into ScienceDirect via WebVPN in Chrome.")

    ok, info = check_chrome_cdp()
    if not ok:
        log("SD", f"ERROR: Chrome CDP not available: {info}")
        log("SD", "Run 'python scripts/chrome.py' first, or start Chrome with --remote-debugging-port=9222")
        return

    # Connect to Chrome
    log("SD", "Connecting to Chrome CDP...")
    p, browser = None, None
    try:
        p, browser, page = await connect_playwright_async_with_timeout(timeout=30000)
    except Exception as e:
        log("SD", f"Cannot connect to Chrome: {e}")
        log("SD", "Ensure Chrome is running with --remote-debugging-port=9222")
        return
    log("SD", "Connected.")

    try:
        # Build and navigate to search URL
        search_url = build_search_url(params)
        log("SD", f"Searching OA articles: {search_url[:120]}...")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        # Extract articles
        all_articles = await extract_articles(page)
        log("SD", f"OA articles found: {len(all_articles)}")

        if not all_articles:
            log("SD", "No OA articles found. Try different keywords.")
            return

        for i, a in enumerate(all_articles[:10], 1):
            sp(f"  {i:2d}. [{a['year']}] {a['title'][:60]} | {a['journal'][:30]}")

        # Download
        target = min(count, len(all_articles))
        log("SD", f"\nDownloading {target} papers...")
        failed = FailedRecord()
        downloaded = 0

        for idx in range(target):
            ok = await download_article(page, all_articles[idx], idx + 1, output_dir, failed)
            if ok:
                downloaded += 1

        # Summary
        log("SD", f"Done! {downloaded}/{target} downloaded to {os.path.abspath(output_dir)}")
        if failed.count > 0:
            xlsx = failed.save_xlsx(output_dir)
            log("SD", f"Failed records: {xlsx} ({failed.count} papers)")

    finally:
        if browser:
            await browser.close()
        if p:
            await p.stop()


def main(args_text: str):
    """同步入口"""
    asyncio.run(main_async(args_text))


if __name__ == "__main__":
    import sys
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
