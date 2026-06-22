"""Crossref — API 搜索 + PDF 下载（无需浏览器）

通过 Crossref REST API 检索文献，支持关键词、年份范围筛选，
自动查找并下载 OA PDF。

用法:
  python main.py crossref "keyword | startYear endYear | count | outputDir"
  python main.py cr "keyword | startYear endYear | count | outputDir"

管道格式:
  第1段: 关键词
  第2段: 起止年份 (空格分隔, 如 "2024 2026")
  第3段: 数量 (默认 5)
  第4段: 输出目录 (默认 ./Crossref_Results)
"""

import sys
import os
import re
import json
import requests

from utils import sp, log, safe_filename, ensure_output_dir, validate_pdf, FailedRecord, clean_doi, parse_standard_args


API_BASE = "https://api.crossref.org/works"
DEFAULT_COUNT = 5
DEFAULT_OUTPUT = "./Crossref_Results"


def parse_args(args_text: str) -> dict:
    """Parse Crossref parameters using standardized pipe format.

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


def search_works(params):
    """通过 Crossref API 搜索文献"""
    filters = []
    if params["start_year"]:
        filters.append(f"from-pub-date:{params['start_year']}-01-01")
    if params["end_year"]:
        filters.append(f"until-pub-date:{params['end_year']}-12-31")

    query_params = {
        "query": params["keyword"],
        "rows": min(params["count"] * 3, 50),
        "sort": "relevance",
        "order": "desc",
    }
    if filters:
        query_params["filter"] = ",".join(filters)

    log("CROSSREF", f"Query: {params['keyword']}")
    log("CROSSREF", f"Year: {params['start_year']}-{params['end_year']}")
    log("CROSSREF", f"Target count: {params['count']}")

    try:
        resp = requests.get(API_BASE, params=query_params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("message", {}).get("items", [])
        log("CROSSREF", f"Fetched {len(items)} works")

        papers = []
        for r in items:
            doi = r.get("DOI", "")
            title = (r.get("title") or [""])[0]
            year = (r.get("published-print") or r.get("published-online") or r.get("created") or {}).get("date-parts", [[None]])[0][0]
            authors = [a.get("family", "") for a in (r.get("author") or []) if a.get("family")]
            # Check OA status — Crossref API has 'is_oa' field in some responses
            is_oa = any(
                link.get("content-type") in ("application/pdf", "unspecified")
                and link.get("URL")
                for link in r.get("link") or []
            )

            # Try to find PDF URL from link field
            pdf_url = ""
            for link in r.get("link") or []:
                if link.get("content-type") in ("application/pdf", "unspecified") and link.get("URL"):
                    pdf_url = link["URL"]
                    break

            papers.append({
                "title": title,
                "doi": doi,
                "year": year,
                "authors": ", ".join(authors[:5]),
                "cited": r.get("is-referenced-by-count", 0),
                "pdf_url": pdf_url,
            })

        # Year filtering (API may not filter precisely)
        if params["start_year"] and params["end_year"]:
            filtered = [p for p in papers if p["year"] is not None and params["start_year"] <= p["year"] <= params["end_year"]]
            log("CROSSREF", f"After year filter: {len(filtered)} papers")
            return filtered[:params["count"]]

        return papers[:params["count"]]
    except Exception as e:
        log("CROSSREF", f"API error: {e}")
        return []


def find_oa_pdf(doi, title):
    """Multi-strategy OA PDF discovery: Unpaywall → direct publisher → Crossref link field."""
    if not doi:
        return ""

    # Strategy 1: Unpaywall API
    try:
        # Use a generic contact email as required by Unpaywall ToS
        url = f"https://api.unpaywall.org/v2/{doi}?email=research@unpaywall.org"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            oa_loc = data.get("best_oa_location", {}) or {}
            for key in ("pdf_url", "url_for_pdf"):
                if oa_loc.get(key):
                    return oa_loc[key]
            # Also check all OA locations
            for loc in data.get("oa_locations", []) or []:
                if loc.get("pdf_url"):
                    return loc["pdf_url"]
    except Exception:
        pass

    # Strategy 2: Check direct publisher PDF URL patterns via HEAD
    patterns = [
        f"https://doi.org/{doi}",
        f"https://doi.org/{doi}/pdf",
        f"https://api.crossref.org/works/{doi}/fulltext/pdf",
    ]
    for url in patterns:
        try:
            resp = requests.head(url, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 allow_redirects=True)
            ct = resp.headers.get("Content-Type", "").lower()
            if "pdf" in ct:
                return url
            # Follow redirect and check final URL
            if resp.headers.get("Location", "").lower().endswith(".pdf"):
                return resp.headers["Location"]
        except Exception:
            pass

    # Strategy 3 (last resort): try /pdf endpoint
    return f"https://api.crossref.org/works/{doi}/fulltext/pdf"


def download_pdf(pdf_url, title, doi, index, output_dir):
    """Download single PDF with Content-Type preflight and content validation."""
    if not pdf_url:
        return None, "No PDF URL found"

    try:
        # HEAD preflight: skip if Content-Type is text/html and not PDF
        try:
            head = requests.head(pdf_url, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 allow_redirects=True)
            ct = head.headers.get("Content-Type", "").lower()
            if ct and "text/html" in ct and "pdf" not in ct:
                return None, f"URL returns HTML (Content-Type: {ct}) — not a direct PDF"
        except Exception:
            pass  # proceed to GET anyway

        # GET with stream to validate early
        resp = requests.get(pdf_url, timeout=60, stream=True,
                            headers={"User-Agent": "Mozilla/5.0"},
                            allow_redirects=True)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        # Read first chunk for content validation
        content_chunks = []
        first_chunk = True
        for chunk in resp.iter_content(chunk_size=8192):
            content_chunks.append(chunk)
            if first_chunk and not chunk.startswith(b"%PDF"):
                # Not PDF — check if it's HTML
                text_start = chunk[:200].strip().lower()
                if text_start.startswith(b"<") or b"<!doctype" in text_start or b"<html" in text_start:
                    return None, f"URL returned HTML (landing page), not PDF"
                # Check if %PDF exists within first 200 bytes
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
            log("CROSSREF", f"  [OK] {fname} ({len(content)//1024} KB)")
            return fpath, ""
        else:
            os.remove(fpath)
            log("CROSSREF", f"  {msg}")
            return None, msg
    except Exception as e:
        log("CROSSREF", f"  error: {e}")
        return None, f"{type(e).__name__}: {e}"


def main(args_text: str):
    """主流程"""
    params = parse_args(args_text)

    if not params["keyword"]:
        log("CROSSREF", "Keyword required.")
        sp("Usage: python main.py crossref \"keyword | startYear endYear | count | outputDir\"")
        return

    output_dir = ensure_output_dir(params["output_dir"])
    log("CROSSREF", f"Target count: {params['count']} | Output: {output_dir}")

    papers = search_works(params)

    if not papers:
        log("CROSSREF", "No papers found.")
        return

    log("CROSSREF", f"\nResults ({len(papers)} papers):")
    for i, p in enumerate(papers, 1):
        sp(f"  {i:2d}. [{p.get('year','?')}] {p['title'][:70]}")
        if p.get("doi"):
            sp(f"      DOI: {p['doi']}")

    # Save metadata
    meta_path = os.path.join(output_dir, "papers_list.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    log("CROSSREF", f"List saved: {meta_path}")

    # Try to find and download PDFs
    failed = FailedRecord()
    downloaded = 0
    for i, p in enumerate(papers, 1):
        log("CROSSREF", f"  [{i}/{len(papers)}] {p['title'][:50]}")
        pdf_url = p.get("pdf_url", "") or find_oa_pdf(p.get("doi", ""), p.get("title", ""))

        if pdf_url:
            result, reason = download_pdf(pdf_url, p["title"], p.get("doi", ""), i, output_dir)
            if result:
                downloaded += 1
            else:
                failed.add(title=p["title"], doi=p.get("doi", ""), link=pdf_url, source="Crossref",
                           reason=reason, query=params["keyword"], year=p.get("year", ""),
                           authors=p.get("authors", ""), pdf_url=pdf_url)
        else:
            failed.add(title=p["title"], doi=p.get("doi", ""), source="Crossref",
                       reason="No PDF URL found", query=params["keyword"], year=p.get("year", ""),
                       authors=p.get("authors", ""))

    log("CROSSREF", f"Done! {downloaded}/{len(papers)} PDFs downloaded to {output_dir}")
    if failed.count > 0:
        xlsx = failed.save_xlsx(output_dir)
        log("CROSSREF", f"Failed records: {xlsx} ({failed.count} papers)")


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
