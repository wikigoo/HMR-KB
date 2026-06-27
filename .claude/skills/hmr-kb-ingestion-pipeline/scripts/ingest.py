#!/usr/bin/env python3
"""
HMR KB Ingestion — deterministic fetch/extract/dedup/state engine.

This script owns every mechanical step of the pipeline so the model never has to
re-implement HTTP, hashing, sanitization, or state I/O by hand. The model's only job
is the *judgement* layer (title, summary, pillars, tags), which it writes into the
.meta.json stub this script produces.

Design goals that fix the bugs in the original prose-only skill:
  * Dedup happens on the fetched BYTES, before anything is committed to disk, so
    duplicates are never written (no orphaned files).
  * PDFs are text-extracted explicitly; the extracted text is what feeds metadata.
  * `pending` is always derived from targets.txt minus processed — never persisted.
  * Failed URLs carry an attempt counter and are dropped once they hit the retry cap.
  * Real timestamps and deterministic doc_ids are stamped here, not invented by a model.

Usage (one URL at a time keeps state crash-safe and lets the model fill metadata
between fetches):

    # 1. Show the next URL to work on (respects processed/failed/retry-cap):
    python ingest.py next --config agent_config.json

    # 2. Fetch + extract + dedup + save one URL. Prints a JSON result describing
    #    the saved file (or skip/fail) and writes a .meta.json STUB the model fills:
    python ingest.py fetch --config agent_config.json --url "https://..."

    # 3. Mark a result into state after the model has filled the stub:
    python ingest.py commit --config agent_config.json --url "https://..." --status processed
    python ingest.py commit --config agent_config.json --url "https://..." --status failed --reason "HTTP 404"

    # 4. Validate the corpus / print a session summary:
    python ingest.py summary --config agent_config.json

Stdlib-only for fetching/hashing/state. Optional libraries improve extraction quality
and are used when present (a clear message is printed when they're missing):
    pip install pypdf trafilatura
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 30            # seconds
MAX_ATTEMPTS = 3               # a URL failing this many times is abandoned, not retried forever
POLITE_DELAY_SECONDS = 1.0      # courtesy pause between network hits

# Editable brand map. Keys are substrings matched against the URL host.
BRAND_MAP = {
    "samsung": "Samsung",
    "apple": "Apple",
    "xiaomi": "Xiaomi",
    "mi.com": "Xiaomi",
    "huawei": "Huawei",
    "oppo": "Oppo",
    "vivo": "Vivo",
    "oneplus": "OnePlus",
    "google": "Google",
    "motorola": "Motorola",
    "nokia": "Nokia",
    "sony": "Sony",
    "realme": "Realme",
    "nothing": "Nothing",
}
FALLBACK_BRAND = "Misc"        # anything unmatched lands here instead of stalling


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Real UTC timestamp — the model must never invent this."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON via a temp file + replace so a crash mid-write can't corrupt state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sanitize(name: str, max_len: int = 80) -> str:
    """
    Deterministic, cross-OS-safe slug. Same input -> same output on every machine,
    so two runs never disagree on a filename. Lowercase, ASCII-ish, no reserved chars.
    """
    name = name.strip().lower()
    name = re.sub(r"https?://", "", name)
    name = re.sub(r"[^a-z0-9]+", "_", name)   # collapse everything non-alnum to _
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "doc"
    return name[:max_len].rstrip("_")


def detect_brand(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    for needle, brand in BRAND_MAP.items():
        if needle in host:
            return brand
    return FALLBACK_BRAND


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def existing_hashes(brand_dir: Path) -> dict:
    """Map content_sha256 -> meta filename for every meta already in this brand folder."""
    seen = {}
    if not brand_dir.exists():
        return seen
    for meta in brand_dir.glob("*.meta.json"):
        data = _load_json(meta, {})
        h = data.get("content_sha256")
        if h:
            seen[h] = meta.name
    return seen


def used_doc_ids(corpus_dir: Path) -> set:
    ids = set()
    if not corpus_dir.exists():
        return ids
    for meta in corpus_dir.rglob("*.meta.json"):
        data = _load_json(meta, {})
        if data.get("doc_id"):
            ids.add(data["doc_id"])
    return ids


def make_doc_id(brand: str, base_slug: str, corpus_dir: Path) -> str:
    """Deterministic, collision-checked: <brand>_<slug>_NNN, bumping NNN until free."""
    taken = used_doc_ids(corpus_dir)
    stem = f"{brand.lower()}_{base_slug}"
    n = 1
    while True:
        candidate = f"{stem}_{n:03d}"
        if candidate not in taken:
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict:
    cfg = _load_json(config_path, None)
    if cfg is None:
        sys.exit(f"ERROR: config not found or invalid: {config_path}\n"
                 f"Run first-run setup to create agent_config.json (see SKILL.md).")
    cfg.setdefault("staging_dir", ".")
    cfg.setdefault("targets_file", "./targets.txt")
    cfg.setdefault("ready_for_flowise_dir", str(Path(cfg["staging_dir"]) / "Ready_For_Flowise"))
    return cfg


def state_path(cfg: dict) -> Path:
    return Path(cfg["staging_dir"]) / "agent_state.json"


def load_state(cfg: dict) -> dict:
    st = _load_json(state_path(cfg), {})
    st.setdefault("processed_urls", [])     # list[str]
    st.setdefault("failed_urls", [])        # list[{url, reason, attempts}]
    # NOTE: pending is intentionally NOT stored — always derived from targets.txt.
    return st


def save_state(cfg: dict, st: dict) -> None:
    _atomic_write_json(state_path(cfg), st)


def read_targets(cfg: dict) -> list:
    p = Path(cfg["targets_file"])
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out, seen = [], set()
    for ln in lines:
        u = ln.strip()
        if u and not u.startswith("#") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def failed_lookup(st: dict, url: str) -> Optional[dict]:
    for f in st["failed_urls"]:
        if f.get("url") == url:
            return f
    return None


def pending_urls(cfg: dict, st: dict) -> list:
    """Derived live: targets minus processed minus permanently-failed (cap reached)."""
    processed = set(st["processed_urls"])
    abandoned = {f["url"] for f in st["failed_urls"] if f.get("attempts", 0) >= MAX_ATTEMPTS}
    return [u for u in read_targets(cfg) if u not in processed and u not in abandoned]


# ---------------------------------------------------------------------------
# Fetching + extraction
# ---------------------------------------------------------------------------

def http_get(url: str):
    """Return (bytes, content_type). Raises urllib errors to the caller."""
    # Courtesy pause before hitting a remote host, so a long targets.txt doesn't
    # hammer a site. Skipped for local/file:// sources where it would only add latency.
    if url.lower().startswith(("http://", "https://")):
        time.sleep(POLITE_DELAY_SECONDS)
    # Try curl_cffi first (impersonates Chrome TLS fingerprint), fall back to urllib
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(url, headers={"User-Agent": USER_AGENT},
                                 timeout=REQUEST_TIMEOUT, impersonate="chrome131")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        return resp.content, ctype
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        return resp.read(), ctype


def is_pdf(url: str, ctype: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf") or "application/pdf" in ctype


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from an already-saved PDF via pypdf; signal EXTRACTION_FAILED if unavailable."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return "EXTRACTION_FAILED"
    try:
        reader = PdfReader(str(pdf_path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text.strip() or "EXTRACTION_FAILED"
    except Exception:
        return "EXTRACTION_FAILED"


def html_to_markdown(data: bytes, url: str) -> str:
    """Prefer trafilatura (clean article extraction); fall back to a crude tag strip."""
    html = data.decode("utf-8", errors="replace")
    try:
        import trafilatura  # type: ignore
        extracted = trafilatura.extract(html, url=url, output_format="markdown",
                                        include_comments=False, include_tables=True)
        if extracted and extracted.strip():
            return extracted.strip()
    except ImportError:
        pass
    except Exception:
        pass
    # crude fallback — strip scripts/styles/tags. Better than nothing; flagged downstream.
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+\n", "\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_next(cfg, st):
    pend = pending_urls(cfg, st)
    print(json.dumps({"next_url": pend[0] if pend else None,
                      "remaining": len(pend)}, ensure_ascii=False))


def cmd_fetch(cfg, st, url):
    brand = detect_brand(url)
    corpus = Path(cfg["staging_dir"]) / "Corpus"
    brand_dir = corpus / brand
    brand_dir.mkdir(parents=True, exist_ok=True)

    try:
        data, ctype = http_get(url)
    except urllib.error.HTTPError as e:
        return _emit_fail(url, f"HTTP {e.code}")
    except urllib.error.URLError as e:
        return _emit_fail(url, f"urlerror: {e.reason}")
    except Exception as e:  # timeout etc.
        return _emit_fail(url, f"{type(e).__name__}: {e}")

    if not data:
        return _emit_fail(url, "empty_content")

    # --- DEDUP ON BYTES, BEFORE WRITING ANYTHING (the key fix) ---
    digest = sha256_bytes(data)
    known = existing_hashes(brand_dir)
    if digest in known:
        print(json.dumps({"status": "skipped_duplicate", "url": url,
                          "brand": brand, "duplicate_of": known[digest]},
                         ensure_ascii=False))
        return

    pdf = is_pdf(url, ctype)
    base_slug = sanitize(url.rsplit("/", 1)[-1] or url)
    # doc_id is unique by construction, so using it as the filename stem guarantees
    # two different docs that share a basename (e.g. two "manual.pdf") can't overwrite
    # each other, and ties the content file, extracted text, and meta together.
    doc_id = make_doc_id(brand, base_slug, corpus)

    if pdf:
        out_path = brand_dir / f"{doc_id}.pdf"
        out_path.write_bytes(data)
        text = extract_pdf_text(out_path)
        source_type = "pdf_manual"
    else:
        md = html_to_markdown(data, url)
        out_path = brand_dir / f"{doc_id}.md"
        out_path.write_text(md, encoding="utf-8")
        text = md
        source_type = "crawled_markdown"

    # Write a metadata STUB the model fills in. Mechanical fields are final; the
    # null fields are the model's judgement layer.
    text_path = brand_dir / f"{doc_id}.extracted.txt"
    text_path.write_text(text, encoding="utf-8")

    meta = {
        "doc_id": doc_id,
        "brand": brand,
        "device_model": None,                 # model fills
        "source_type": source_type,
        "source_url": url,
        "local_file_name": out_path.name,
        "ingested_timestamp": _now_iso(),
        "content_sha256": digest,
        "hmr_target_pillars": [],             # model fills (see references/pillars.md)
        "ai_clean_title": None,               # model fills
        "ai_executive_summary": ("EXTRACTION_FAILED" if text == "EXTRACTION_FAILED" else None),
        "semantic_tags": []                   # model fills: 6-12 English terms
    }
    meta_path = brand_dir / f"{doc_id}.meta.json"
    _atomic_write_json(meta_path, meta)

    print(json.dumps({
        "status": "saved",
        "url": url,
        "brand": brand,
        "saved_file": str(out_path),
        "meta_file": str(meta_path),
        "extracted_text_file": str(text_path),
        "source_type": source_type,
        "extraction_ok": text != "EXTRACTION_FAILED",
        "note": "Model: read extracted_text_file, then fill the null fields in meta_file."
    }, ensure_ascii=False))


def _emit_fail(url, reason):
    print(json.dumps({"status": "fetch_failed", "url": url, "reason": reason},
                     ensure_ascii=False))


def cmd_commit(cfg, st, url, status, reason):
    if status == "processed":
        if url not in st["processed_urls"]:
            st["processed_urls"].append(url)
        st["failed_urls"] = [f for f in st["failed_urls"] if f.get("url") != url]
    elif status == "failed":
        rec = failed_lookup(st, url)
        if rec:
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["reason"] = reason or rec.get("reason", "unknown")
        else:
            st["failed_urls"].append({"url": url, "reason": reason or "unknown", "attempts": 1})
    else:
        sys.exit(f"ERROR: unknown status '{status}' (use processed|failed)")
    save_state(cfg, st)   # written after EVERY url — crash-safe
    rec = failed_lookup(st, url)
    print(json.dumps({"committed": status, "url": url,
                      "attempts": rec.get("attempts") if rec else None,
                      "abandoned": bool(rec and rec["attempts"] >= MAX_ATTEMPTS)},
                     ensure_ascii=False))


def cmd_summary(cfg, st):
    pend = pending_urls(cfg, st)
    abandoned = [f for f in st["failed_urls"] if f.get("attempts", 0) >= MAX_ATTEMPTS]
    print("=== HMR Ingestion Summary ===")
    print(f"  Processed     : {len(st['processed_urls'])}")
    print(f"  Pending       : {len(pend)}")
    print(f"  Failed (open) : {len([f for f in st['failed_urls'] if f.get('attempts',0) < MAX_ATTEMPTS])}")
    print(f"  Abandoned     : {len(abandoned)} (hit retry cap {MAX_ATTEMPTS})")
    print(f"  Corpus        : {Path(cfg['staging_dir']) / 'Corpus'}")
    if st["failed_urls"]:
        print("  --- failures ---")
        for f in st["failed_urls"]:
            print(f"    [{f.get('attempts',0)}x] {f['url']}  ({f.get('reason')})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="HMR KB ingestion engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("next", "fetch", "commit", "summary"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="agent_config.json")
        if name == "fetch":
            sp.add_argument("--url", required=True)
        if name == "commit":
            sp.add_argument("--url", required=True)
            sp.add_argument("--status", required=True, choices=["processed", "failed"])
            sp.add_argument("--reason", default="")

    args = p.parse_args()
    cfg = load_config(Path(args.config))
    st = load_state(cfg)

    if args.cmd == "next":
        cmd_next(cfg, st)
    elif args.cmd == "fetch":
        cmd_fetch(cfg, st, args.url)
    elif args.cmd == "commit":
        cmd_commit(cfg, st, args.url, args.status, args.reason)
    elif args.cmd == "summary":
        cmd_summary(cfg, st)


if __name__ == "__main__":
    main()
