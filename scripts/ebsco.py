"""EBSCOhost — VPN 搜索 + 全文下载（弹框下载 + API响应拦截）

通过 Playwright CDP 连接已打开的 Chrome，经学校 WebVPN 访问 EBSCOhost。
下载方式（已验证可行）：
  1. 搜索文章 → 进入详情页
  2. 点击 value="download" 按钮 → 弹出格式选择框（PDF默认选中）
  3. 点击 data-auto="bulk-download-modal-download-button" 确认下载
  4. 拦截 /api/search/v1/record/{id}/fulltext/pdf 响应 → 直接保存 PDF 文件

用法:
  python main.py ebsco "keyword | startYear endYear | count | outputDir | vpnDomain"
"""

import os
import re
import json
import base64
import urllib.parse
from datetime import datetime, timezone

from utils import sp, log, safe_filename, ensure_output_dir, FailedRecord, get_vpn_domain, connect_playwright_async_with_timeout, check_chrome_cdp, validate_pdf


DEFAULT_COUNT = 10
DEFAULT_OUTPUT = "./EBSCO_Results"
EBSCO_PROFILE_ID = os.environ.get("EBSCO_PROFILE_ID", "dmjzjj")


def parse_args(args_text: str) -> dict:
    params = {
        "keyword": "",
        "start_year": 2016,
        "end_year": 2026,
        "count": DEFAULT_COUNT,
        "output_dir": DEFAULT_OUTPUT,
        "vpn_domain": "",
    }
    if not args_text or not args_text.strip():
        return params
    parts = [p.strip() for p in args_text.split("|")]
    if len(parts) >= 1 and parts[0]:
        params["keyword"] = parts[0]
    if len(parts) >= 2 and parts[1]:
        years = parts[1].split()
        if len(years) >= 1 and years[0].isdigit():
            params["start_year"] = int(years[0])
        if len(years) >= 2 and years[1].isdigit():
            params["end_year"] = int(years[1])
    if len(parts) >= 3 and parts[2].isdigit():
        params["count"] = int(parts[2])
    if len(parts) >= 4 and parts[3]:
        params["output_dir"] = parts[3]
    if len(parts) >= 5 and parts[4]:
        params["vpn_domain"] = parts[4]
    return params


def _detect_vpn_domain(page, given_domain=""):
    """Detect VPN domain from current page URL, preferring EBSCO tabs."""
    if given_domain:
        return given_domain
    try:
        ctx = page.context
        for pg in ctx.pages:
            url = pg.url.lower()
            if "ebsco" in url and ("webvpn" in url or ".vpn." in url):
                parsed = urllib.parse.urlparse(pg.url)
                hostname = parsed.hostname or ""
                # Extract the outer VPN domain from proxied URL
                dot_parts = hostname.split(".")
                if len(dot_parts) > 2:
                    # e.g. research-ebsco-com-443.webvpn.upc.edu.cn -> webvpn.upc.edu.cn
                    vpn_match = re.search(r'([\w-]+\.(?:webvpn|vpn)\.[^/]+)', pg.url)
                    if vpn_match:
                        return vpn_match.group(1).split(".", 1)[1]
                log("EBSCO", f"Detected VPN domain (EBSCO tab): {hostname}")
                return hostname
        # Fallback: any VPN tab
        for pg in ctx.pages:
            url = pg.url.lower()
            if "webvpn." in url or ".vpn." in url:
                parsed = urllib.parse.urlparse(pg.url)
                hostname = parsed.hostname or ""
                log("EBSCO", f"Detected VPN domain (any tab): {hostname}")
                return hostname
    except Exception:
        pass
    # Last resort: env config
    domain = get_vpn_domain("")
    if domain:
        log("EBSCO", f"Using VPN domain from env: {domain}")
        return domain
    log("EBSCO", "Could not auto-detect VPN domain.")
    return ""


