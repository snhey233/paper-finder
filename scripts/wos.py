"""Web of Science — 高级检索 + 期刊筛选 + OA 批量下载 PDF

通过 Playwright CDP + WebVPN 操作 Web of Science Core Collection。
支持关键词搜索、期刊来源筛选、Open Access 过滤、逐篇获取 Free Full Text PDF。

用法:
  python main.py wos "keyword | startYear endYear | maxPDFs | outputDir"
"""

import asyncio
import os
import re
import base64
import json
import urllib.parse
from datetime import datetime, timezone

from utils import sp, log, safe_filename, ensure_output_dir, connect_playwright_async_with_timeout, FailedRecord, parse_standard_args, get_scihub_domains, check_chrome_cdp, validate_pdf


DEFAULT_COUNT = 10
DEFAULT_OUTPUT = "./WoS_Results"

# Sci-Hub fallback domains (from .env or defaults)
SCIHUB_DOMAINS = get_scihub_domains()

# Blocked publishers (WebVPN restrictions)
BLOCKED_PUBLISHERS = ["elsevier", "sciencedirect", "wiley"]


def parse_args(args_text: str) -> dict:
    """解析 WoS 参数

    格式: keyword | startYear endYear | maxPDFs | outputDir
    """
    params = {
        "keyword": "",
        "start_year": None,
        "end_year": None,
        "count": DEFAULT_COUNT,
        "output_dir": DEFAULT_OUTPUT,
    }
    return parse_standard_args(args_text, params)


# ── Helper functions ─────────────────────────────────────────────────────

async def wait(t=2):
    """Short async sleep helper"""
    await asyncio.sleep(t)


async def find_pdf_url(page):
    """Find PDF URL on a publisher page using multiple strategies."""
    return await page.evaluate("""
    () => {
        var m = document.querySelector('meta[name="citation_pdf_url"]');
        if (m && m.content) return m.content;
        var l = document.querySelector('link[type="application/pdf"]');
        if (l && l.href) return l.href;
        for (var s of ['a[href*="pdf"]','a[href$=".pdf"]','a[href*="/pdf"]',
                        'a[href*="download"]','a[href*="epdf"]',
                        'a[class*="pdf"]','a[class*="download"]'])
            for (var e of document.querySelectorAll(s))
                if (e.href) return e.href;
        for (var a of document.querySelectorAll('a'))
            if (a.href && (a.href.includes('pdf') || a.href.endsWith('.pdf')
                || a.href.includes('/download') || a.href.includes('epdf')))
                return a.href;
        return null;
    }
    """)


async def find_doi(page):
    """Extract DOI from publisher page."""
    doi = await page.evaluate(
        'document.querySelector(\'meta[name="citation_doi"]\')?.content || ""'
    )
    if not doi:
        doi = await page.evaluate("""
        () => {
            var m = document.querySelector('a[href*="doi.org"]');
            if (m) { var r = m.href.match(/10\\.\\d{4,}[^&?#\\s]+/); if (r) return r[0]; }
            return '';
        }
        """)
    return doi


async def fetch_pdf_b64(page, pdf_url):
    """Fetch PDF via browser context → base64 DataURL."""
    if not pdf_url:
        return None
    return await page.evaluate(f"""
    async () => {{
        try {{
            var r = await fetch('{pdf_url}');
            if (!r.ok) return 'HTTP' + r.status;
            var b = await r.blob();
            var rd = new FileReader();
            return await new Promise(function(rl) {{
                rd.onload = function() {{ rl(rd.result); }};
                rd.readAsDataURL(b);
            }});
        }} catch(e) {{ return 'ERR' + e.message; }}
    }}
    """)


