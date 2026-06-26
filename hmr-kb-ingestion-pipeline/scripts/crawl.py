#!/usr/bin/env python3
"""
HMR KB Crawl — deterministic link-discovery pre-step.

`ingest.py` ingests *concrete* document URLs (a PDF manual, one support page). But a curated
`targets.txt` often holds *listing* pages instead — a brand's support-portal landing page that
links out to the real documents. This script turns one such seed into the individual document URLs
the ingestion loop needs.

Division of labour (same as the rest of the pipeline):
  * crawl.py DISCOVERS urls. It never downloads document content and never writes metadata.
  * ingest.py FETCHES / extracts / dedups / stamps metadata — unchanged.
  * The human approves the final corpus.

Design: this module is deliberately thin. Every mechanical primitive it needs already lives in
ingest.py (the HTTP client, the brand map, atomic JSON writes, the timestamp helper), so it
*imports* them rather than re-declaring — one user-agent, one brand map, one atomic write, no drift.
crawl.py adds only the discovery-specific logic: link extraction, URL normalization, and the
allow/deny filter chain.

Stdlib-only. No JavaScript rendering — `html.parser` sees server-rendered HTML only. That covers
most support portals; pages that build their link list in the browser are a documented gap.

Usage (writes discovered URLs to <staging>/targets_discovered.txt, never to the curated targets.txt):

    # 1. Discover document links from one listing page (prints JSON, writes nothing):
    python crawl.py discover --config agent_config.json --url "https://brand.example/support/"

    # 2. Persist the discovery into targets_discovered.txt for the ingestion loop:
    python crawl.py append --config agent_config.json --url "https://brand.example/support/"

    # 3. Or treat every URL already in targets.txt as a seed and discover from each:
    python crawl.py batch --config agent_config.json

    # 4. Show what has been crawled so far:
    python crawl.py status --config agent_config.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

# crawl.py sits next to ingest.py in scripts/. Importing it means the user-agent, request timeout,
# polite delay, brand map, atomic-write, timestamp and config/target helpers all have exactly ONE
# definition — the one ingest.py already ships. Nothing to keep "in sync by hand".
import ingest
from ingest import (
    USER_AGENT,
    REQUEST_TIMEOUT,
    FALLBACK_BRAND,
    _now_iso,
    _load_json,
    _atomic_write_json,
    detect_brand,
    http_get,
    load_config,
    read_targets,
)

# ---------------------------------------------------------------------------
# Crawl-specific tunables (the rest are inherited from ingest.py)
# ---------------------------------------------------------------------------

SAME_DOMAIN_ONLY = True          # never follow cross-domain links — almost always correct, and the
                                 # main guard against the crawler wandering off the seeded portal.
MAX_DISCOVERED_PER_SEED = 200    # hard cap on URLs kept from a single seed page

# Only collect links whose path ends in one of these (a recognized document/page type).
ALLOWED_EXTENSIONS = (".pdf", ".html", ".htm", ".md")

# Path segments that mark a likely documentation page, used when a link has no recognized
# extension (e.g. a clean URL like /support/model/SM-S928/).
ALLOWED_PATH_PATTERNS = (
    "/support/", "/manual/", "/manuals/", "/guide/", "/guides/",
    "/docs/", "/documentation/", "/troubleshoot/", "/troubleshooting/",
    "/specs/", "/specifications/", "/datasheet/", "/download/", "/downloads/",
    "/faq/", "/help/", "/how-to/", "/repair/", "/teardown/", "/device/", "/wiki/",
)

# Definitely-not-documentation. Checked BEFORE the allow-list — deny wins.
BLOCKED_PATH_PATTERNS = (
    "/account/", "/login/", "/signup/", "/cart/", "/checkout/",
    "/shop/", "/store/", "/buy/", "/purchase/",
    "/about/", "/contact/", "/careers/", "/press/", "/news/",
    "/privacy/", "/terms/", "/legal/", "/cookies/",
    "/blog/", "/community/", "/forum/", "/social/",
    "/assets/", "/static/", "/images/", "/css/", "/js/",
    "/api/", "/rss/", "/feed/", "/sitemap/",
)

# Query parameters that carry no document identity — stripped during normalization so the same
# page reached via different campaigns collapses to one entry.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid", "msclkid",
})

DISCOVERED_FILENAME = "targets_discovered.txt"


# ---------------------------------------------------------------------------
# Pure helpers (no network — these are what the unit tests exercise)
# ---------------------------------------------------------------------------

def _base_domain(netloc: str) -> str:
    """Hostname without a leading 'www.' and without any port, lowercased."""
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _normalize_url(url: str) -> str:
    """
    Deterministic normalization so the same page reached via different routes collapses to one
    entry: lowercase scheme+host, drop default ports, strip tracking params, sort the rest, drop
    the fragment, and remove a trailing slash from non-root paths.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    # Drop default ports.
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    # Strip tracking params, keep the rest sorted for determinism.
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    query = urllib.parse.urlencode(sorted(kept))

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Fragment is dropped entirely — anchors don't identify a distinct document for our purposes.
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _url_passes_filters(url: str, base_domain: str) -> bool:
    """True if `url` should be kept for ingestion. Fail-fast; deny-list beats allow-list."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    if SAME_DOMAIN_ONLY and _base_domain(parts.netloc) != base_domain:
        return False
    path = parts.path.lower()
    if not path.endswith("/"):
        path_for_match = path + "/"   # so "/support" matches the "/support/" pattern too
    else:
        path_for_match = path
    if any(block in path_for_match for block in BLOCKED_PATH_PATTERNS):
        return False
    if path.split("?")[0].endswith(ALLOWED_EXTENSIONS):
        return True
    if any(allow in path_for_match for allow in ALLOWED_PATH_PATTERNS):
        return True
    return False


class _LinkParser(HTMLParser):
    """Collect every <a href> and honor a <base href> if the page declares one."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list = []
        self.base_href = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "base" and self.base_href is None and d.get("href"):
            self.base_href = d["href"]
        elif tag == "a" and d.get("href"):
            self.hrefs.append(d["href"])


