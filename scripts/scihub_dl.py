"""Sci-Hub — 通过浏览器 CDP 或直连下载 PDF（无需 PyPI scihub 包）

用法:
  python main.py scihub "10.1109/ACCESS.2023.3312345"
  python main.py scihub --doi "10.1109/ACCESS.2023.3312345"
  python main.py scihub --title "deep learning review"
  python main.py scihub --keyword "machine learning" --results 10
"""

import sys
import os
import re
import json
import base64
import shlex

from utils import sp, log, safe_filename, ensure_output_dir, validate_pdf, FailedRecord, get_scihub_domains, check_chrome_cdp


DEFAULT_OUTPUT = "./SciHub_Results"
SCIHUB_DOMAINS = get_scihub_domains()

# Pre-check: filter to reachable domains at module load time
_REACHABLE_DOMAINS = None  # lazy-loaded on first use


def _get_reachable_domains():
    """Return only domains that respond to HEAD, lazily cached."""
    global _REACHABLE_DOMAINS
    if _REACHABLE_DOMAINS is not None:
        return _REACHABLE_DOMAINS
    import requests
    reachable = []
    for domain in SCIHUB_DOMAINS:
        try:
            resp = requests.head(f"{domain}/", timeout=5,
                                 headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code < 500:
                reachable.append(domain)
        except Exception:
            continue
    _REACHABLE_DOMAINS = reachable or SCIHUB_DOMAINS  # fallback to all if none reachable
    if _REACHABLE_DOMAINS and _REACHABLE_DOMAINS != SCIHUB_DOMAINS:
        log("SCIHUB", f"Active domains: {len(_REACHABLE_DOMAINS)}/{len(SCIHUB_DOMAINS)}")
    return _REACHABLE_DOMAINS


def fetch_pdf_via_browser(doi, output_dir):
    """通过 Playwright CDP 访问 Sci-Hub 下载 PDF"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("SCIHUB", "playwright not installed, skipping browser method.")
        return None

    log("SCIHUB", "Trying browser-based Sci-Hub download...")
    active_domains = _get_reachable_domains()
    for domain in active_domains:
        p, browser = None, None
        try:
            p = sync_playwright().start()
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0]
            page = ctx.new_page()

            url = f"{domain}/{doi}"
            log("SCIHUB", f"  Opening {domain}/...")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)

            # Try to get PDF via embed/iframe
            pdf_b64 = page.evaluate("""
            async () => {
                // try embed
                for (const sel of ['embed[type="application/pdf"]', 'iframe#pdf',
                                    'iframe[src*="download"]', 'object[type="application/pdf"]']) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    let src = el.getAttribute('src') || el.getAttribute('data') || '';
                    if (!src) continue;
                    if (!src.startsWith('http')) src = window.location.origin + src;
                    try {
                        const resp = await fetch(src);
                        if (!resp.ok) continue;
                        const blob = await resp.blob();
                        if (blob.size < 5000) continue;
                        const buf = await blob.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = '';
                        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                        return 'data:application/pdf;base64,' + btoa(bin);
                    } catch(e) { continue; }
                }
                // try all links
                for (const a of document.querySelectorAll('a')) {
                    const h = a.href || '';
                    if (h.endsWith('.pdf') || h.includes('/download/') || h.includes('/storage/')) {
                        try {
                            const resp = await fetch(h);
                            if (!resp.ok) continue;
                            const blob = await resp.blob();
                            if (blob.size < 5000) continue;
                            const buf = await blob.arrayBuffer();
                            const bytes = new Uint8Array(buf);
                            let bin = '';
                            for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                            return 'data:application/pdf;base64,' + btoa(bin);
                        } catch(e) { continue; }
                    }
                }
                return null;
            }
            """)

            if pdf_b64 and pdf_b64.startswith("data:application/pdf;base64,"):
                raw = base64.b64decode(pdf_b64.split(",")[1])
                safe = safe_filename(doi, 40)
                fpath = os.path.join(output_dir, f"{safe}.pdf")
                with open(fpath, "wb") as f:
                    f.write(raw)
                ok, msg = validate_pdf(fpath)
                if ok:
                    log("SCIHUB", f"  [OK] {fpath} ({len(raw)//1024} KB)")
                    return fpath
                else:
                    os.remove(fpath)
                    log("SCIHUB", f"  Downloaded but {msg}")
            else:
                log("SCIHUB", f"  No PDF at {domain}")

        except Exception as e:
            log("SCIHUB", f"  {domain} error: {str(e)[:60]}")
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if p:
                try:
                    p.stop()
                except Exception:
                    pass

    return None


def fetch_pdf_direct(doi, output_dir):
    """通过 requests 直连 Sci-Hub 下载 PDF（首选）"""
    try:
        import requests
    except ImportError:
        log("SCIHUB", "requests not installed.")
        return None

    log("SCIHUB", "Trying direct Sci-Hub download...")
    active_domains = _get_reachable_domains()
    for domain in active_domains:
        try:
            resp = requests.get(f"{domain}/{doi}", timeout=30,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue

            html = resp.text

            # Remove base tag so relative URLs resolve correctly
            html_clean = re.sub(r'<base[^>]*>', '', html)

            # Find PDF URL in embed/iframe
            pdf_url = None
            m = re.search(r'embed[^>]+src="([^"]+)"', html)
            if m:
                pdf_url = m.group(1)
            else:
                m = re.search(r'iframe[^>]+src="([^"]+)"', html)
                if m:
                    pdf_url = m.group(1)

            if pdf_url:
                if pdf_url.startswith("//"):
                    pdf_url = "https:" + pdf_url
                elif pdf_url.startswith("/"):
                    pdf_url = domain + pdf_url
                elif not pdf_url.startswith("http"):
                    pdf_url = domain + "/" + pdf_url

                pdf_resp = requests.get(pdf_url, timeout=30,
                                        headers={"User-Agent": "Mozilla/5.0"})
                if pdf_resp.status_code == 200 and len(pdf_resp.content) > 5000:
                    safe = safe_filename(doi, 40).replace(" ", "_")
                    fpath = os.path.join(output_dir, f"{safe}.pdf")
                    with open(fpath, "wb") as f:
                        f.write(pdf_resp.content)
                    ok, msg = validate_pdf(fpath)
                    if ok:
                        log("SCIHUB", f"  [OK] {fpath} ({len(pdf_resp.content)//1024} KB)")
                        return fpath
                    else:
                        os.remove(fpath)

            # Fallback: try /pdf/ endpoint
            pdf_resp = requests.get(f"{domain}/pdf/{doi}", timeout=30,
                                    headers={"User-Agent": "Mozilla/5.0"})
            if pdf_resp.status_code == 200 and len(pdf_resp.content) > 5000:
                safe = safe_filename(doi, 40).replace(" ", "_")
                fpath = os.path.join(output_dir, f"{safe}.pdf")
                with open(fpath, "wb") as f:
                    f.write(pdf_resp.content)
                ok, msg = validate_pdf(fpath)
                if ok:
                    log("SCIHUB", f"  [OK] {fpath} (direct PDF)")
                    return fpath
                else:
                    os.remove(fpath)

        except Exception as e:
            log("SCIHUB", f"  {domain} error: {str(e)[:60]}")
            continue

    return None


def search_by_doi(doi, output_dir):
    """通过 DOI 下载 PDF"""
    output_dir = ensure_output_dir(output_dir)
    log("SCIHUB", f"Searching DOI: {doi}")

    # Method 1: direct HTTP
    result = fetch_pdf_direct(doi, output_dir)
    if result:
        return result

    # Method 2: browser CDP
    log("SCIHUB", "Direct failed, trying browser method...")
    result = fetch_pdf_via_browser(doi, output_dir)
    return result


def save_failed(output_dir, doi="", title="", keyword="", reason="Download failed"):
    failed = FailedRecord()
    failed.add(title=title or doi or keyword, doi=doi, source="SciHub", reason=reason, query=keyword)
    path = failed.save_xlsx(ensure_output_dir(output_dir))
    log("SCIHUB", f"Failed records: {path} ({failed.count} papers)")
    return path


def search_by_title(title, output_dir):
    """通过标题搜索，先 CrossRef 找 DOI，再下载"""
    try:
        import requests
    except ImportError:
        log("SCIHUB", "requests library required.")
        return None

    log("SCIHUB", f"Searching title: {title}")
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 5},
            timeout=15,
        )
        data = resp.json()
        items = data.get("message", {}).get("items", [])
        if not items:
            log("SCIHUB", "No DOI found.")
            return None

        for item in items:
            doi = item.get("DOI", "")
            if doi:
                log("SCIHUB", f"Found DOI: {doi}")
                return search_by_doi(doi, output_dir)

        log("SCIHUB", "No DOI found in results.")
        return None
    except Exception as e:
        log("SCIHUB", f"CrossRef error: {e}")
        return None


def search_by_keyword(keyword, num_results, output_dir):
    """通过关键词搜索，列出来供选择下载"""
    try:
        import requests
    except ImportError:
        log("SCIHUB", "requests library required.")
        return None

    log("SCIHUB", f"Searching keyword: {keyword} (top {num_results})")
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params={"query": keyword, "rows": num_results},
            timeout=15,
        )
        data = resp.json()
        items = data.get("message", {}).get("items", [])
        results = []
        for item in items:
            doi = item.get("DOI", "")
            title = (item.get("title") or [""])[0]
            year = (item.get("published-print") or item.get("published-online") or {}).get("date-parts", [[None]])[0][0]
            if doi:
                results.append({"doi": doi, "title": title, "year": year})
                log("SCIHUB", f"  [{len(results)}] {title[:60]} -> {doi}")

        output_dir = ensure_output_dir(output_dir)
        list_path = os.path.join(output_dir, "scihub_search_results.json")
        with open(list_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        log("SCIHUB", f"Results saved: {list_path}")

        log("SCIHUB", f"Downloading all {len(results)} papers...")
        for r in results:
            if r.get("doi"):
                search_by_doi(r["doi"], output_dir)
        return results
    except Exception as e:
        log("SCIHUB", f"CrossRef error: {e}")
        return None


def check_domains():
    """Probe each Sci-Hub domain with HEAD to check reachability."""
    import requests
    reachable = []
    for domain in SCIHUB_DOMAINS:
        try:
            resp = requests.head(f"{domain}/", timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code < 500:
                reachable.append(domain)
                log("SCIHUB", f"  {domain} -> reachable (HTTP {resp.status_code})")
            else:
                log("SCIHUB", f"  {domain} -> unreachable (HTTP {resp.status_code})")
        except Exception as e:
            log("SCIHUB", f"  {domain} -> error: {str(e)[:50]}")
    return reachable


def main(args_text: str):
    """主入口"""
    plain = args_text.strip().strip("\"'")

    # --list-domains flag
    if plain == "--list-domains":
        sp("Configured Sci-Hub domains:")
        for d in SCIHUB_DOMAINS:
            sp(f"  {d}")
        sp("\nTo customize, set SCIHUB_DOMAINS in .env file:")
        sp('  SCIHUB_DOMAINS=https://sci-hub.ru,https://sci-hub.se')
        sp("\nProbing domain reachability...")
        reachable = check_domains()
        if reachable:
            sp(f"\nReachable: {len(reachable)}/{len(SCIHUB_DOMAINS)}")
        else:
            sp("\nNo domains reachable. Sci-Hub may be blocked in your region.")
            sp("Try finding current Sci-Hub mirrors online and add them to .env")
        return
    # Detect bare DOI as positional argument: "10.xxxx/xxxx" without --flags
    if plain.startswith("10.") and "/" in plain and not plain.startswith("--"):
        result = search_by_doi(plain, DEFAULT_OUTPUT)
        if not result:
            save_failed(DEFAULT_OUTPUT, doi=plain, reason="Sci-Hub download failed")
            return False
        return True

    # CLI-style args
    import argparse
    parser = argparse.ArgumentParser(prog="scihub", add_help=False)
    parser.add_argument("--doi", type=str, default="")
    parser.add_argument("--title", type=str, default="")
    parser.add_argument("--keyword", type=str, default="")
    parser.add_argument("--results", type=int, default=10)
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT)

    try:
        parsed = parser.parse_args(shlex.split(args_text))
    except SystemExit:
        sp("Sci-Hub Download Tool")
        sp("Usage:")
        sp('  python main.py scihub "10.1109/ACCESS.2023.3312345"')
        sp('  python main.py scihub --doi "10.1109/ACCESS.2023.3312345"')
        sp('  python main.py scihub --title "deep learning review"')
        sp('  python main.py scihub --keyword "machine learning" --results 10')
        return False

    output_dir = parsed.output
    if parsed.doi:
        result = search_by_doi(parsed.doi, output_dir)
        if not result:
            save_failed(output_dir, doi=parsed.doi, reason="Sci-Hub download failed")
            return False
        return True
    elif parsed.title:
        result = search_by_title(parsed.title, output_dir)
        if not result:
            save_failed(output_dir, title=parsed.title, reason="No DOI or PDF found for title")
            return False
        return True
    elif parsed.keyword:
        results = search_by_keyword(parsed.keyword, parsed.results, output_dir)
        if not results:
            save_failed(output_dir, keyword=parsed.keyword, reason="No DOI results found")
            return False
        return True
    else:
        sp("Sci-Hub Download Tool")
        sp("Usage:")
        sp('  python main.py scihub "10.1109/ACCESS.2023.3312345"')
        sp('  python main.py scihub --doi "10.1109/ACCESS.2023.3312345"')
        sp('  python main.py scihub --title "deep learning review"')
        sp('  python main.py scihub --keyword "machine learning" --results 10')
        return False


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
