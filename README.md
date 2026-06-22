# download

Academic literature search and PDF download helper.

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium
python scripts/chrome.py
```

Then run:

```bash
python scripts/main.py <source> "keyword | startYear endYear | count | outputDir"
```

Examples:

```bash
python scripts/main.py openalex "large language models | 2024 2026 | 5 | ./papers | field=Computer Science"
python scripts/main.py crossref "large language models | 2024 2026 | 5 | ./papers"
python scripts/main.py semantic "large language models | 2024 2026 | 5 | ./papers"
python scripts/main.py sl "retrieval augmented generation | 2024 2026 | 5 | ./papers | sort=relevance"
python scripts/main.py scihub --doi "10.1109/ACCESS.2023.3312345" --output ./papers
```

## Supported Sources

| Source | Aliases | Notes |
|---|---|---|
| Springer Link | `sl`, `springer` | OA search and PDF download |
| CNKI | `cnki` | Requires WebVPN/institutional login |
| IEEE Xplore | `ieee` | Browser search, direct PDF, Sci-Hub fallback |
| EBSCOhost | `ebsco` | Requires WebVPN/institutional login |
| Web of Science | `wos` | Browser workflow, may require institutional login |
| Wiley | `wiley` | Browser search and PDF download |
| ScienceDirect | `sd`, `sciencedirect` | Often requires WebVPN/institutional login |
| Sci-Hub | `scihub`, `sci-hub` | DOI/title/keyword downloader |
| OpenAlex | `openalex`, `oa` | REST API metadata and OA PDF download |
| Crossref | `crossref`, `cr` | REST API metadata and OA PDF discovery |
| Semantic Scholar | `semantic`, `ss` | Browser search and PDF download |

## Unified Arguments

Prefer this format for every source:

```text
keyword | startYear endYear | count | outputDir
```

Add source-specific options after the output directory:

```text
keyword | startYear endYear | count | outputDir | field=Computer Science | sort=relevance
```

Useful options:

- `field=...` for OpenAlex field/topic hints.
- `sort=relevance`, `sort=date`, or `sort=citations` where supported.
- `sources=CSSCI,SCI` for CNKI source filters.

Legacy positional formats are still accepted where possible, but new automation should use the unified first four segments.

## Output Quality

The scripts validate saved PDFs with a `%PDF` header and minimum size check. Invalid downloads are deleted and written to failure records instead of being reported as success.

Failure records are saved as `失败记录.xlsx` or CSV fallback and use English field names:

```text
source, query, title, authors, year, doi, landing_url, pdf_url, status, failure_reason, raw_error, timestamp
```

## VPN Workflow

For CNKI, EBSCO, Web of Science, and ScienceDirect:

1. Start Chrome CDP with `python scripts/chrome.py`.
2. Navigate to the source or WebVPN login page.
3. Ask the user to log in manually.
4. Continue the download only after the user confirms the login is complete.

ScienceDirect, Wiley, IEEE, and Web of Science can also be affected by institutional access or anti-bot checks. Save and inspect debug HTML when a browser source returns zero results unexpectedly.
