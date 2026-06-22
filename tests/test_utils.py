"""Tests for download skill shared utilities."""
import os
import sys
import tempfile
import pytest

# Ensure scripts/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

from utils import parse_standard_args, safe_filename, validate_pdf, extract_year, clean_doi, get_scihub_domains, check_chrome_cdp


class TestParseStandardArgs:
    """Test the unified pipe argument parser."""

    def test_basic_keyword_only(self):
        defaults = {"keyword": "", "start_year": None, "end_year": None, "count": 5, "output_dir": "./out"}
        result = parse_standard_args("machine learning", defaults)
        assert result["keyword"] == "machine learning"
        assert result["count"] == 5

    def test_keyword_with_year_range(self):
        defaults = {"keyword": "", "start_year": None, "end_year": None, "count": 5, "output_dir": "./out"}
        result = parse_standard_args("deep learning | 2024 2026 | 10 | ./papers", defaults)
        assert result["keyword"] == "deep learning"
        assert result["start_year"] == 2024
        assert result["end_year"] == 2026
        assert result["count"] == 10
        assert result["output_dir"] == "./papers"

    def test_with_sort_key(self):
        defaults = {"keyword": "", "start_year": None, "end_year": None, "sort_by": "relevance", "count": 5, "output_dir": "./out"}
        result = parse_standard_args("transformer | 2024 2026 | citations | 10 | ./papers", defaults)
        assert result["sort_by"] == "citations"
        assert result["count"] == 10

    def test_with_field_key(self):
        defaults = {"keyword": "", "start_year": None, "end_year": None, "field": "", "count": 5, "output_dir": "./out"}
        result = parse_standard_args("deep learning | 2024 2026 | Computer Science | 5", defaults)
        assert "Computer Science" in result.get("field", "")

    def test_with_key_value_options(self):
        defaults = {"keyword": "", "start_year": None, "end_year": None, "count": 5, "output_dir": "./out", "sort_by": "relevance"}
        result = parse_standard_args("test | 2024 2026 | 10 | ./papers | sort=cited | field=CS", defaults,
                                     option_aliases={"sort": "sort_by"})
        assert result["sort_by"] == "cited"
        assert result["field"] == "CS"

    def test_empty_args(self):
        defaults = {"keyword": "", "start_year": None, "end_year": None, "count": 5, "output_dir": "./out"}
        result = parse_standard_args("", defaults)
        assert result == defaults


class TestSafeFilename:
    def test_basic_sanitize(self):
        assert safe_filename("Hello World", 80) == "Hello World"

    def test_remove_special_chars(self):
        assert safe_filename("a/b:c*d?e", 80) == "a_b_c_d_e"

    def test_truncate_long(self):
        long_name = "a" * 100
        assert len(safe_filename(long_name, 50)) == 50

    def test_empty_fallback(self):
        assert safe_filename("", 80) == "paper"


class TestValidatePdf:
    def test_valid_pdf(self, tmpdir):
        path = os.path.join(tmpdir, "test.pdf")
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\nsome content\n")
        ok, _ = validate_pdf(path, min_bytes=1)
        assert ok is True

    def test_not_a_pdf(self, tmpdir):
        path = os.path.join(tmpdir, "not.pdf")
        with open(path, "wb") as f:
            f.write(b"<html>not a pdf</html>")
        ok, msg = validate_pdf(path, min_bytes=1)
        assert ok is False
        assert "not a PDF" in msg

    def test_too_small(self, tmpdir):
        path = os.path.join(tmpdir, "small.pdf")
        with open(path, "wb") as f:
            f.write(b"tiny")
        ok, msg = validate_pdf(path, min_bytes=1000)
        assert ok is False
        assert "too small" in msg

    def test_file_not_found(self):
        ok, msg = validate_pdf("/nonexistent/path.pdf")
        assert ok is False
        assert "not found" in msg


class TestExtractYear:
    def test_valid_year(self):
        assert extract_year("2024-01-15") == 2024

    def test_no_year(self):
        assert extract_year("") == 0
        assert extract_year("no digits") == 0


class TestCleanDoi:
    def test_full_url(self):
        assert clean_doi("https://doi.org/10.1109/ACCESS.2023.3312345") == "10.1109/ACCESS.2023.3312345"

    def test_bare_doi(self):
        assert clean_doi("10.1234/abc.2023.001") == "10.1234/abc.2023.001"

    def test_empty(self):
        assert clean_doi("") == ""


class TestGetScihubDomains:
    def test_returns_list(self):
        domains = get_scihub_domains()
        assert isinstance(domains, list)
        assert len(domains) > 0
        for d in domains:
            assert d.startswith("https://")


class TestCheckChromeCdp:
    def test_returns_tuple(self):
        ok, info = check_chrome_cdp(port=19999)  # unlikely to be running
        assert ok is False
        assert isinstance(info, str)
