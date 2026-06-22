---
name: paper-finder
description: 学术文献批量检索下载工具，支持 Springer Link / CNKI 知网 / IEEE Xplore / EBSCOhost / Web of Science / Wiley / Sci-Hub / OpenAlex / Crossref / Semantic Scholar / ScienceDirect 共11个数据库源的搜索与PDF下载。触发词：paper-finder、文献下载、找文献、paper download、文献检索、论文下载、Springer、CNKI、IEEE、EBSCO、Web of Science、Wiley、Sci-Hub、OpenAlex、Semantic Scholar、Crossref、ScienceDirect、学术搜索、批量下载。
---

# PaperFinder — 学术文献批量检索下载工具

Use this skill to search academic sources, collect paper metadata, and download valid PDF files when available.

## Setup

Install dependencies once:

```bash
pip install -r requirements.txt
playwright install chromium
```

Start Chrome with CDP before browser-based sources:

```bash
python scripts/chrome.py
```

Some sources require a university WebVPN or institutional login. Ask the user to log in manually in the Chrome window, then continue only after they confirm the login is complete.

## Unified Command Format

Prefer the stable pipe format for all sources:

```bash
python scripts/main.py <source> "keyword | startYear endYear | count | outputDir"
```

Source-specific options go after the fourth segment as named options:

```bash
python scripts/main.py openalex "large language models | 2024 2026 | 5 | ./papers | field=Computer Science"
python scripts/main.py sl "retrieval augmented generation | 2024 2026 | 5 | ./papers | sort=relevance"
python scripts/main.py cnki "deep learning | 2024 2026 | 20 | ./papers | sources=CSSCI,SCI | sort=cited"
```

Legacy source-specific positional formats are still accepted where possible, but new calls should use the unified first four segments.

## Sources

- `openalex` / `oa`: OpenAlex REST API search and OA PDF download.
- `crossref` / `cr`: Crossref REST API search with OA PDF discovery.
- `semantic` / `ss`: Semantic Scholar browser search and PDF download.
- `sl` / `springer`: Springer Link OA search and PDF download.
- `wiley`: Wiley Online Library browser search and PDF download.
- `ieee`: IEEE Xplore browser search with Sci-Hub fallback.
- `wos`: Web of Science browser workflow.
- `sd` / `sciencedirect`: ScienceDirect browser workflow, often WebVPN-dependent.
- `cnki`: CNKI browser workflow, WebVPN-dependent.
- `ebsco`: EBSCOhost browser workflow, WebVPN-dependent.
- `scihub` / `sci-hub`: DOI/title/keyword based Sci-Hub downloader.

## Quality Rules

- Treat a command as successful only when a downloaded file passes PDF validation.
- Non-PDF content, empty files, HTML pages, HTTP errors, and missing PDF links must be recorded as failures.
- Failure records are written as `失败记录.xlsx` or CSV fallback with English field names including `source`, `query`, `title`, `doi`, `landing_url`, `pdf_url`, `status`, `failure_reason`, `raw_error`, and `timestamp`.
- For sources that require VPN or institutional access, distinguish login/access problems from true zero-result searches whenever possible.

## Examples

```bash
python scripts/main.py openalex "large language models | 2024 2026 | 5 | ./papers | field=Computer Science"
python scripts/main.py crossref "large language models | 2024 2026 | 5 | ./papers"
python scripts/main.py semantic "large language models | 2024 2026 | 5 | ./papers"
python scripts/main.py sl "retrieval augmented generation | 2024 2026 | 5 | ./papers | sort=relevance"
python scripts/main.py wiley "large language models | 2024 2026 | 5 | ./papers"
python scripts/main.py ieee "large language models | 2024 2026 | 5 | ./papers | sort=citations"
python scripts/main.py scihub --doi "10.1109/ACCESS.2023.3312345" --output ./papers
```
