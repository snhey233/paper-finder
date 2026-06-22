"""IEEE Xplore VPN — 通过浏览器 VPN/机构代理批量下载 PDF

与 ieee.py 的区别：
  - ieee.py 使用 page.context.request.get() → 不走浏览器 VPN
  - ieee_vpn.py 使用 page.evaluate("fetch()") → 走浏览器 VPN 代理

三种 PDF 获取策略：
  1. 嵌入 URL — 从页面的 <embed>/<iframe> 提取 PDF 直链
  2. Stamp 端点 — 通过 IEEE stamp 服务获取 PDF
  3. onclick 链接 — 模拟 PDF 按钮点击获取真实下载链接

用法:
  python main.py ieee-vpn "keyword | startYear endYear | sortBy | count | outputDir"
"""

import asyncio
import base64
import json
import os
import re
import urllib.parse

from utils import sp, log, safe_filename, ensure_output_dir, save_json, connect_playwright_async_with_timeout, FailedRecord, parse_standard_args, check_chrome_cdp, validate_pdf


DEFAULT_QUERY = "(refined petroleum OR product oil OR refined oil) AND (scheduling OR distribution OR dispatch) AND optimization"
DEFAULT_START_YEAR = 2020
DEFAULT_END_YEAR = 2026
DEFAULT_SORT = "citations"  # citations | date | relevance
DEFAULT_COUNT = 10
DEFAULT_OUTPUT = "./IEEE_VPN_Results"


def parse_args(args_text: str) -> dict:
    """Parse IEEE VPN parameters using standardized pipe format.

    Format: keyword | startYear endYear | sortBy | count | outputDir
    sortBy: citations (default) / date / relevance
    """
    params = {
        "keyword": DEFAULT_QUERY,
        "start_year": DEFAULT_START_YEAR,
        "end_year": DEFAULT_END_YEAR,
        "sort_by": DEFAULT_SORT,
        "count": DEFAULT_COUNT,
        "output_dir": DEFAULT_OUTPUT,
    }
    parsed = parse_standard_args(args_text, params, {"sort": "sort_by", "sort_by": "sort_by"})
    sort_map = {"citations": "citations", "date": "date", "relevance": "relevance",
                "被引": "citations", "日期": "date", "相关度": "relevance"}
    parsed["sort_by"] = sort_map.get(str(parsed.get("sort_by", "")).lower(), DEFAULT_SORT)
    return parsed


def build_search_url(query, start_year, end_year, sort):
    """构建 IEEE Xplore 搜索 URL"""
    return (
        "https://ieeexplore.ieee.org/search/searchresult.jsp"
        f"?queryText={urllib.parse.quote(query)}"
        "&highlight=true&returnFacets=ALL&returnType=SEARCH&matchPubs=true"
        f"&ranges={start_year}_{end_year}_PYear"
        f"&sortType={sort}"
    )


async def search_ieee(page, query, start_year, end_year, sort):
    """搜索 IEEE 并提取论文列表"""
    search_url = build_search_url(query, start_year, end_year, sort)
    log("IEEE-VPN", f"Searching: {query}")
    log("IEEE-VPN", f"URL: {search_url}")

    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)

    papers = await page.evaluate("""
        () => {
            const papers = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="/document/"]').forEach(a => {
                const idMatch = a.href.match(/document\\/(\\d+)/);
                if (!idMatch || seen.has(idMatch[1])) return;
                seen.add(idMatch[1]);
                const heading = a.closest('h2, h3, h4, .title, .result-item-title');
                const title = heading ? heading.textContent.trim() : a.textContent.trim();
                const parentText = (a.closest('li, div, article, section') || a.parentElement)?.textContent || '';
                const yearMatch = parentText.match(/\\b(20[12]\\d)\\b/);
                papers.push({
                    title: title.replace(/\\s+/g, ' ').trim(),
                    link: a.href,
                    year: yearMatch ? yearMatch[1] : ''
                });
            });
            return papers;
        }
    """)

    # 年份过滤
    valid = [p for p in papers if p.get("year", "").isdigit()
             and start_year <= int(p["year"]) <= end_year]
    if len(valid) >= 3:
        papers = valid

    return papers


