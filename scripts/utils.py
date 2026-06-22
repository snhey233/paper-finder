"""download — 学术文献批量检索下载工具集

共享工具函数：Chrome连接、参数解析、文件操作、控制台输出、失败记录导出Excel
"""

import sys
import os
import re
import json
import time
import urllib.parse
import csv
import asyncio


# ── 环境配置 ────────────────────────────────────────────────────────────────
def load_env():
    """加载 .env 文件（如果有），返回配置字典"""
    config = {
        "VPN_DOMAIN": os.environ.get("VPN_DOMAIN", ""),
        "SCIHUB_DOMAINS": os.environ.get("SCIHUB_DOMAINS", ""),
    }
    # 尝试从 .env 文件加载
    env_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ".env",
    ]
    for env_path in env_paths:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, val = line.partition("=")
                        key, val = key.strip(), val.strip().strip("\"'")
                        if key == "VPN_DOMAIN" and val:
                            config["VPN_DOMAIN"] = val
                        elif key == "SCIHUB_DOMAINS" and val:
                            config["SCIHUB_DOMAINS"] = val
            except Exception:
                pass
            break
    return config


_env_config = load_env()


def get_vpn_domain(default="", warn=True):
    """获取 VPN 域名，优先从环境变量/.env 读取"""
    domain = _env_config.get("VPN_DOMAIN") or default
    if warn and not domain:
        pass  # 调用方自行决定是否输出警告
    return domain


def get_scihub_domains():
    """获取 Sci-Hub 域名列表，优先从 .env/环境变量读取。

    用户可在 .env 中设置 SCIHUB_DOMAINS 为逗号分隔的 URL 列表：
      SCIHUB_DOMAINS=https://sci-hub.ru,https://sci-hub.se
    """
    env_domains = _env_config.get("SCIHUB_DOMAINS", "")
    if env_domains:
        return [d.strip() for d in env_domains.split(",") if d.strip()]
    return ["https://sci-hub.ru", "https://sci-hub.se", "https://sci-hub.sg"]


# ── 控制台输出 ────────────────────────────────────────────────────────────
def sp(*args, sep=" ", end="\n", flush=True):
    """Safe print — 处理 Windows GBK 编码问题"""
    text = sep.join(str(a) for a in args)
    try:
        print(text, end=end, flush=flush)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode(), end=end, flush=flush)


def log(tag, msg):
    """带标签的日志输出，如 [ieee] xxx"""
    sp(f"[{tag}] {msg}")


# ── 参数解析 ──────────────────────────────────────────────────────────────
def parse_pipe_args(raw_args: str, defaults: dict) -> dict:
    """解析管道符分隔的参数，合并默认值

    格式: keyword | startYear endYear | [extra...]
    返回: {keyword, start_year, end_year, ...}
    """
    params = dict(defaults)
    if not raw_args or not raw_args.strip():
        return params

    parts = [p.strip() for p in raw_args.split("|")]

    if len(parts) >= 1 and parts[0]:
        params["keyword"] = parts[0]
    if len(parts) >= 2 and parts[1]:
        years = parts[1].split()
        if len(years) >= 1 and years[0].isdigit():
            params["start_year"] = int(years[0])
        if len(years) >= 2 and years[1].isdigit():
            params["end_year"] = int(years[1])
    if len(parts) >= 3 and parts[2]:
        params["extra"] = parts[2]
    if len(parts) >= 4 and parts[3]:
        try:
            params["count"] = int(parts[3])
        except ValueError:
            params["extra2"] = parts[3]
    if len(parts) >= 5 and parts[4]:
        params["output_dir"] = parts[4]
    if len(parts) >= 6 and parts[5]:
        params["vpn_domain"] = parts[5]

    return params