def _resolve_ebsco_domain(vpn_domain, page):
    """Build EBSCO proxied domain from VPN domain."""
    if not vpn_domain:
        vpn_domain = _detect_vpn_domain(page)
    if vpn_domain:
        return f"research-ebsco-com-443.{vpn_domain}"
    # Hardcoded fallback
    default_domain = f"research-ebsco-com-443.{get_vpn_domain('webvpn.upc.edu.cn')}"
    log("EBSCO", f"Using default domain: {default_domain}")
    return default_domain


async def main_async(args_text: str):
    """Async main workflow using Playwright."""
    params = parse_args(args_text)
    keyword = params["keyword"]
    if not keyword:
        log("EBSCO", "Keyword is required.")
        print("Enter search keyword (e.g. FinTech): ", end="")
        keyword = input().strip()
        if not keyword:
            log("EBSCO", "No keyword provided, exiting.")
            return
        params["keyword"] = keyword

    output_dir = ensure_output_dir(params["output_dir"])
    log("EBSCO", f"Keyword: {params['keyword']}")
    log("EBSCO", f"Year: {params['start_year']}-{params['end_year']} | Count: {params['count']} | Output: {output_dir}")
    log("EBSCO", f"VPN domain: {params['vpn_domain'] or '(auto-detect)'}")

    ok, info = check_chrome_cdp()
    if not ok:
        log("EBSCO", f"ERROR: Chrome CDP not available: {info}")
        log("EBSCO", "Run 'python scripts/chrome.py' first, or start Chrome with --remote-debugging-port=9222")
        return

    p, browser = None, None
    log("EBSCO", "Connecting to Chrome...")
    try:
        p, browser, page = await connect_playwright_async_with_timeout(timeout=30000)
    except Exception as e:
        log("EBSCO", f"Cannot connect to Chrome: {e}")
        log("EBSCO", 'Ensure Chrome running with: --remote-debugging-port=9222 --remote-allow-origins=*')
        return

    try:
        vpn_domain = params["vpn_domain"] or _detect_vpn_domain(page)
        ebsco_domain = _resolve_ebsco_domain(vpn_domain, page)

        # Navigate to EBSCO advanced search
        search_url = f"https://{ebsco_domain}/c/{EBSCO_PROFILE_ID}/search/advanced/filters"
        log("EBSCO", f"Opening EBSCO: {search_url[:80]}...")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)

        # Wait for page to stabilize (VPN login check built-in)
        log("EBSCO", "Checking page load...")
        await page.wait_for_timeout(3000)

        # Search
        log("EBSCO", f"Searching: {keyword}")
        try:
            search_input = page.locator('#search-autocomplete-1-input, input[type="text"]').first
            await search_input.click()
            await search_input.fill(keyword)
            await page.wait_for_timeout(1000)
        except Exception:
            log("EBSCO", "Search input not found, injecting keyword via URL...")
            search_url = f"https://{ebsco_domain}/c/{EBSCO_PROFILE_ID}/search/advanced/filters?q={urllib.parse.quote(keyword)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

        # Click search button
        try:
            search_btn = page.locator('button:has-text("搜索"), button:has-text("Search")').first
            await search_btn.click()
            await page.wait_for_timeout(5000)
        except Exception:
            log("EBSCO", "Search button not found, trying Enter key...")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)

        # Extract articles
        articles = await page.evaluate("""
        () => {
            var cards = document.querySelectorAll('[class*=search-result]');
            var results = [];
            var seen = {};
            cards.forEach(function(c) {
                var link = c.querySelector('a[href*="search/details"]');
                if (link && link.href) {
                    var m = link.href.match(/\\/details\\/([a-z0-9]+)/);
                    var title = (link.textContent || '').trim();
                    if (m && !seen[m[1]]) {
                        seen[m[1]] = true;
                        results.push({id: m[1], title: title.substring(0, 120)});
                    }
                }
            });
            return results;
        }
        """)
        log("EBSCO", f"Found {len(articles)} articles")
        if not articles:
            log("EBSCO", "No articles found. Try different keywords.")
            return

        for a in articles[:10]:
            sp(f"  [{a['id']}] {a['title'][:60]}")

        # Download via modal click + API response interception
        # EBSCO does not provide PDF via direct URL; it requires clicking the
        # download button in the detail page, selecting PDF in the modal,
        # and then capturing the /api/search/v1/record/{id}/fulltext/pdf response.
        failed = FailedRecord()
        downloaded = []

        for i, article in enumerate(articles[: params["count"]]):
            rec_id = article["id"]
            title = article.get("title", f"article_{rec_id}") or f"article_{rec_id}"
            safe_title = safe_filename(title, 50).replace(" ", "_")
            fname = f"{i+1:02d}_{safe_title}.pdf"
            fpath = os.path.join(output_dir, fname)

            log("EBSCO", f"  [{i+1}/{min(params['count'], len(articles))}] {title[:50]}...")

            # Navigate to article detail page
            detail_url = f"https://{ebsco_domain}/c/{EBSCO_PROFILE_ID}/search/details/{rec_id}"
            try:
                await page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
            except Exception:
                log("EBSCO", f"  detail page timeout for {rec_id}, skipping...")
                failed.add(title=title, source="EBSCO", reason=f"detail page timeout")
                continue

            # Check if there's a download button (full text available)
            has_download = await page.evaluate(
                "document.querySelector('button[value=\"download\"]') !== null"
            )
            if not has_download:
                log("EBSCO", "  no full text available (no download button)")
                failed.add(title=title, source="EBSCO", reason="no download button (no full text)")
                continue

            # Set up response interception for fulltext/pdf API
            pdf_data = []
            async def _capture_pdf(resp):
                if f"/record/{rec_id}/fulltext/pdf" in resp.url:
                    try:
                        body = await resp.body()
                        if len(body) > 50000:
                            pdf_data.append(body)
                    except Exception:
                        pass

            page.on("response", _capture_pdf)

            # Click download button -> modal opens (PDF format pre-selected)
            await page.evaluate("document.querySelector('button[value=\"download\"]')?.click()")
            await page.wait_for_timeout(3000)

            # Click the confirm download button in the modal
            await page.evaluate(
                "document.querySelector('button[data-auto=\"bulk-download-modal-download-button\"]')?.click()"
            )

            # Wait for PDF response
            for _ in range(20):
                if pdf_data:
                    break
                await page.wait_for_timeout(1000)

            if pdf_data and len(pdf_data[0]) > 50000:
                raw = pdf_data[0]
                with open(fpath, "wb") as f:
                    f.write(raw)
                sz = len(raw) // 1024
                is_valid = raw[:5] == b"%PDF-"
                if is_valid:
                    downloaded.append({"file": fname, "title": title, "size_kb": sz})
                    log("EBSCO", f"  ✓ {sz}KB [{len(downloaded)}/{params['count']}]")
                else:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    failed.add(title=title, source="EBSCO", reason=f"response is not a valid PDF",
                               landing_url=detail_url)
            else:
                failed.add(title=title, source="EBSCO",
                           reason=f"no PDF response from /fulltext/pdf API",
                           landing_url=detail_url)

            await page.wait_for_timeout(1000)

        log("EBSCO", f"Done! Downloaded {len(downloaded)} papers to {os.path.abspath(output_dir)}")
        if failed.count > 0:
            xlsx = failed.save_xlsx(output_dir)
            log("EBSCO", f"Failed records: {xlsx} ({failed.count} papers)")
        for r in downloaded:
            sp(f"  {r['file']}  ({r['size_kb']} KB)")

        # Save metadata
        meta = {
            "keyword": params["keyword"],
            "year_range": f"{params['start_year']}-{params['end_year']}",
            "download_time": datetime.now(timezone.utc).isoformat(),
            "papers": downloaded,
        }
        meta_path = os.path.join(output_dir, "papers_list.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        log("EBSCO", f"Metadata saved to {meta_path}")

    finally:
        if browser:
            await browser.close()
        if p:
            await p.stop()


def main(args_text: str):
    """同步入口"""
    import asyncio
    asyncio.run(main_async(args_text))


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
