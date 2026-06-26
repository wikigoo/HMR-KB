"""
Offline unit tests for crawl.py — no network. Everything here exercises the pure functions
(_discover_simple, _normalize_url, _url_passes_filters) against in-memory HTML or local fixtures.

Run from anywhere:  pytest hmr-kb-ingestion-pipeline/tests/ -q
"""

import sys
from pathlib import Path

# crawl.py and ingest.py live in ../scripts relative to this file.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import crawl  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name):
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _discover_simple against fixtures
# ---------------------------------------------------------------------------

def test_listing_keeps_only_documents():
    """Samsung listing: 2 PDFs + 2 support pages survive; nav/footer/off-domain/dupes are dropped."""
    results = crawl._discover_simple(
        _fixture("samsung_manual_listing.html"),
        "https://www.samsung.com/ca/support/user-manuals-and-guide/",
        "samsung.com",
    )
    assert results == sorted(results)            # deterministic ordering
    assert len(results) == 4
    assert "https://www.samsung.com/ca/downloads/manual-s24-ultra.pdf" in results
    assert "https://www.samsung.com/ca/support/user-manuals-and-guide/downloads/manual-s23.pdf" in results
    assert "https://www.samsung.com/ca/support/model/SM-S928BZKAXAC" in results
    assert "https://www.samsung.com/ca/support/model/SM-S921BZKAXAC" in results
    # none of the noise leaked through
    assert not any("/account/" in u or "/cart" in u or "/blog/" in u or "/privacy/" in u
                   for u in results)
    assert not any("other-cdn.example.com" in u for u in results)


def test_empty_listing_returns_nothing():
    results = crawl._discover_simple(
        _fixture("listing_no_links.html"), "https://example.com/support/", "example.com")
    assert results == []


def test_cross_domain_dropped_when_same_domain_only():
    results = crawl._discover_simple(
        _fixture("listing_cross_domain.html"), "https://example.com/listing.html", "example.com")
    assert len(results) == 2
    assert all("example.com" in u for u in results)
    assert not any("other-site.com" in u or "example.org" in u for u in results)


def test_base_href_used_for_relative_resolution():
    results = crawl._discover_simple(
        _fixture("listing_with_base_tag.html"), "https://example.com/docs/listing.html", "example.com")
    assert set(results) == {
        "https://example.com/docs/v2/manual-a.pdf",
        "https://example.com/docs/v2/guides/manual-b.pdf",
    }


# ---------------------------------------------------------------------------
# _discover_simple against inline HTML (spec §10.1 cases)
# ---------------------------------------------------------------------------

def test_relative_url_resolution():
    html = b'<html><a href="model-x/manual.pdf">Manual</a></html>'
    results = crawl._discover_simple(html, "https://example.com/support/", "example.com")
    assert results == ["https://example.com/support/model-x/manual.pdf"]


def test_blocked_paths_rejected():
    html = (b'<html><a href="/account/settings">Account</a>'
            b'<a href="/support/manual.pdf">Manual</a></html>')
    results = crawl._discover_simple(html, "https://example.com/", "example.com")
    assert results == ["https://example.com/support/manual.pdf"]


def test_duplicate_urls_deduped():
    html = b'<html><a href="/support/m.pdf">M1</a><a href="/support/m.pdf">M2</a></html>'
    results = crawl._discover_simple(html, "https://example.com/", "example.com")
    assert len(results) == 1


def test_tracking_params_stripped_but_real_params_kept():
    html = b'<html><a href="/support/manual.pdf?utm_source=google&ref=nl&id=42">M</a></html>'
    results = crawl._discover_simple(html, "https://example.com/", "example.com")
    assert len(results) == 1
    assert "utm_source" not in results[0]
    assert "ref=" not in results[0]
    assert "id=42" in results[0]


def test_anchor_mailto_javascript_skipped():
    html = (b'<html><a href="#top">top</a><a href="mailto:x@y.com">mail</a>'
            b'<a href="javascript:void(0)">js</a></html>')
    results = crawl._discover_simple(html, "https://example.com/", "example.com")
    assert results == []


# ---------------------------------------------------------------------------
# _normalize_url
# ---------------------------------------------------------------------------

def test_normalize_drops_default_port_and_trailing_slash():
    assert crawl._normalize_url("https://Example.com:443/Support/Page/") \
        == "https://example.com/Support/Page"


def test_normalize_sorts_params_and_drops_fragment():
    out = crawl._normalize_url("https://example.com/x?b=2&a=1#section")
    assert out == "https://example.com/x?a=1&b=2"


def test_normalize_keeps_nondefault_port():
    assert crawl._normalize_url("http://example.com:8080/x") == "http://example.com:8080/x"


# ---------------------------------------------------------------------------
# _url_passes_filters
# ---------------------------------------------------------------------------

def test_non_http_scheme_rejected():
    assert crawl._url_passes_filters("ftp://example.com/manual.pdf", "example.com") is False


def test_extension_accept_and_pattern_accept():
    assert crawl._url_passes_filters("https://example.com/x/manual.pdf", "example.com") is True
    assert crawl._url_passes_filters("https://example.com/support/model/abc", "example.com") is True


def test_unrecognized_path_rejected():
    assert crawl._url_passes_filters("https://example.com/random/thing", "example.com") is False
