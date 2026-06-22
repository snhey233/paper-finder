"""IEEE Xplore — 搜索 + PDF 下载（支持 VPN + 打印到PDF）

通过 Chrome CDP + Playwright 操作 IEEE Xplore。
支持两种模式：
  1. 直连（OA论文）：直接访问 ieeexplore.ieee.org
  2. VPN（非OA论文）：通过学校 WebVPN 代理访问 + 打印到PDF

用法:
  python main.py ieee "keyword | startYear endYear | sortBy | count | outputDir"
  python main.py ieee "keyword | startYear endYear | sortBy | count | outputDir | vpnDomain"
"""

import asyncio
import json
import os
import re
import urllib.parse

from utils import sp, log, safe_filename, save_json, ensure_output_dir, connect_playwright_async_with_timeout, FailedRecord, validate_pdf, check_chrome_cdp, get_vpn_domain


DEFAULT_COUNT = 10
DEFAULT_OUTPUT = "./IEEE_Results"


def parse_args(args_text: str) -> dict:
    """解析 IEEE 参数

    格式: keyword | startYear endYear | sortBy | count | outputDir | vpnDomain
    """
    params = {
        "keyword": "",
        "start_year": 2020,
        "end_year": 2026,
        "sort_by": "citations",
        "count": 10,
        "output_dir": "./IEEE_Results",
        "vpn_domain": "",
    }
    if not args_text or not args_text.strip():
        return params
    parts = [p.strip() for p in args_text.split("|")]
    if len(parts) >= 1 and parts[0]:
        params["keyword"] = parts[0]
    if len(parts) >= 2 and parts[1]:
        years = parts[1].split()
        if years[0].isdigit():
            params["start_year"] = int(years[0])
        if len(years) >= 2 and years[1].isdigit():
            params["end_year"] = int(years[1])
    if len(parts) >= 3 and parts[2]:
        sort_map = {"cited": "citations", "citations": "citations", "newest": "date", "date": "date"}
        params["sort_by"] = sort_map.get(parts[2].lower(), "citations")
    if len(parts) >= 4 and parts[3].isdigit():
        params["count"] = int(parts[3])
    if len(parts) >= 5 and parts[4]:
        params["output_dir"] = parts[4]
    if len(parts) >= 6 and parts[5]:
        params["vpn_domain"] = parts[5]
    return params


def ieee_domain(vpn_domain=""):
    """返回 IEEE 基础域名，走VPN则使用代理域名"""
    if vpn_domain:
        return f"ieeexplore-ieee-org-443.{vpn_domain}"
    return "ieeexplore.ieee.org"


def build_search_url(query, start_year, end_year, sort_by, vpn_domain=""):
    """构建 IEEE Xplore 搜索 URL（支持VPN代理）"""
    domain = ieee_domain(vpn_domain)
    return (
        f"https://{domain}/search/searchresult.jsp"
        f"?queryText={urllib.parse.quote(query)}"
        "&highlight=true&returnFacets=ALL&returnType=SEARCH&matchPubs=true"
        f"&ranges={start_year}_{end_year}_PYear"
        f"&sortType={sort_by}"
    )


