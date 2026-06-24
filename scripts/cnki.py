"""CNKI 知网 — 搜索 + 筛选 + 批量下载 PDF/CAJ

通过 Playwright CDP + Chrome WebVPN 操作知网 CNKI。
支持关键词搜索、年份范围、来源类型（SCI/CSSCI/北大核心等）、
排序方式选择、批量下载 PDF/CAJ。

用法:
  python main.py cnki "keyword | startYear endYear | sources | sortBy | count | outputDir"
"""

import time
import os
import re
import json
import urllib.parse

from utils import sp, log, safe_filename, ensure_output_dir, FailedRecord, get_vpn_domain, parse_standard_args, check_chrome_cdp


DEFAULT_COUNT = 20
DEFAULT_OUTPUT = "./CNKI_Results"
CNKI_VPN_DOMAIN = get_vpn_domain("kns-cnki-net-443.webvpn.upc.edu.cn")


def parse_args(args_text: str) -> dict:
    """解析 CNKI 专有参数

    格式: keyword | startYear endYear | sources | sortBy | count | outputDir
    sources: 逗号分隔，如 CSSCI,SCI,北大核心
    sortBy: 被引/下载/时间/相关
    """
    params = {
        "keyword": "",
        "start_year": None,
        "end_year": None,
        "sources": [],
        "sort_by": "被引",
        "count": DEFAULT_COUNT,
        "output_dir": DEFAULT_OUTPUT,
    }
    parsed = parse_standard_args(args_text, params, {"sort": "sort_by", "sort_by": "sort_by", "source": "sources"})
    return parsed


def connect_browser():
    """连接到已打开的 Chrome（CDP 端口 9222）"""
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
    except Exception:
        p.stop()
        raise
    context = browser.contexts[0]

    # 检查是否已有知网页面，复用而非新建
    for pg in context.pages:
        if "cnki" in pg.url.lower() and "kns8" in pg.url.lower():
            log("CNKI", f"Reusing existing CNKI tab: {pg.url[:80]}")
            try:
                pg.goto(pg.url, wait_until="load", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            return p, browser, context, pg

    # 创建新标签页并导航到知网VPN
    page = context.new_page()
    nav_url = f"https://{CNKI_VPN_DOMAIN}/kns8s/defaultresult/index"
    try:
        page.goto(nav_url, wait_until="load", timeout=30000)
        time.sleep(3)
    except Exception:
        pass
    return p, browser, context, page


def search(page, keyword, start_year=None, end_year=None):
    """搜索关键词 - 优先直接URL"""
    log("CNKI", f"Searching: {keyword}")
    time.sleep(2)

    # 直接构造搜索URL
    encoded = urllib.parse.quote(keyword)
    search_url = f"https://{CNKI_VPN_DOMAIN}/kns8s/defaultresult/index?korder=SU&kw={encoded}"
    if start_year and end_year:
        search_url += f"&year={start_year}%2C{end_year}"
    try:
        page.goto(search_url, wait_until="load", timeout=30000)
        time.sleep(3)
        log("CNKI", f"Navigated to search URL: {search_url[:80]}...")
    except Exception as e:
        log("CNKI", f"Navigation exception: {e}")
        time.sleep(2)


def set_filters(page, start_year, end_year, sources, sort_by):
    """设置筛选条件：年份、来源、排序"""
    log("CNKI", "Setting filters...")

    if start_year and end_year:
        try:
            page.evaluate(f"""
            () => {{
                const sy = document.getElementById('txtStartYear');
                const ey = document.getElementById('txtEndYear');
                if (sy) {{ sy.value = '{start_year}'; sy.dispatchEvent(new Event('input', {{bubbles: true}})); }}
                if (ey) {{ ey.value = '{end_year}'; ey.dispatchEvent(new Event('input', {{bubbles: true}})); }}
            }}
            """)
            log("CNKI", f"Year: {start_year}-{end_year}")
            time.sleep(1)
        except Exception as e:
            log("CNKI", f"Year filter exception: {e}")

    for source in sources:
        try:
            page.evaluate(f"""
            () => {{
                const links = document.querySelectorAll('.sidebar-filter a');
                for (const el of links) {{
                    if ((el.textContent || '').trim() === '{source}') {{
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }}
            """)
            log("CNKI", f"Source: {source}")
            time.sleep(1)
        except Exception as e:
            log("CNKI", f"Source '{source}' exception: {e}")

    # 排序
    try:
        page.evaluate(f"""
        () => {{
            const items = document.querySelectorAll('li');
            for (const el of items) {{
                if ((el.textContent || '').trim() === '{sort_by}') {{
                    el.click();
                    return true;
                }}
            }}
            return false;
        }}
        """)
        log("CNKI", f"Sort: {sort_by}")
        time.sleep(2)
    except Exception as e:
        log("CNKI", f"Sort exception: {e}")


def get_articles(page, count):
    """获取文献列表 - 支持多个选择器"""
    time.sleep(3)
    articles = []
    try:
        articles = page.evaluate(f"""
        () => {{
            const results = [];
            const seen = new Set();

            // Try multiple CSS selectors for article title links
            const selectors = [
                'a.fz14',           // old CNKI
                '.title a',          // common pattern
                'dt a', 'dd a',      // definition list
                '[class*="title"] a',
                '[class*="result"] a',
                'a[name]',
                'h3 a', 'h4 a',      // heading links
                'td.gwname a',       // CNKI result table
                '.name a',
                '.latestResult a',
            ];
            for (const sel of selectors) {{
                const els = document.querySelectorAll(sel);
                els.forEach(el => {{
                    const t = (el.textContent || '').trim();
                    const h = el.href || '';
                    if (t.length > 5 && !seen.has(t) && h && !h.startsWith('javascript')) {{
                        seen.add(t);
                        results.push({{title: t, href: h}});
                    }}
                }});
                if (results.length > 0) break;
            }}

            // Fallback: get all links with meaningful text
            if (results.length === 0) {{
                const allLinks = document.querySelectorAll('a');
                allLinks.forEach(el => {{
                    const t = (el.textContent || '').trim();
                    const h = el.href || '';
                    if (t.length > 10 && !seen.has(t) && h && h.indexOf('kns8') > -1) {{
                        seen.add(t);
                        results.push({{title: t, href: h}});
                    }}
                }});
            }}

            return JSON.stringify(results.slice(0, {count}));
        }}
        """)
        articles = json.loads(articles)
    except Exception as e:
        log("CNKI", f"Get articles exception: {e}")
    log("CNKI", f"Found {len(articles)} articles")
    if len(articles) > 0:
        for a in articles[:3]:
            log("CNKI", f"  - {a['title'][:50]}")
    return articles


def download_articles(context, articles, count, output_dir, failed):
    """批量下载文献"""
    actual = min(count, len(articles))
    log("CNKI", f"Downloading {actual} papers to {output_dir}")
    os.makedirs(output_dir, exist_ok=True)

    # 设置下载目录
    if context.pages:
        try:
            cdp = context.new_cdp_session(context.pages[0])
            cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": os.path.abspath(output_dir)
            })
        except Exception:
            pass

    success, fail_count = 0, 0
    for idx in range(actual):
        art = articles[idx]
        title = art["title"]
        href = art["href"]
        log("CNKI", f"[{idx+1}/{actual}] {title[:50]}")

        try:
            tab = context.new_page()
            tab.set_default_timeout(30000)
            tab.goto(href, wait_until="load", timeout=60000)
            time.sleep(4)

            result = tab.evaluate("""() => {
                const links = document.querySelectorAll('a');
                for (const el of links) {
                    const t = (el.textContent || '').trim();
                    if (t === 'PDF下载') { el.click(); return 'pdf'; }
                }
                for (const el of links) {
                    if ((el.textContent || '').trim() === 'CAJ下载') { el.click(); return 'caj'; }
                }
                return 'none';
            }""")

            if result == "pdf":
                log("CNKI", "  ✓ PDF download triggered")
                success += 1
            elif result == "caj":
                log("CNKI", "  ⚠ CAJ download triggered")
                success += 1
            else:
                failed.add(title=title, link=href, source="CNKI", reason="No PDF/CAJ download button on detail page")
                log("CNKI", "  ✗ No PDF/CAJ button found")
                fail_count += 1

            time.sleep(5)
            tab.close()
            time.sleep(1)
        except Exception as e:
            failed.add(title=title, link=href, source="CNKI", reason=str(e)[:60])
            log("CNKI", f"  ✗ Error: {str(e)[:60]}")
            fail_count += 1

    log("CNKI", f"Done: {success} success, {fail_count} failed")
    return success, fail_count