def _discover_simple(html_bytes: bytes, seed_url: str, base_domain: str) -> list:
    """
    Parse an HTML page (stdlib) and return the absolute, normalized, filtered, deduplicated and
    sorted list of document URLs found in it. Deterministic for a given input. May be empty.
    """
    html = html_bytes.decode("utf-8", errors="replace")
    parser = _LinkParser()
    parser.feed(html)

    # Relative links resolve against <base href> if present, else the seed URL.
    base = urllib.parse.urljoin(seed_url, parser.base_href) if parser.base_href else seed_url

    kept = set()
    for href in parser.hrefs:
        href = href.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = _normalize_url(urllib.parse.urljoin(base, href))
        if _url_passes_filters(absolute, base_domain):
            kept.add(absolute)

    return sorted(kept)[:MAX_DISCOVERED_PER_SEED]


def _robots_allows(url: str) -> bool:
    """
    Check the seed host's robots.txt for our user-agent. Fail-OPEN: if robots.txt is missing or
    unreachable we proceed (the operator explicitly seeded this URL). file:// seeds are always
    allowed — robots.txt is a web convention.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return True
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            rp.parse(resp.read().decode("utf-8", errors="replace").splitlines())
    except Exception:
        return True   # no readable robots.txt -> not disallowed
    return rp.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# Crawl state + discovered-targets I/O
# ---------------------------------------------------------------------------

def crawl_state_path(cfg: dict) -> Path:
    return Path(cfg["staging_dir"]) / "crawl_state.json"


def discovered_path(cfg: dict) -> Path:
    """targets_discovered.txt lives next to the curated targets.txt (or a config override)."""
    override = cfg.get("discovered_targets_file")
    if override:
        return Path(override)
    return Path(cfg["targets_file"]).parent / DISCOVERED_FILENAME


def load_crawl_state(cfg: dict) -> dict:
    st = _load_json(crawl_state_path(cfg), {})
    st.setdefault("discovered_urls", {})   # seed_url -> {brand, discovered_at, discovered_count, urls}
    st.setdefault("last_crawl", None)
    return st


def save_crawl_state(cfg: dict, st: dict) -> None:
    _atomic_write_json(crawl_state_path(cfg), st)


def _read_discovered_file(cfg: dict) -> list:
    """URLs already written to targets_discovered.txt (blank/# lines ignored), order preserved."""
    p = discovered_path(cfg)
    if not p.exists():
        return []
    out, seen = [], set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        u = ln.strip()
        if u and not u.startswith("#") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _existing_target_urls(cfg: dict) -> set:
    """
    Everything already known to the pipeline: the curated targets.txt, the discovered file, and
    every URL recorded in crawl_state. Used to skip re-adding URLs on a re-crawl.
    """
    known = set(read_targets(cfg))
    known.update(_read_discovered_file(cfg))
    st = load_crawl_state(cfg)
    for entry in st["discovered_urls"].values():
        known.update(entry.get("urls", []))
    return known


# ---------------------------------------------------------------------------
# Discovery (network)
# ---------------------------------------------------------------------------

def _discover(seed_url: str):
    """
    Fetch + parse one seed page. Returns (discovered_urls, total_links_considered) on success, or
    raises on a fetch/robots problem so the caller can emit the right JSON + exit code.
    """
    if not _robots_allows(seed_url):
        raise PermissionError("disallowed by robots.txt")
    data, _ctype = http_get(seed_url)   # inherits polite delay, user-agent, timeout
    base_domain = _base_domain(urllib.parse.urlsplit(seed_url).netloc)
    discovered = _discover_simple(data, seed_url, base_domain)
    return discovered, data


def _record_discovery(cfg: dict, st: dict, seed_url: str, brand: str, urls: list) -> None:
    st["discovered_urls"][seed_url] = {
        "brand": brand,
        "discovered_at": _now_iso(),
        "discovered_count": len(urls),
        "urls": urls,
    }
    st["last_crawl"] = _now_iso()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_discover(cfg, args):
    seed = args.url
    brand = detect_brand(seed)
    try:
        discovered, _raw = _discover(seed)
    except PermissionError as e:
        print(json.dumps({"status": "blocked_by_robots", "seed_url": seed,
                          "error": str(e), "discovered_urls": []}, ensure_ascii=False))
        return 1
    except urllib.error.HTTPError as e:
        print(json.dumps({"status": "fetch_failed", "seed_url": seed,
                          "error": f"HTTP {e.code}", "discovered_urls": []}, ensure_ascii=False))
        return 1
    except urllib.error.URLError as e:
        print(json.dumps({"status": "fetch_failed", "seed_url": seed,
                          "error": f"urlerror: {e.reason}", "discovered_urls": []}, ensure_ascii=False))
        return 1
    except Exception as e:
        print(json.dumps({"status": "fetch_failed", "seed_url": seed,
                          "error": f"{type(e).__name__}: {e}", "discovered_urls": []}, ensure_ascii=False))
        return 1

    # Drop URLs the pipeline already knows about, so discovered_count reflects only new work.
    known = _existing_target_urls(cfg)
    fresh = [u for u in discovered if u not in known]

    st = load_crawl_state(cfg)
    _record_discovery(cfg, st, seed, brand, fresh)
    save_crawl_state(cfg, st)

    print(json.dumps({
        "status": "ok",
        "seed_url": seed,
        "brand": brand,
        "mode": "simple",
        "discovered_count": len(fresh),
        "already_known": len(discovered) - len(fresh),
        "discovered_urls": fresh,
        "note": "Run 'crawl.py append' to write these into targets_discovered.txt for ingestion.",
    }, ensure_ascii=False))
    return 0


def _write_discovered(cfg, seed_url, urls, output_mode):
    """
    Persist `urls` to targets_discovered.txt. Returns (added, already_present).
    output_mode: 'append'/'replace' both target targets_discovered.txt (the curated targets.txt is
    never touched); 'dry-run' writes nothing.
    """
    present = set(_read_discovered_file(cfg))
    new = [u for u in urls if u not in present]
    if output_mode == "dry-run":
        return new, [u for u in urls if u in present]

    path = discovered_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Crawled from {seed_url} — {_now_iso()} — {len(new)} URLs\n"
    body = "".join(u + "\n" for u in new)
    if output_mode == "replace" or not path.exists():
        path.write_text(header + body, encoding="utf-8")
    else:  # append
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + header + body)
    return new, [u for u in urls if u in present]


