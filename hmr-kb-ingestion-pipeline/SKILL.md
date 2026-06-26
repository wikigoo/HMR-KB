---
name: hmr-kb-ingestion-pipeline
description: >
  Harvest, convert, deduplicate, and enrich mobile-hardware documentation (PDF manuals and HTML
  support pages) into a staged, metadata-tagged corpus ready for Flowise upload. Use this skill
  whenever the user wants to: download phone/tablet manuals or crawl brand support pages; turn a
  targets.txt list of URLs into a local corpus; generate structured .meta.json files aligned with
  HMR's Five Product Pillars; deduplicate or organize a documentation corpus by brand; or resume a
  previous ingestion run. Trigger on mentions of "HMR ingestion", "knowledge base ingestion",
  "ingestion pipeline", "targets.txt", "download manuals", "crawl support pages", "build/expand the
  HMR knowledge base", or "prepare documents for Flowise" — even when the user does not name the
  skill or file type explicitly.
---

# HMR Knowledge Base Ingestion Pipeline

Harvests mobile-hardware documentation (PDFs + HTML), deduplicates it, and produces structured
HMR metadata — staged for human review before manual upload to Flowise.

**Division of labour:** the bundled `scripts/ingest.py` owns every mechanical step (fetching,
content-type sniffing, PDF text extraction, hashing, dedup, filename sanitization, doc_id
generation, timestamping, and crash-safe state I/O). You — the model — own only the *judgement*
layer: reading the extracted text and filling the title, summary, pillar mapping, tags, and
device model. Do not re-implement the mechanical parts by hand; the script makes every run
deterministic and reproducible.

> **Dependencies:** the script runs on the Python standard library alone, but extraction quality
> is much better with `pypdf` (PDF text) and `trafilatura` (clean HTML→Markdown). If they're
> missing, the script still runs and clearly flags any file it couldn't extract. Suggest
> `pip install pypdf trafilatura` on first run.

---

## First-Run Setup

If `agent_config.json` does not exist in the staging directory, ask the user for:

```
1. Staging directory path (e.g., C:\HMR_Staging  or  ~/HMR_Staging)
2. Path to targets.txt  (default: ./targets.txt)
```

Write `agent_config.json` (the script reads paths from here, so keep them absolute when possible):

```json
{
  "staging_dir": "<user_provided_path>",
  "targets_file": "<user_provided_path>",
  "ready_for_flowise_dir": "<staging_dir>/Ready_For_Flowise"
}
```

`agent_state.json` lives inside `staging_dir` and is created/updated by the script — never
hand-edit it.

---

## Directory Structure

```
<staging_dir>/
├── Corpus/
│   └── <Brand>/
│       ├── samsung_s24_manual_pdf_001.pdf
│       ├── samsung_s24_manual_pdf_001.extracted.txt   ← text for you to read
│       ├── samsung_s24_manual_pdf_001.meta.json       ← stub you complete
│       └── ...   (all three share the doc_id stem)
├── Ready_For_Flowise/        ← approved files moved here by the human admin
└── agent_state.json          ← session progress (script-managed)
agent_config.json             ← staging paths
targets.txt                   ← curated source URLs, one per line (# comments allowed)
targets_discovered.txt        ← crawl.py output (optional); ingest.py reads it too
crawl_state.json              ← crawl progress (script-managed; only if crawl.py is used)
```

---

## Crawl Step (Optional)