def main(args_text: str):
    """CNKI 主流程"""
    params = parse_args(args_text)
    keyword = params["keyword"]
    if not keyword:
        keyword = input("Enter search keyword: ").strip()
        if not keyword:
            log("CNKI", "Keyword required.")
            return

    output_dir = ensure_output_dir(params["output_dir"])

    log("CNKI", f"Keyword: {keyword}")
    log("CNKI", f"Sources: {','.join(params['sources']) or 'all'} | Sort: {params['sort_by']} | Count: {params['count']} | Output: {output_dir}")

    ok, info = check_chrome_cdp()
    if not ok:
        log("CNKI", f"ERROR: Chrome CDP not available: {info}")
        log("CNKI", "Run 'python scripts/chrome.py' first, or start Chrome with --remote-debugging-port=9222")
        return

    p, browser, context, page = connect_browser()
    try:
        search(page, keyword, params["start_year"], params["end_year"])
        # 跳过 set_filters，年份已在 URL 中指定
        articles = get_articles(page, params["count"])

        if not articles:
            log("CNKI", "No articles found, exiting.")
            return

        # 保存列表
        list_path = os.path.join(output_dir, "CNKI_列表.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write(f"CNKI 检索结果\n关键词: {keyword}\n")
            f.write(f"来源: {','.join(params['sources']) or '全部'}\n排序: {params['sort_by']}\n")
            f.write(f"共 {len(articles)} 篇\n{'='*60}\n\n")
            for i, a in enumerate(articles):
                f.write(f"{i+1}. {a['title']}\n   URL: {a['href']}\n\n")
        log("CNKI", f"List saved: {list_path}")

        # Auto-continue to download
        actual_count = min(params['count'], len(articles))
        log("CNKI", f"Downloading {actual_count} papers...")

        failed_rec = FailedRecord()
        download_articles(context, articles, params["count"], output_dir, failed_rec)
        if failed_rec.count > 0:
            xlsx = failed_rec.save_xlsx(output_dir)
            log("CNKI", f"Failed records saved: {xlsx} ({failed_rec.count} papers)")

    finally:
        browser.close()
        p.stop()


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
