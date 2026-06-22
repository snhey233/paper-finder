"""OpenAlex — API 搜索 + PDF 下载（无需浏览器）

通过 OpenAlex REST API 检索文献，支持关键词、年份范围、学科领域筛选，
自动识别并下载 Open Access PDF。

用法:
  python main.py openalex "keyword | startYear endYear | field | count | outputDir"
  python main.py oa "keyword | startYear endYear | field | count | outputDir"
"""

import sys
import os
import re
import json
import requests

from utils import sp, log, safe_filename, ensure_output_dir, validate_pdf, FailedRecord, parse_standard_args, looks_like_direct_pdf_url


API_BASE = "https://api.openalex.org/works"
DEFAULT_COUNT = 5
DEFAULT_OUTPUT = "./OpenAlex_Results"


def parse_args(args_text: str) -> dict:
    """解析 OpenAlex 参数

    格式: keyword | startYear endYear | field | count | outputDir
    """
    params = {
        "keyword": "",
        "start_year": None,
        "end_year": None,
        "field": "",
        "count": DEFAULT_COUNT,
        "output_dir": DEFAULT_OUTPUT,
    }
    return parse_standard_args(args_text, params, {"field": "field", "concept": "field", "topic": "field"})


def validate_params(params):
    """校验参数有效性"""
    if params["start_year"] and params["end_year"]:
        if params["start_year"] > params["end_year"]:
            log("OPENALEX", f"WARNING: start_year ({params['start_year']}) > end_year ({params['end_year']}), swapping")
            params["start_year"], params["end_year"] = params["end_year"], params["start_year"]


def search_works(params):
    """通过 OpenAlex API 搜索文献"""
    filters = []
    if params["start_year"]:
        filters.append(f"from_publication_date:{params['start_year']}-01-01")
    if params["end_year"]:
        filters.append(f"to_publication_date:{params['end_year']}-12-31")
    search_text = params["keyword"]
    if params["field"]:
        # OpenAlex does not accept concept.display_name as a filter. Treat the
        # field as an additional relevance term unless a future ID mapper is
        # available, which avoids the API 400 regression.
        search_text = f"{params['keyword']} {params['field']}".strip()

    query_params = {
        "search": search_text,
        "per_page": min(params["count"] * 3, 200),
    }
    if filters:
        query_params["filter"] = ",".join(filters)

    log("OPENALEX", f"Query: {params['keyword']}")
    if params["field"]:
        log("OPENALEX", f"Field hint: {params['field']} (used as search text, not raw API filter)")
    log("OPENALEX", f"Filters: {filters}")
    log("OPENALEX", f"Count: {params['count']}")

    try:
        resp = requests.get(API_BASE, params=query_params, timeout=30, headers={
            "User-Agent": "mailto:research@openalex.org"
        })
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        log("OPENALEX", f"Found {len(results)} works")

        papers = []
        for r in results:
            doi = (r.get("doi") or "").replace("https://doi.org/", "")
            oa_location = r.get("open_access", {}) or {}
            pdf_url = ""
            # Also try best_oa_location
            best_loc = r.get("best_oa_location", {}) or {}
            if best_loc.get("pdf_url"):
                pdf_url = best_loc["pdf_url"]
            elif oa_location.get("is_oa") and oa_location.get("pdf_url"):
                pdf_url = oa_location["pdf_url"]

            authors = [a.get("author", {}).get("display_name", "")
                       for a in (r.get("authorships") or []) if a.get("author")]
            papers.append({
                "title": r.get("title", ""),
                "doi": doi,
                "year": r.get("publication_year"),
                "authors": ", ".join(authors[:5]),
                "cited": r.get("cited_by_count", 0),
                "pdf_url": pdf_url,
                "is_oa": oa_location.get("is_oa", False),
                "landing_url": (best_loc.get("landing_page_url") if best_loc else "") or r.get("id", ""),
            })
        return papers[:params["count"]]
    except Exception as e:
        log("OPENALEX", f"API error: {e}")
        return []


def download_pdf(pdf_url, title, index, output_dir):
    """下载单篇 PDF"""
    if not pdf_url:
        return None, "No PDF URL found"
    if not looks_like_direct_pdf_url(pdf_url):
        return None, "not direct PDF URL"

    try:
        resp = requests.get(pdf_url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (compatible; OpenAlexDownloader/1.0)"
        })
        if resp.status_code != 200:
            log("OPENALEX", f"  HTTP {resp.status_code}")
            return None, f"HTTP {resp.status_code}"

        safe = safe_filename(title, 80).replace(" ", "_")
        fname = f"{index:02d}_{safe}.pdf"
        fpath = os.path.join(output_dir, fname)

        with open(fpath, "wb") as f:
            f.write(resp.content)

        ok, msg = validate_pdf(fpath)
        if ok:
            log("OPENALEX", f"  [OK] {fname} ({len(resp.content)//1024} KB)")
            return fpath, ""
        else:
            os.remove(fpath)
            log("OPENALEX", f"  {msg}")
            return None, msg
    except Exception as e:
        log("OPENALEX", f"  error: {e}")
        return None, f"{type(e).__name__}: {e}"


def main(args_text: str):
    """主流程"""
    params = parse_args(args_text)
    validate_params(params)

    if not params["keyword"]:
        log("OPENALEX", "Keyword required.")
        return

    output_dir = ensure_output_dir(params["output_dir"])
    papers = search_works(params)

    if not papers:
        log("OPENALEX", "No papers found.")
        return

    log("OPENALEX", f"\nResults ({len(papers)} papers):")
    for i, p in enumerate(papers, 1):
        oa = "🔓" if p["is_oa"] else " "
        sp(f"  {i:2d}.{oa} [{p['year']}] {p['title'][:70]}")
        if p["pdf_url"]:
            sp(f"      PDF: {p['pdf_url'][:80]}")

    # Save metadata
    meta_path = os.path.join(output_dir, "papers_list.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    log("OPENALEX", f"List saved: {meta_path}")

    # Download OA PDFs
    failed = FailedRecord()
    downloaded = 0
    for i, p in enumerate(papers, 1):
        if p["is_oa"] and p["pdf_url"]:
            result, reason = download_pdf(p["pdf_url"], p["title"], i, output_dir)
            if result:
                downloaded += 1
            else:
                failed.add(title=p["title"], doi=p["doi"], link=p["pdf_url"], source="OpenAlex", reason=reason,
                           query=params["keyword"], year=p.get("year", ""), authors=p.get("authors", ""),
                           landing_url=p.get("landing_url", ""), pdf_url=p.get("pdf_url", ""))
        elif not p["is_oa"]:
            failed.add(title=p["title"], doi=p["doi"], source="OpenAlex", reason="Not open access",
                       query=params["keyword"], year=p.get("year", ""), authors=p.get("authors", ""),
                       landing_url=p.get("landing_url", ""), pdf_url=p.get("pdf_url", ""))
        else:
            failed.add(title=p["title"], doi=p["doi"], source="OpenAlex", reason="No PDF URL found",
                       query=params["keyword"], year=p.get("year", ""), authors=p.get("authors", ""),
                       landing_url=p.get("landing_url", ""), pdf_url=p.get("pdf_url", ""))

    log("OPENALEX", f"Done! {downloaded}/{len(papers)} PDFs downloaded to {output_dir}")
    if failed.count > 0:
        xlsx = failed.save_xlsx(output_dir)
        log("OPENALEX", f"Failed records: {xlsx} ({failed.count} papers)")


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(args)
