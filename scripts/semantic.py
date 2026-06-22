"""Semantic Scholar — API 搜索 + OA PDF 下载（无需浏览器）

通过 Semantic Scholar GraphQL API (REST) 检索文献，不再使用浏览器。
首选 S2 API (api.semanticscholar.org)，回退使用 OpenAlex API。

用法:
  python main.py semantic "keyword | startYear endYear | count | outputDir"
  python main.py ss "keyword | startYear endYear | count | outputDir"
"""

import sys
import os
import json
import requests
import time

from utils import sp, log, safe_filename, ensure_output_dir, validate_pdf, FailedRecord, parse_standard_args


API_BASE = "https://api.semanticscholar.org/graph/v1/paper/search"
API_FIELDS = "title,year,authors,openAccessPdf,citationCount,externalIds,publicationDate"
OPENALEX_BASE = "https://api.openalex.org/works"

DEFAULT_COUNT = 5
DEFAULT_OUTPUT = "./Semantic_Results"

# Simple rate limiting with exponential backoff
_LAST_REQUEST_TIME = 0


def _rate_limit(retry_after=0):
    """Ensure minimum interval between API requests; wait longer if retry_after hints."""
    global _LAST_REQUEST_TIME
    now = time.time()
    elapsed = now - _LAST_REQUEST_TIME
    wait = max(1.0, retry_after)
    if elapsed < wait:
        time.sleep(wait - elapsed)
    _LAST_REQUEST_TIME = time.time()