async def fetch_pdf_via_browser(page, pdf_url):
    """核心方法：通过浏览器内 fetch 获取 PDF（走浏览器代理/VPN）

    在浏览器内执行 fetch（走 VPN 代理），返回 base64 编码的 PDF，
    Python 端解码后保存为 .pdf 文件。
    """
    b64_data = await page.evaluate(f"""async () => {{
        try {{
            const resp = await fetch('{pdf_url}', {{credentials: 'include'}});
            const blob = await resp.blob();

            if (blob.size < 1000) {{
                return 'TOO_SMALL:' + blob.size;
            }}

            if (blob.type && !blob.type.includes('pdf') && !blob.type.includes('octet')) {{
                if (blob.size < 100000) {{
                    const text = await blob.text();
                    if (text.includes('access denied') || text.includes('sign in')) {{
                        return 'AUTH_REQUIRED';
                    }}
                }}
            }}

            return await new Promise((resolve, reject) => {{
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject('FileReader error');
                reader.readAsDataURL(blob);
            }});
        }} catch(e) {{
            return 'FETCH_ERROR:' + e.message;
        }}
    }}""")

    if b64_data and b64_data.startswith('data:'):
        _, encoded = b64_data.split(',', 1)
        pdf_bytes = base64.b64decode(encoded)
        if b"%PDF" in pdf_bytes[:500] or len(pdf_bytes) > 50000:
            return pdf_bytes

    return None


