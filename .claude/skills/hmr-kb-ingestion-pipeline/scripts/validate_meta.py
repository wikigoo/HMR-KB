#!/usr/bin/env python3
"""
Validate HMR .meta.json files before the human-review handoff.

A stub written by ingest.py has several null/empty fields that the model is supposed
to fill (title, summary, pillars, tags, device_model). This gate catches:
  * malformed JSON,
  * unfilled stub fields (the model forgot a file),
  * invalid pillar names,
  * tag counts outside the 6-12 range,
  * mismatch between the meta and the file it claims to describe.

Run it on a single file or recursively over the Corpus. Exit code is non-zero if any
file fails, so it can gate a script/CI step.

    python validate_meta.py path/to/file.meta.json
    python validate_meta.py --corpus /path/to/Corpus
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_PILLARS = {
    "1_new_phone_buying_guide",
    "2_used_phone_fraud_detection",
    "3_hardware_troubleshooting",
    "4_hardware_education",
    "5_accessories_guidance",
}

REQUIRED_KEYS = {
    "doc_id", "brand", "device_model", "source_type", "source_url",
    "local_file_name", "ingested_timestamp", "content_sha256",
    "hmr_target_pillars", "ai_clean_title", "ai_executive_summary", "semantic_tags",
}

VALID_SOURCE_TYPES = {"pdf_manual", "crawled_markdown"}


def validate(meta_path: Path) -> list:
    """Return a list of error strings (empty means valid)."""
    errors = []
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"invalid JSON: {e}"]
    except OSError as e:
        return [f"cannot read: {e}"]

    missing = REQUIRED_KEYS - data.keys()
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")

    # Unfilled stub fields — these must be completed by the model.
    for k in ("device_model", "ai_clean_title"):
        if not data.get(k):
            errors.append(f"'{k}' is still empty (model must fill it)")

    summary = data.get("ai_executive_summary")
    if not summary:
        errors.append("'ai_executive_summary' is empty (model must fill it)")
    elif summary == "EXTRACTION_FAILED":
        errors.append("'ai_executive_summary' == EXTRACTION_FAILED (review/replace source)")

    st = data.get("source_type")
    if st not in VALID_SOURCE_TYPES:
        errors.append(f"source_type '{st}' not in {sorted(VALID_SOURCE_TYPES)}")

    pillars = data.get("hmr_target_pillars") or []
    if not pillars:
        errors.append("hmr_target_pillars is empty (assign at least one)")
    bad = [p for p in pillars if p not in VALID_PILLARS]
    if bad:
        errors.append(f"invalid pillar(s): {bad}")

    tags = data.get("semantic_tags") or []
    if not (6 <= len(tags) <= 12):
        errors.append(f"semantic_tags count {len(tags)} outside required 6-12")

    # The described file should sit next to the meta.
    local = data.get("local_file_name")
    if local and not (meta_path.parent / local).exists():
        errors.append(f"local_file_name '{local}' not found beside meta")

    return errors


def main():
    ap = argparse.ArgumentParser(description="Validate HMR .meta.json files")
    ap.add_argument("path", nargs="?", help="a single .meta.json file")
    ap.add_argument("--corpus", help="validate every .meta.json under this dir")
    args = ap.parse_args()

    targets = []
    if args.corpus:
        targets = sorted(Path(args.corpus).rglob("*.meta.json"))
    elif args.path:
        targets = [Path(args.path)]
    else:
        ap.error("provide a file path or --corpus DIR")

    if not targets:
        print("No .meta.json files found.")
        return

    failed = 0
    for t in targets:
        errs = validate(t)
        if errs:
            failed += 1
            print(f"FAIL  {t}")
            for e in errs:
                print(f"        - {e}")
        else:
            print(f"OK    {t}")

    print(f"\n{len(targets) - failed}/{len(targets)} valid.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