def cmd_append(cfg, args):
    seed = args.url
    st = load_crawl_state(cfg)
    entry = st["discovered_urls"].get(seed)
    if entry is None:
        # Nothing cached for this seed — discover first.
        rc = cmd_discover(cfg, args)
        if rc != 0:
            return rc
        st = load_crawl_state(cfg)
        entry = st["discovered_urls"].get(seed, {})
    urls = entry.get("urls", [])

    added, already = _write_discovered(cfg, seed, urls, args.output_mode)
    print(json.dumps({
        "status": "dry_run" if args.output_mode == "dry-run" else "appended",
        "seed_url": seed,
        "output_mode": args.output_mode,
        "discovered_targets_file": str(discovered_path(cfg)),
        "new_urls_added": len(added),
        "urls_already_present": len(already),
        "total_discovered": len(urls),
        "urls": added if args.output_mode == "dry-run" else None,
    }, ensure_ascii=False))
    return 0


def cmd_batch(cfg, args):
    seeds = read_targets(cfg)
    details, processed, failed, total_disc, total_added = [], 0, 0, 0, 0
    st = load_crawl_state(cfg)

    for seed in seeds:
        brand = detect_brand(seed)
        try:
            discovered, _raw = _discover(seed)
        except Exception as e:
            failed += 1
            details.append({"seed_url": seed, "status": "failed", "error": f"{type(e).__name__}: {e}"})
            continue

        known = _existing_target_urls(cfg)
        fresh = [u for u in discovered if u not in known]
        _record_discovery(cfg, st, seed, brand, fresh)
        save_crawl_state(cfg, st)   # crash-safe: persisted after each seed

        added, _already = _write_discovered(cfg, seed, fresh, args.output_mode)
        processed += 1
        total_disc += len(fresh)
        total_added += len(added)
        details.append({"seed_url": seed, "status": "ok",
                        "discovered": len(fresh), "added": len(added)})

    print(json.dumps({
        "status": "batch_complete",
        "seeds_processed": processed,
        "seeds_failed": failed,
        "total_discovered": total_disc,
        "total_added": total_added,
        "output_mode": args.output_mode,
        "details": details,
    }, ensure_ascii=False))
    return 0