If `targets.txt` holds **listing/index pages** (a brand's support-portal landing page) rather than
individual document links, run the crawler first to turn each seed into concrete document URLs.
`scripts/crawl.py` only *discovers* URLs — it never downloads content or writes metadata — and it
writes its results to `targets_discovered.txt` (next to `targets.txt`), leaving your curated file
untouched. `ingest.py` automatically reads **both** files, so discoveries flow straight into the
loop below.

```bash
# Discover document links from one listing page (prints JSON; writes nothing):
python scripts/crawl.py discover --config agent_config.json --url "<listing_page_url>"

# Persist the discovery into targets_discovered.txt for ingestion:
python scripts/crawl.py append --config agent_config.json --url "<listing_page_url>"

# Or crawl every URL already in targets.txt as a seed, in one pass:
python scripts/crawl.py batch --config agent_config.json

# Review what has been crawled so far:
python scripts/crawl.py status --config agent_config.json
```

The crawler is stdlib-only (no extra dependencies), stays on the seed's own domain, respects
`robots.txt`, and reuses `ingest.py`'s User-Agent, timeout, polite delay, and brand map. Discovered
documents are ingested by the normal loop and keep the usual `source_type` of `pdf_manual` or
`crawled_markdown`. It does a single page per seed (no recursion) and does not render JavaScript —
pages that build their link list in the browser are a known gap.

---

## Execution Pipeline

Run the loop below until `next` reports no remaining URLs. Working **one URL at a time** keeps
state crash-safe and lets you fill metadata between fetches. All commands take
`--config agent_config.json`.

### 1. Ask the script for the next URL

```bash
python scripts/ingest.py next --config agent_config.json
```

It returns `{"next_url": "...", "remaining": N}`, deriving the queue live from `targets.txt`
minus already-processed and minus permanently-failed URLs (those that hit the retry cap). The
pending list is never persisted, so editing `targets.txt` between sessions just works.

### 2. Fetch + extract + dedup + save that URL

```bash
python scripts/ingest.py fetch --config agent_config.json --url "<next_url>"
```

The script: detects the brand (falls back to `Misc`), fetches with a polite User-Agent and
timeout, hashes the **bytes before writing anything**, and:

- **`"status": "skipped_duplicate"`** — identical content already in the corpus; nothing written,
  nothing to do. Go to step 4 with `processed`.
- **`"status": "saved"`** — it wrote the content file, a `.extracted.txt`, and a `.meta.json`
  stub. PDFs are text-extracted; HTML is converted to Markdown. Proceed to step 3.
- **`"status": "fetch_failed"`** — go to step 4 with `failed` and the reason.

### 3. Fill the metadata stub (your judgement layer)

Read the `extracted_text_file` from the script's output, then complete the **null/empty** fields
in the `meta_file`. The mechanical fields (`doc_id`, `content_sha256`, `ingested_timestamp`,
`source_url`, `local_file_name`, `source_type`, `brand`) are already final — leave them.

Fields you fill:

| Field | What to write |
|-------|---------------|
| `device_model` | specific model, series, or `General` |
| `ai_clean_title` | clean human-readable title |
| `ai_executive_summary` | three English paragraphs, retrieval-oriented |
| `hmr_target_pillars` | one or more exact pillar keys |
| `semantic_tags` | 6–12 English tags (technical + colloquial) |

See `references/pillars.md` for the pillar table and tagging guidance. If `extracted.txt` is
`EXTRACTION_FAILED`, leave the summary as-is and note it for the human reviewer rather than
inventing content.

### 4. Commit the result to state

```bash
python scripts/ingest.py commit --config agent_config.json --url "<url>" --status processed
# or, on failure:
python scripts/ingest.py commit --config agent_config.json --url "<url>" --status failed --reason "HTTP 404"
```

State is written after **every** URL. Failures accumulate an attempt counter and are abandoned
once they reach the retry cap (default 3), so dead links don't retry forever.

### 5. Validate before handoff

When the queue is empty, gate the corpus:

```bash
python scripts/validate_meta.py --corpus "<staging_dir>/Corpus"
python scripts/ingest.py summary --config agent_config.json
```

Fix any file the validator flags (usually an unfilled stub field or an out-of-range tag count),
then print the summary for the user.

---

## Session Summary

`ingest.py summary` reports processed / pending / failed-open / abandoned counts, the corpus
path, and every failure with its reason and attempt count. Relay this to the user and call out
anything that needs attention (abandoned URLs, extraction failures).

---

## Human-in-the-Loop Handoff

The agent never touches Flowise. After ingestion:

1. Human reviews files under `<staging_dir>/Corpus/<Brand>/` and checks `.meta.json` accuracy.
2. Deletes poorly scraped `.md` files if needed (and their `.meta.json` / `.extracted.txt`).
3. Moves approved files to `<staging_dir>/Ready_For_Flowise/`.
4. Manually uploads to the Flowise dashboard → `HMR-Chatbot-V4` → Document Loader node.

---

## Invocation Examples

```
"run the HMR ingestion agent on targets.txt"
"process these URLs and generate meta files for Flowise"
"resume the last ingestion session"
"download Samsung manuals from targets.txt and build metadata"
```

---

## Notes & Limitations

- **Near-duplicate HTML** (pages whose markup changes byte-to-byte between crawls) won't be
  caught by SHA-256 content hashing — exact duplicates only. Mention this if a user re-crawls.
- **Brand map** lives at the top of `scripts/ingest.py` (`BRAND_MAP`); add hosts there as the
  source list grows. Unmatched hosts land in `Misc/` rather than stalling.
- **Retry cap** and **polite delay** are constants at the top of `scripts/ingest.py` — adjust if
  a source needs gentler treatment.