async def scihub_download(ctx, doi):
    """Try Sci-Hub as fallback for blocked publishers."""
    if not doi:
        return None
    for domain in SCIHUB_DOMAINS:
        hp = await ctx.new_page()
        try:
            await hp.goto(f"{domain}/{doi}", timeout=25000)
            await wait(5)
            b64 = await hp.evaluate("""
            async () => {
                for (var sel of ['embed[type="application/pdf"]', 'iframe#pdf', 'iframe[src*="download"]']) {
                    var el = document.querySelector(sel);
                    if (!el) continue;
                    var src = el.getAttribute('src') || '';
                    if (!src) continue;
                    if (!src.startsWith('http')) src = window.location.origin + src;
                    try {
                        var r = await fetch(src);
                        if (r.ok) {
                            var b = await r.blob();
                            if (b.size > 5000) {
                                var rd = new FileReader();
                                return await new Promise(function(rl) {
                                    rd.onload = function() { rl(rd.result); };
                                    rd.readAsDataURL(b);
                                });
                            }
                        }
                    } catch(e) {}
                }
                return null;
            }
            """)
            if b64 and b64.startswith("data:application/pdf;base64,"):
                return b64
        except Exception:
            pass
        finally:
            await hp.close()
    return None


def is_blocked_publisher(url):
    """Check if publisher page URL belongs to a blocked publisher."""
    url_lower = url.lower()
    for bp in BLOCKED_PUBLISHERS:
        if bp in url_lower:
            return True
    return False


def save_pdf_b64(b64_string, title, counter, output_dir, suffix=""):
    """Decode base64 PDF and save to file. Returns file path or None."""
    if not b64_string or not b64_string.startswith("data:application/pdf;base64,"):
        return None
    raw = base64.b64decode(b64_string.split(",")[1])
    safe = safe_filename(title, 40)
    suffix_str = f"_{suffix}" if suffix else ""
    fname = f"{counter:02d}_{safe}{suffix_str}.pdf"
    fpath = os.path.join(output_dir, fname)
    with open(fpath, "wb") as f:
        f.write(raw)
    ok, msg = validate_pdf(fpath)
    if ok:
        return fpath
    try:
        os.remove(fpath)
    except OSError:
        pass
    return None


# ── Main WoS Workflow ────────────────────────────────────────────────────