async def search_ieee(page, query, start_year, end_year, sort_by, vpn_domain=""):
    """搜索 IEEE 并提取论文列表"""
    search_url = build_search_url(query, start_year, end_year, sort_by, vpn_domain)
    log("IEEE", f"Searching: {query}")
    log("IEEE", f"URL: {search_url}")

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
                const section = a.closest('div[class], li, article, section') ||
                               a.parentElement?.closest('div, li') || a.parentElement;
                const contextText = section ? section.textContent : '';
                const heading = a.closest('h2, h3, h4');
                const title = heading ? heading.textContent.trim() : a.textContent.trim();
                const yearMatch = contextText.match(/\\b(20[12]\\d)\\b/);
                const year = yearMatch ? yearMatch[1] : '';
                papers.push({ title, link: a.href, year });
            });
            return papers;
        }
    """)

    valid = [p for p in papers if p.get("year", "").isdigit()
             and start_year <= int(p["year"]) <= end_year]
    if len(valid) >= 3:
        papers = valid

    return papers


async def try_direct_pdf(page, doc_id, vpn_domain=""):
    """尝试通过 IEEE stamp 端点直接下载 PDF"""
    domain = ieee_domain(vpn_domain)
    stamp_url = f"https://{domain}/stamp/stamp.jsp?tp=&arnumber={doc_id}"
    resp = await page.context.request.get(stamp_url, timeout=30000)
    if resp:
        content = await resp.body()
        if b"%PDF" in content[:500] or len(content) > 80000:
            return content
    return None


async def try_print_pdf(page, doc_id, vpn_domain=""):
    """通过 VPN 访问文章页，用打印到PDF下载（适用于非OA论文）"""
    domain = ieee_domain(vpn_domain)
    article_url = f"https://{domain}/document/{doc_id}"
    try:
        await page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        # 检查是否可访问（非OA也能看到摘要页）
        title = await page.title()
        if "IEEE" not in title and "Page" not in title:
            return None

        # 打印到PDF
        pdf_data = await page.pdf()
        if len(pdf_data) > 50000:
            return pdf_data
        return None
    except Exception:
        return None


async def download_papers(page, papers, output_dir, count, failed, vpn_domain=""):
    """批量下载 PDF，返回成功数"""
    downloaded = 0
    for i, paper in enumerate(papers[:count]):
        title = paper.get("title", "")
        link = paper.get("link", "")
        doi = paper.get("doi", "")

        if not link:
            continue

        safe_title = safe_filename(title, 80) or f"paper_{i+1}"
        log("IEEE", f"  [{i+1}/{min(count, len(papers))}] {safe_title[:55]}...")

        doc_match = re.search(r"/document/(\d+)", link)
        doc_id = doc_match.group(1) if doc_match else None
        if not doc_id:
            failed.add(title=title, link=link, source="IEEE", reason="No document ID in URL")
            log("IEEE", "    No document ID")
            continue

        # Method 1: IEEE direct (for OA)
        content = await try_direct_pdf(page, doc_id, vpn_domain)
        if content:
            fp = os.path.join(output_dir, f"{i+1:02d}_{safe_title}.pdf")
            with open(fp, "wb") as f:
                f.write(content)
            ok, msg = validate_pdf(fp)
            if ok:
                log("IEEE", f"    DOWNLOADED from IEEE ({len(content)} bytes)")
                downloaded += 1
                continue
            try:
                os.remove(fp)
            except OSError:
                pass

        # Method 2: Print-to-PDF via VPN (for non-OA)
        if vpn_domain:
            log("IEEE", "    Trying print-to-PDF via VPN...")
            content = await try_print_pdf(page, doc_id, vpn_domain)
            if content:
                fp = os.path.join(output_dir, f"{i+1:02d}_{safe_title}.pdf")
                with open(fp, "wb") as f:
                    f.write(content)
                ok, msg = validate_pdf(fp)
                if ok:
                    log("IEEE", f"    DOWNLOADED via VPN print-to-PDF ({len(content)} bytes)")
                    downloaded += 1
                    continue
                try:
                    os.remove(fp)
                except OSError:
                    pass

        # Method 3: Sci-Hub fallback (inline)
        if doi:
            try:
                from scihub_dl import try_scihub_download
                content = await try_scihub_download(doi, None)
                if content and len(content) > 50000:
                    fp = os.path.join(output_dir, f"{i+1:02d}_{safe_title}.pdf")
                    with open(fp, "wb") as f:
                        f.write(content)
                    ok, msg = validate_pdf(fp)
                    if ok:
                        log("IEEE", f"    DOWNLOADED via Sci-Hub ({len(content)} bytes)")
                        downloaded += 1
                        continue
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            except Exception:
                pass
            failed.add(title=title, doi=doi, link=link, source="IEEE",
                       reason="Sci-Hub failed", landing_url=link)
        else:
            failed.add(title=title, link=link, source="IEEE",
                       reason="No DOI", landing_url=link)

        log("IEEE", "    Not available")

    return downloaded


async def main_async(args_text: str):
    """异步主入口"""
    params = parse_args(args_text)
    output_dir = ensure_output_dir(params["output_dir"])
    vpn_domain = params.get("vpn_domain", "")

    log("IEEE", "=" * 50)
    log("IEEE", f"Query: {params['keyword']}")
    log("IEEE", f"Year: {params['start_year']}-{params['end_year']}")
    log("IEEE", f"Sort: {params['sort_by']} | Count: {params['count']} | Output: {output_dir}")
    if vpn_domain:
        log("IEEE", f"VPN: {vpn_domain}")

    log("IEEE", "Connecting to Chrome...")
    ok, info = check_chrome_cdp()
    if not ok:
        log("IEEE", f"ERROR: Chrome CDP not available: {info}")
        return
    p, browser = None, None
    try:
        p, browser, page = await connect_playwright_async_with_timeout(timeout=30000)
    except Exception as e:
        log("IEEE", f"ERROR: Cannot connect to Chrome: {e}")
        return

    try:
        papers = await search_ieee(page, params["keyword"], params["start_year"], params["end_year"], params["sort_by"], vpn_domain)
        log("IEEE", f"Found {len(papers)} papers in range {params['start_year']}-{params['end_year']}.")
        log("IEEE", f"Found {len(papers)} papers in range {params['start_year']}-{params['end_year']}.")
        for i, paper in enumerate(papers[: params["count"]]):
            sp(f"  {i+1:2d}. [{paper.get('year','?')}] {paper['title'][:70]}")
        sp("")

        if not papers:
            log("IEEE", "No papers found. Try broader keywords.")
            return

        save_json(papers[: params["count"]], os.path.join(output_dir, "papers_list.json"))

        failed = FailedRecord()
        dl_count = await download_papers(page, papers, output_dir, params["count"], failed, vpn_domain)

        log("IEEE", f"Done! Papers found: {len(papers)}, PDFs downloaded: {dl_count}/{min(params['count'], len(papers))}")
        if failed.count > 0:
            xlsx = failed.save_xlsx(output_dir)
            log("IEEE", f"Failed records saved: {xlsx} ({failed.count} papers)")

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