async def try_download_paper(page, doc_id, title, output_dir, idx, failed):
    """尝试三种方式下载单篇论文"""
    safe_title = safe_filename(title, 80) or f"paper_{idx}"
    log("IEEE-VPN", f"  [{idx}] {safe_title[:55]}...")

    link = f"https://ieeexplore.ieee.org/document/{doc_id}"

    # 打开论文详情页（走 VPN）
    try:
        await page.goto(link, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log("IEEE-VPN", f"      Page load failed: {str(e)[:60]}")
        return False

    # 检查访问权限
    page_text = await page.evaluate("document.body?.innerText?.toLowerCase() || ''")
    if "access denied" in page_text or ("your institution" not in page_text and "sign in" in page_text and "access" in page_text):
        msg = "[需要机构授权] 请在浏览器中通过 VPN 或机构登录后重试"
        log("IEEE-VPN", f"      {msg}")
        failed.add(title=title, link=link, source="IEEE-VPN", reason=msg)
        return False

    # --- 方法 1: 嵌入 PDF URL（iframe/embed/object）---
    embed_url = await page.evaluate("""() => {
        const e = document.querySelector('embed[type="application/pdf"]');
        if (e && e.src) return e.src;
        const i = document.querySelector('iframe[src*="ielx"], iframe[src*=".pdf"]');
        if (i && i.src) return i.src;
        const o = document.querySelector('object[type="application/pdf"]');
        if (o && o.data) return o.data;
        return '';
    }""")

    if embed_url:
        pdf_url = embed_url if embed_url.startswith('http') else f"https://ieeexplore.ieee.org{embed_url}"
        log("IEEE-VPN", f"      Found embed PDF: {pdf_url[:70]}...")
        pdf_data = await fetch_pdf_via_browser(page, pdf_url)
        if pdf_data:
            filepath = os.path.join(output_dir, f"{idx:02d}_{safe_title}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf_data)
            ok, msg = validate_pdf(filepath)
            if ok:
                log("IEEE-VPN", f"      Downloaded ({len(pdf_data)//1024} KB, embed)")
                return True
            try:
                os.remove(filepath)
            except OSError:
                pass
            log("IEEE-VPN", f"      Embed PDF invalid: {msg}")
        log("IEEE-VPN", "      Embed fetch failed")

    # --- 方法 2: Stamp URL（IEEE 标准 PDF 服务端点）---
    stamp_url = f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={doc_id}"
    pdf_data = await fetch_pdf_via_browser(page, stamp_url)
    if pdf_data:
        filepath = os.path.join(output_dir, f"{idx:02d}_{safe_title}.pdf")
        with open(filepath, "wb") as f:
            f.write(pdf_data)
        ok, msg = validate_pdf(filepath)
        if ok:
            log("IEEE-VPN", f"      Downloaded ({len(pdf_data)//1024} KB, stamp)")
            return True
        try:
            os.remove(filepath)
        except OSError:
            pass
        log("IEEE-VPN", f"      Stamp PDF invalid: {msg}")

    # --- 方法 3: 找 PDF 按钮 onclick 中的真实 URL ---
    onclick_url = await page.evaluate("""() => {
        const all = document.querySelectorAll('a, button');
        for (const el of all) {
            const t = (el.textContent || '').trim().toLowerCase();
            if (t === 'pdf') {
                const oc = el.getAttribute('onclick') || el.parentElement?.getAttribute('onclick') || '';
                const m = oc.match(/['"](https?:[^'"]+)['"]/);
                if (m) return m[1];
            }
        }
        return '';
    }""")
    if onclick_url:
        pdf_data = await fetch_pdf_via_browser(page, onclick_url)
        if pdf_data:
            filepath = os.path.join(output_dir, f"{idx:02d}_{safe_title}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf_data)
            ok, msg = validate_pdf(filepath)
            if ok:
                log("IEEE-VPN", f"      Downloaded ({len(pdf_data)//1024} KB, onclick)")
                return True
            try:
                os.remove(filepath)
            except OSError:
                pass
            log("IEEE-VPN", f"      onclick PDF invalid: {msg}")

    log("IEEE-VPN", "      Not available (no full text)")
    failed.add(title=title, link=link, source="IEEE-VPN", reason="No full text available via any method")
    return False


async def main_async(args_text: str):
    """异步主入口"""
    params = parse_args(args_text)
    output_dir = ensure_output_dir(params["output_dir"])

    log("IEEE-VPN", "=" * 50)
    log("IEEE-VPN", f"Query: {params['keyword']}")
    log("IEEE-VPN", f"Year: {params['start_year']}-{params['end_year']}")
    log("IEEE-VPN", f"Sort: {params['sort_by']} | Count: {params['count']} | Output: {output_dir}")

    log("IEEE-VPN", "Connecting to Chrome...")
    ok, info = check_chrome_cdp()
    if not ok:
        log("IEEE-VPN", f"ERROR: Chrome CDP not available: {info}")
        log("IEEE-VPN", "Run 'python scripts/chrome.py' first, or start Chrome with --remote-debugging-port=9222")
        return
    p, browser = None, None
    try:
        p, browser, page = await connect_playwright_async_with_timeout(timeout=30000)
    except Exception as e:
        log("IEEE-VPN", f"ERROR: Cannot connect to Chrome: {e}")
        log("IEEE-VPN", "Ensure Chrome is running with --remote-debugging-port=9222")
        log("IEEE-VPN", 'Example: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222')
        return
    log("IEEE-VPN", "Connected.")

    try:
        # 搜索
        log("IEEE-VPN", "Searching IEEE Xplore...")
        papers = await search_ieee(page, params["keyword"], params["start_year"], params["end_year"], params["sort_by"])
        log("IEEE-VPN", f"Found {len(papers)} papers in range {params['start_year']}-{params['end_year']}.")
        for i, paper in enumerate(papers[: params["count"]]):
            sp(f"  {i+1:2d}. [{paper.get('year','?')}] {paper['title'][:70]}")

        if not papers:
            log("IEEE-VPN", "No papers found. Try broader keywords.")
            return

        # 保存论文列表
        save_json(papers[: params["count"]], os.path.join(output_dir, "papers_list.json"))

        # 下载
        log("IEEE-VPN", f"Downloading {min(params['count'], len(papers))} papers...")
        log("IEEE-VPN", "Hint: Ensure browser has VPN/IEEE access, or download will fail.")

        failed = FailedRecord()

        # 检查已下载，避免重复
        existing = set()
        for fname in os.listdir(output_dir):
            if fname.endswith(".pdf"):
                existing.add(fname[3:].replace(".pdf", "").strip()[:25])

        success = 0
        for idx, paper in enumerate(papers[: params["count"]], 1):
            doc_match = re.search(r"/document/(\d+)", paper["link"])
            doc_id = doc_match.group(1) if doc_match else None
            if not doc_id:
                log("IEEE-VPN", f"  [{idx}] No document ID, skip")
                continue

            title = paper.get("title", "")
            safe = safe_filename(title, 25)

            # 跳过已下载
            skip = False
            for et in existing:
                if safe[:20] in et or et[:20] in safe:
                    log("IEEE-VPN", f"  [{idx}] Already downloaded, skip")
                    success += 1
                    skip = True
                    break
            if skip:
                continue

            if await try_download_paper(page, doc_id, title, output_dir, idx, failed):
                success += 1

            await page.wait_for_timeout(2000)

        total = len([f for f in os.listdir(output_dir) if f.endswith('.pdf')])
        log("IEEE-VPN", f"Done! This session: {success} | Total in dir: {total} PDFs")
        log("IEEE-VPN", f"Output: {output_dir}")
        if failed.count > 0:
            xlsx = failed.save_xlsx(output_dir)
            log("IEEE-VPN", f"Failed records: {xlsx} ({failed.count} papers)")

    finally:
        if browser:
            await browser.close()
        if p:
            await p.stop()


def main(args_text: str):
    """同步入口"""
    asyncio.run(main_async(args_text))


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