async def main_async(args_text: str):
    """异步主入口"""
    params = parse_args(args_text)
    keyword = params["keyword"]
    count = params["count"]
    output_dir = ensure_output_dir(params["output_dir"])

    if not keyword:
        log("WoS", "Keyword is required.")
        return

    log("WoS", "=" * 50)
    log("WoS", f"Keyword: {keyword} | Count: {count} | Output: {output_dir}")

    ok, info = check_chrome_cdp()
    if not ok:
        log("WoS", f"ERROR: Chrome CDP not available: {info}")
        log("WoS", "Run 'python scripts/chrome.py' first, or start Chrome with --remote-debugging-port=9222")
        return

    # Connect to Chrome
    log("WoS", "Connecting to Chrome...")
    p, browser = None, None
    try:
        p, browser, page = await connect_playwright_async_with_timeout(timeout=30000)
    except Exception as e:
        log("WoS", f"ERROR: Cannot connect to Chrome: {e}")
        log("WoS", "Ensure Chrome is running with --remote-debugging-port=9222")
        return

    ctx = browser.contexts[0]
    wos = page

    try:
        # Find existing WoS page
        for pg in ctx.pages:
            if "webofscience" in pg.url:
                wos = pg
                log("WoS", "Found existing WoS page.")
                break

        # Close publisher tabs from previous runs
        for pg in ctx.pages:
            if "webofscience" not in pg.url and "webvpn" not in pg.url \
               and "chrome" not in pg.url and "newtab" not in pg.url:
                try:
                    await pg.close()
                except Exception:
                    pass

        # ===== Cookie consent =====
        try:
            cookie_btn = wos.locator('[data-ta*="cookie"], [aria-label*="cookie"], button:has-text("Accept"), button:has-text("同意"), button:has-text("Accept All")')
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
                log("WoS", "Cookie consent accepted.")
                await wait(1)
        except Exception:
            pass

        # ===== STEP 1: Search =====
        if "summary" not in wos.url:
            log("WoS", "[1] Searching...")
            inp = wos.locator('[data-ta="search-criteria-input"]')
            await inp.click()
            await wait(0.3)
            await inp.fill(keyword)
            await wait(0.3)
            await wos.keyboard.press("Enter")
            await wait(10)

            if "basic-search" in wos.url:
                log("WoS", "Enter key failed, trying force click...")
                await wos.evaluate(
                    'document.querySelector(\'[data-ta="run-search"]\')?.click()'
                )
                await wait(10)

            if "basic-search" in wos.url:
                log("WoS", f"Auto-search failed. Please search '{keyword}' manually in Chrome.")
                return

        log("WoS", f"[OK] {(await wos.title())[:70]}")

        # ===== STEP 2: Open Access filter =====
        log("WoS", "\n[2] Open Access filter...")
        await wos.evaluate("""
        () => {
            for (var cb of document.querySelectorAll('[role="checkbox"]')) {
                var a = (cb.getAttribute('aria-label')||'').toLowerCase();
                if (a.includes('open access') && !cb.checked) { cb.click(); return; }
            }
        }
        """)
        await wait(8)
        log("WoS", f"  Results: {(await wos.title())[:70]}")

        # ===== STEP 3: Year filter if specified =====
        if params["start_year"] and params["end_year"]:
            log("WoS", f"\n[3] Year filter: {params['start_year']}-{params['end_year']}...")
            await wos.evaluate(f"""
            () => {{
                var yearRanges = document.querySelectorAll('[data-ta="filter-section-YR"]');
                for (var sec of yearRanges) {{
                    var btn = sec.querySelector('[aria-expanded]');
                    if (btn && btn.getAttribute('aria-expanded') === 'false') btn.click();
                    var inputs = sec.querySelectorAll('input[type="text"]');
                    if (inputs.length >= 2) {{
                        inputs[0].value = '{params["start_year"]}';
                        inputs[1].value = '{params["end_year"]}';
                        inputs[0].dispatchEvent(new Event('input', {{bubbles:true}}));
                        inputs[1].dispatchEvent(new Event('input', {{bubbles:true}}));
                        var refineBtn = sec.querySelector('button[data-ta="refine"]');
                        if (refineBtn) refineBtn.click();
                    }}
                    break;
                }}
            }}
            """)
            await wait(8)

        # ===== STEP 4: Download PDFs =====
        log("WoS", f"\n[4] Downloading PDFs...")
        failed = FailedRecord()
        downloaded = 0
        need = count

        for pg_num in range(1, 11):
            if downloaded >= need:
                break

            if pg_num > 1:
                try:
                    np = wos.locator('[aria-label="Next page"], [data-ta="next-page"]')
                    await np.first.click(timeout=5000)
                    await wait(6)
                    try:
                        await wos.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    await wait(3)
                except Exception:
                    break

            # Scroll to trigger lazy loading
            for i in range(8):
                await wos.evaluate("window.scrollBy(0, 400)")
                await wait(0.3)

            # Extract articles
            arts = await wos.evaluate("""
            () => {
                var r = [];
                for (var rec of document.querySelectorAll('app-record')) {
                    var te = rec.querySelector('app-summary-title');
                    if (!te) continue;
                    var se = rec.querySelector('[data-ta="source"]');
                    var ls = [];
                    var le = rec.querySelector('app-summary-record-links');
                    if (le) for (var a of le.querySelectorAll('a'))
                        ls.push({text: a.textContent.trim(), href: a.href});
                    r.push({title: te.textContent.trim().substring(0,100),
                            source: se ? se.textContent.trim().substring(0,50) : '',
                            links: ls});
                }
                return r;
            }
            """) or []

            for idx, a in enumerate(arts):
                if downloaded >= need:
                    break

                free_links = [l for l in a["links"] if "Free" in l["text"]]
                if not free_links:
                    continue

                log("WoS", f"\n  [{downloaded+1}/{need}] {a['title'][:45]}")

                # Click Free Full Text link
                await wos.evaluate(f"""
                () => {{
                    var recs = document.querySelectorAll('app-record');
                    var rec = recs[{idx}];
                    if (rec) {{
                        for (var a of rec.querySelectorAll('a'))
                            if (a.textContent.includes('Free Full Text')) {{ a.click(); return; }}
                    }}
                }}
                """)
                await wait(8)

                # Find publisher page
                pub_page = None
                for pg in ctx.pages:
                    if pg != wos and "webofscience" not in pg.url:
                        pub_page = pg
                        break

                if not pub_page:
                    failed.add(title=a["title"], source="WoS", reason="No publisher page opened after clicking Free Full Text")
                    log("WoS", "    No publisher page opened")
                    continue

                pub_url = pub_page.url
                log("WoS", f"    Publisher: {pub_url[:70]}")

                # Skip blocked publishers → Sci-Hub
                if is_blocked_publisher(pub_url):
                    log("WoS", "    Blocked publisher, trying Sci-Hub...")
                    doi = await find_doi(pub_page)
                    if doi:
                        b64 = await scihub_download(ctx, doi)
                        if b64:
                            path = save_pdf_b64(b64, a["title"], downloaded + 1, output_dir, suffix="scihub")
                            if path:
                                sz = os.path.getsize(path)
                                log("WoS", f"    [OK] Sci-Hub ({sz//1024} KB)")
                                downloaded += 1
                        else:
                            failed.add(title=a["title"], doi=doi, link=pub_url, source="WoS", reason="Sci-Hub fallback failed for blocked publisher")
                    else:
                        failed.add(title=a["title"], link=pub_url, source="WoS", reason="Blocked publisher, no DOI found for Sci-Hub")
                    try:
                        await pub_page.close()
                    except Exception:
                        pass
                    continue

                await wait(5)

                # Direct download
                pdf_url = await find_pdf_url(pub_page)
                b64 = await fetch_pdf_b64(pub_page, pdf_url) if pdf_url else None

                if b64 and b64.startswith("data:application/pdf;base64,"):
                    path = save_pdf_b64(b64, a["title"], downloaded + 1, output_dir)
                    if path:
                        sz = os.path.getsize(path)
                        log("WoS", f"    [OK] ({sz//1024} KB)")
                        downloaded += 1
                else:
                    log("WoS", f"    Direct failed: {str(b64)[:40]}")
                    # Sci-Hub fallback
                    doi = await find_doi(pub_page)
                    if doi:
                        b64 = await scihub_download(ctx, doi)
                        if b64:
                            path = save_pdf_b64(b64, a["title"], downloaded + 1, output_dir, suffix="scihub")
                            if path:
                                sz = os.path.getsize(path)
                                log("WoS", f"    [OK] Sci-Hub ({sz//1024} KB)")
                                downloaded += 1
                        else:
                            failed.add(title=a["title"], doi=doi, link=pub_url, source="WoS", reason="Direct + Sci-Hub both failed")
                    else:
                        failed.add(title=a["title"], link=pub_url, source="WoS", reason=f"Direct download failed ({str(b64)[:30]}), no DOI for Sci-Hub")

                try:
                    await pub_page.close()
                except Exception:
                    pass
                await wait(3)

        # ===== Summary =====
        log("WoS", f"\nDone! Downloaded {downloaded} PDFs to {os.path.abspath(output_dir)}")
        if failed.count > 0:
            xlsx = failed.save_xlsx(output_dir)
            log("WoS", f"Failed records saved: {xlsx} ({failed.count} papers)")

        # Save metadata
        meta = {
            "keyword": keyword,
            "download_time": datetime.now(timezone.utc).isoformat(),
            "downloaded": downloaded,
            "output_dir": output_dir,
        }
        meta_path = os.path.join(output_dir, "papers_list.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

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