def cmd_status(cfg, args):
    st = load_crawl_state(cfg)
    seeds = st["discovered_urls"]
    print(json.dumps({
        "seeds_crawled": len(seeds),
        "total_discovered": sum(e.get("discovered_count", 0) for e in seeds.values()),
        "urls_in_targets": len(read_targets(cfg)),
        "urls_in_discovered_file": len(_read_discovered_file(cfg)),
        "last_crawl": st["last_crawl"],
        "crawled_seeds": [
            {"seed_url": s, "brand": e.get("brand", FALLBACK_BRAND),
             "discovered_at": e.get("discovered_at"), "discovered_count": e.get("discovered_count", 0)}
            for s, e in seeds.items()
        ],
    }, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="HMR KB link discovery — crawl listing pages to find document URLs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("discover", "append", "batch", "status"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="agent_config.json")
        if name in ("discover", "append"):
            sp.add_argument("--url", required=True)
        if name in ("append", "batch"):
            sp.add_argument("--output-mode", dest="output_mode",
                            choices=["append", "replace", "dry-run"], default="replace")

    args = p.parse_args()
    cfg = load_config(Path(args.config))   # exits with a clear message on a bad config

    dispatch = {"discover": cmd_discover, "append": cmd_append,
                "batch": cmd_batch, "status": cmd_status}
    rc = dispatch[args.cmd](cfg, args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