def parse_standard_args(args_text: str, defaults: dict, option_aliases: dict = None) -> dict:
    """Parse the stable pipe format used by all sources.

    Format: keyword | startYear endYear | count | outputDir | key=value
    Older source-specific positional formats are still accepted when possible.
    """
    if option_aliases is None:
        option_aliases = {}
    params = dict(defaults)
    if not args_text or not args_text.strip():
        return params

    parts = [p.strip() for p in args_text.split("|")]
    option_aliases = option_aliases or {}

    if len(parts) >= 1 and parts[0]:
        if "keyword" in params:
            params["keyword"] = parts[0]
        elif "query" in params:
            params["query"] = parts[0]
    if len(parts) >= 2 and parts[1]:
        years = parts[1].split()
        if len(years) >= 1 and years[0].isdigit():
            params["start_year"] = int(years[0])
        if len(years) >= 2 and years[1].isdigit():
            params["end_year"] = int(years[1])
    if len(parts) >= 3 and parts[2]:
        if parts[2].isdigit():
            params["count"] = int(parts[2])
        elif "sort_by" in params:
            params["sort_by"] = parts[2]
        elif "field" in params:
            params["field"] = parts[2]
        elif "sources" in params:
            params["sources"] = [s.strip() for s in parts[2].split(",") if s.strip()]
    if len(parts) >= 4 and parts[3]:
        if parts[3].isdigit() and not parts[2].isdigit():
            params["count"] = int(parts[3])
        else:
            params["output_dir"] = parts[3]
    if len(parts) >= 5 and parts[4]:
        if parts[4].isdigit() and len(parts) >= 3 and not parts[2].isdigit():
            params["count"] = int(parts[4])
        elif "=" not in parts[4]:
            params["output_dir"] = parts[4]
    if len(parts) >= 6 and parts[5] and "=" not in parts[5]:
        params["output_dir"] = parts[5]

    for raw in parts[4:]:
        if "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip().lower().replace("-", "_")
        target = option_aliases.get(key, key)
        value = value.strip()
        if target == "count" and value.isdigit():
            params[target] = int(value)
        elif target == "sources":
            params[target] = [s.strip() for s in value.split(",") if s.strip()]
        elif value:
            params[target] = value

    return params


def safe_filename(text: str, max_len: int = 80) -> str:
    """将任意文本转为安全的文件名"""
    name = re.sub(r'[\\/*?:"<>|]', "_", text)
    return name.strip(". ")[:max_len] or "paper"


def extract_year(date_str: str) -> int:
    """从日期字符串中提取年份"""
    m = re.search(r"(20\d{2})", date_str or "")
    return int(m.group(1)) if m else 0


def validate_pdf(path, min_bytes=1024):
    """校验文件是否为有效 PDF

    Args:
        path: 文件路径
        min_bytes: 最小字节数（默认 1KB）

    Returns:
        (True, "") 或 (False, 原因)
    """
    if not os.path.exists(path):
        return False, "file not found"
    size = os.path.getsize(path)
    if size < min_bytes:
        return False, f"file too small ({size} bytes < {min_bytes})"
    if size == 0:
        return False, "file is empty (0 bytes)"
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        # PDF header starts with %PDF, possibly followed by version number
        if not header.startswith(b"%PDF"):
            return False, f"not a PDF (header: {header[:8].hex()})"
    except Exception as e:
        return False, f"read error: {e}"
    return True, ""


def looks_like_direct_pdf_url(url: str) -> bool:
    """Cheap preflight for URLs that are likely to return a PDF."""
    if not url:
        return False
    clean = url.split("?", 1)[0].lower()
    return clean.endswith(".pdf") or "/pdf" in clean or "pdf/" in clean or "download" in clean


def clean_doi(raw_url):
    """从 URL 或文本中提取纯 DOI

    >>> clean_doi("https://doi.org/10.1109/ACCESS.2023.3312345")
    '10.1109/ACCESS.2023.3312345'
    >>> clean_doi("10.1234/abc.2023.001")
    '10.1234/abc.2023.001'
    """
    if not raw_url:
        return ""
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9a-z]+)", raw_url)
    return m.group(1) if m else raw_url.strip()


def save_json(data, filepath):
    """保存 JSON 文件"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_list(articles, filepath, header_lines=None):
    """保存文献列表为文本文件"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        if header_lines:
            for line in header_lines:
                f.write(line + "\n")
            f.write("=" * 60 + "\n\n")
        for i, art in enumerate(articles):
            f.write(f"{i+1}. {art.get('title', '')}\n")
            for k, v in art.items():
                if k != "title":
                    f.write(f"   {k}: {v}\n")
            f.write("\n")
    return filepath


def ensure_output_dir(path):
    """确保输出目录存在，返回路径"""
    if not path:
        return None
    os.makedirs(path, exist_ok=True)
    return path


# ── 下载失败记录 + 导出 Excel ──────────────────────────────────────────────