def parse_args(args_text: str) -> dict:
    """Parse Semantic Scholar parameters using standardized pipe format.

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


def search_via_s2_api(keyword, start_year, end_year, count):
    """Search Semantic Scholar via GraphQL REST API, return paper list."""
    _rate_limit()

    params = {
        "query": keyword,
        "limit": min(count * 3, 100),
        "fields": API_FIELDS,
        "sort": "relevance",
    }
    if start_year:
        params["year"] = f"{start_year}-{end_year or start_year}"

    log("SEMANTIC", f"S2 API: {keyword}")
    try:
        resp = requests.get(API_BASE, params=params, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            log("SEMANTIC", f"S2 API rate limited (429). Waiting {retry_after}s...")
            _rate_limit(retry_after)
            resp = requests.get(API_BASE, params=params, timeout=30,
                                headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 403:
            log("SEMANTIC", "S2 API returned 403. Falling back to OpenAlex...")
            return None  # trigger fallback
        if resp.status_code == 429:
            log("SEMANTIC", "S2 API still rate limited after retry. Falling back to OpenAlex...")
            return None
        resp.raise_for_status()
        data = resp.json()
        papers = []
        for r in data.get("data", [])[:count]:
            oa = r.get("openAccessPdf") or {}
            authors_list = (r.get("authors") or [])[:5]
            papers.append({
                "title": r.get("title", ""),
                "year": _extract_year_from_paper(r),
                "authors": ", ".join(a.get("name", "") for a in authors_list),
                "citation_count": r.get("citationCount", 0),
                "doi": (r.get("externalIds") or {}).get("DOI", ""),
                "pdf_url": oa.get("url", ""),
                "link": f"https://www.semanticscholar.org/paper/{r.get('paperId', '')}",
                "source": "semantic_scholar",
            })
        log("SEMANTIC", f"S2 API found {len(papers)} papers")
        return papers
    except requests.exceptions.RequestException as e:
        log("SEMANTIC", f"S2 API error: {e}")
        return None  # trigger fallback


def _extract_year_from_paper(r):
    """Extract year from various Semantic Scholar fields."""
    pub_date = r.get("publicationDate") or ""
    if pub_date and len(pub_date) >= 4:
        try:
            return int(pub_date[:4])
        except ValueError:
            pass
    year = r.get("year")
    if year:
        return year
    return ""


def search_via_openalex(keyword, start_year, end_year, count):
    """Fallback: search OpenAlex API instead of Semantic Scholar."""
    _rate_limit()

    params = {
        "search": keyword,
        "per_page": min(count * 3, 50),
        "sort": "relevance_score:desc",
    }
    filters = []
    if start_year:
        filters.append(f"from_publication_date:{start_year}-01-01")
    if end_year:
        filters.append(f"to_publication_date:{end_year}-12-31")
    if filters:
        params["filter"] = ",".join(filters)

    log("SEMANTIC", f"OpenAlex fallback: {keyword}")
    _rate_limit(2)  # extra wait before hitting a second API
    try:
        resp = requests.get(OPENALEX_BASE, params=params, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 15))
            log("SEMANTIC", f"OpenAlex rate limited (429). Waiting {retry_after}s...")
            _rate_limit(retry_after)
            resp = requests.get(OPENALEX_BASE, params=params, timeout=30,
                                headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
        papers = []
        for r in (data.get("results") or [])[:count]:
            oa_locs = r.get("locations") or []
            pdf_url = ""
            for loc in oa_locs:
                if loc.get("is_oa") and loc.get("pdf_url"):
                    pdf_url = loc["pdf_url"]
                    break
            if not pdf_url:
                best_loc = r.get("best_oa_location") or {}
                pdf_url = best_loc.get("pdf_url") or ""

            authors_list = (r.get("authorships") or [])[:5]
            papers.append({
                "title": r.get("title", ""),
                "year": (r.get("publication_year") or ""),
                "authors": ", ".join(a.get("author", {}).get("display_name", "") for a in authors_list),
                "citation_count": r.get("cited_by_count", 0),
                "doi": r.get("doi", "").replace("https://doi.org/", ""),
                "pdf_url": pdf_url,
                "link": r.get("doi", ""),
                "source": "openalex",
            })
        log("SEMANTIC", f"OpenAlex fallback found {len(papers)} papers")
        return papers
    except requests.exceptions.RequestException as e:
        log("SEMANTIC", f"OpenAlex fallback error: {e}")
        return []


def download_pdf(pdf_url, title, index, output_dir):
    """Download single PDF via direct HTTP request."""
    if not pdf_url:
        return None, "No PDF URL found"

    try:
        # HEAD preflight
        try:
            head = requests.head(pdf_url, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 allow_redirects=True)
            ct = head.headers.get("Content-Type", "").lower()
            if ct and "text/html" in ct and "pdf" not in ct:
                return None, f"URL returns HTML (Content-Type: {ct})"
        except Exception:
            pass

        resp = requests.get(pdf_url, timeout=60, stream=True,
                            headers={"User-Agent": "Mozilla/5.0"},
                            allow_redirects=True)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        content_chunks = []
        first_chunk = True
        for chunk in resp.iter_content(chunk_size=8192):
            content_chunks.append(chunk)
            if first_chunk:
                text_start = chunk[:200].strip().lower()
                if text_start.startswith(b"<!") or b"<html" in text_start:
                    return None, "URL returned HTML landing page, not PDF"
                if b"%PDF" not in chunk[:200]:
                    return None, "Content does not appear to be PDF (no %PDF header)"
            first_chunk = False

        content = b"".join(content_chunks)
        safe = safe_filename(title, 80).replace(" ", "_") or f"paper_{index}"
        fname = f"{index:02d}_{safe}.pdf"
        fpath = os.path.join(output_dir, fname)

        with open(fpath, "wb") as f:
            f.write(content)

        ok, msg = validate_pdf(fpath)
        if ok:
            log("SEMANTIC", f"  [OK] {fname} ({len(content)//1024} KB)")
            return fpath, ""
        else:
            os.remove(fpath)
            return None, msg
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main(args_text: str):
    """Main entry — API search + PDF download (no browser needed)."""
    params = parse_args(args_text)

    if not params["keyword"]:
        log("SEMANTIC", "Keyword required.")
        sp('Usage: python main.py semantic "keyword | startYear endYear | count | outputDir"')
        return

    output_dir = ensure_output_dir(params["output_dir"])
    log("SEMANTIC", f"Query: {params['keyword']} | Year: {params['start_year']}-{params['end_year']} | Count: {params['count']} | Output: {output_dir}")

    # Search: try S2 API first, fall back to OpenAlex
    papers = search_via_s2_api(params["keyword"], params["start_year"], params["end_year"], params["count"])
    if papers is None:
        papers = search_via_openalex(params["keyword"], params["start_year"], params["end_year"], params["count"])

    if not papers:
        log("SEMANTIC", "No papers found.")
        return

    log("SEMANTIC", f"\nResults ({len(papers)} papers):")
    for i, p in enumerate(papers, 1):
        source_tag = "S2" if p.get("source") == "semantic_scholar" else "OA"
        pdf_flag = " [PDF]" if p.get("pdf_url") else ""
        sp(f"  {i:2d}. [{source_tag}{pdf_flag}] {p['title'][:70]}")
        if p.get("doi"):
            sp(f"       DOI: {p['doi']}")

    # Save metadata
    meta_path = os.path.join(output_dir, "papers_list.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    log("SEMANTIC", f"List saved: {meta_path}")

    # Download PDFs
    failed = FailedRecord()
    downloaded = 0
    for i, p in enumerate(papers, 1):
        log("SEMANTIC", f"  [{i}/{len(papers)}] {p['title'][:50]}")
        pdf_url = p.get("pdf_url", "")
        # Only use doi.org if we have valid OA pdf_url from API — don't fallback to doi.org
        # as it returns HTML landing page, not PDF

        if pdf_url:
            result, reason = download_pdf(pdf_url, p["title"], i, output_dir)
            if result:
                downloaded += 1
            else:
                failed.add(title=p["title"], doi=p.get("doi", ""), link=pdf_url, source="SemanticScholar",
                           reason=reason, query=params["keyword"], year=str(p.get("year", "")),
                           authors=p.get("authors", ""), pdf_url=pdf_url)
        else:
            failed.add(title=p["title"], doi=p.get("doi", ""), source="SemanticScholar",
                       reason="No PDF URL available", query=params["keyword"],
                       year=str(p.get("year", "")), authors=p.get("authors", ""))

    log("SEMANTIC", f"Done! {downloaded}/{len(papers)} PDFs downloaded to {output_dir}")
    if failed.count > 0:
        xlsx = failed.save_xlsx(output_dir)
        log("SEMANTIC", f"Failed records: {xlsx} ({failed.count} papers)")


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