class FailedRecord:
    """记录下载失败的论文信息，支持最后汇总导出 Excel/CSV"""

    def __init__(self):
        self.records = []  # [{title, doi, link, source, reason}, ...]

    def add(self, title="", doi="", link="", source="", reason="", **extra):
        self.records.append({
            "query": extra.get("query", ""),
            "title": title,
            "authors": extra.get("authors", ""),
            "year": extra.get("year", ""),
            "doi": doi,
            "landing_url": extra.get("landing_url", ""),
            "pdf_url": extra.get("pdf_url", link),
            "link": link,
            "source": source,
            "status": extra.get("status", "failed"),
            "reason": reason,
            "raw_error": extra.get("raw_error", ""),
            "timestamp": extra.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    @property
    def count(self):
        return len(self.records)

    def save_xlsx(self, output_dir, filename="失败记录.xlsx"):
        """导出为 Excel (.xlsx)，回退到 .csv"""
        if not self.records:
            return None

        filepath = os.path.join(output_dir, filename)
        os.makedirs(output_dir, exist_ok=True)

        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "失败记录"
            # 表头
            headers = ["index", "source", "query", "title", "authors", "year", "doi",
                       "landing_url", "pdf_url", "status", "failure_reason", "raw_error", "timestamp"]
            ws.append(headers)
            for i, r in enumerate(self.records, 1):
                ws.append([i, r.get("source", ""), r.get("query", ""), r.get("title", ""),
                           r.get("authors", ""), r.get("year", ""), r.get("doi", ""),
                           r.get("landing_url", ""), r.get("pdf_url", "") or r.get("link", ""),
                           r.get("status", "failed"), r.get("reason", ""),
                           r.get("raw_error", ""), r.get("timestamp", "")])
            # 调整列宽
            for col in ws.columns:
                max_len = max((len(str(cell.value or "")) for cell in col), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)
            wb.save(filepath)
            return filepath
        except ImportError:
            # 无 openpyxl，回退到 CSV
            csv_path = filepath.replace(".xlsx", ".csv")
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["index", "source", "query", "title", "authors", "year", "doi",
                                 "landing_url", "pdf_url", "status", "failure_reason", "raw_error", "timestamp"])
                for i, r in enumerate(self.records, 1):
                    writer.writerow([i, r.get("source", ""), r.get("query", ""), r.get("title", ""),
                                     r.get("authors", ""), r.get("year", ""), r.get("doi", ""),
                                     r.get("landing_url", ""), r.get("pdf_url", "") or r.get("link", ""),
                                     r.get("status", "failed"), r.get("reason", ""),
                                     r.get("raw_error", ""), r.get("timestamp", "")])
            return csv_path


# ── Chrome CDP 连接 (Playwright) ──────────────────────────────────────────

import urllib.request as _urllib_request
import urllib.error as _urllib_error
import json as _json


def check_chrome_cdp(port=9222):
    """检查 Chrome CDP 是否可连接。返回 (ok, info)。"""
    try:
        resp = _urllib_request.urlopen(f"http://localhost:{port}/json/version", timeout=5)
        data = _json.loads(resp.read().decode())
        return True, data.get("Browser", "unknown")
    except (_urllib_error.URLError, ConnectionRefusedError):
        return False, "Port unreachable — Chrome not running or CDP not enabled"
    except Exception as e:
        return False, str(e)


def connect_playwright(port=9222):
    """连接到已打开的 Chrome，返回 (playwright, browser, context, page)

    使用 Playwright sync_api，适用于 cnki 等同步操作场景
    """
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
    except Exception:
        p.stop()
        raise
    context = browser.contexts[0]
    page = context.new_page()
    return p, browser, context, page


def connect_playwright_async(port=9222):
    """异步版本 connect_over_cdp

    适用于 ieee, sl 等异步操作场景
    """
    from playwright.async_api import async_playwright

    async def _connect():
        p = await async_playwright().start()
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
        except Exception:
            await p.stop()
            raise
        ctx = browser.contexts[0]
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()
        # close extra pages
        for pg in pages[1:]:
            await pg.close()
        return p, browser, page
    return _connect


async def connect_playwright_async_with_timeout(port=9222, timeout=30000):
    """Connect to Chrome CDP with an explicit timeout."""
    connect_fn = connect_playwright_async(port)
    try:
        return await asyncio.wait_for(connect_fn(), timeout=timeout / 1000)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Chrome CDP connection timed out after {timeout}ms")
