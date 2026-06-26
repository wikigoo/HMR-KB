# HMR Knowledge Base Ingestion Pipeline

A [Claude](https://claude.com/claude-code) **Skill** that turns a list of URLs into a clean,
deduplicated, metadata-tagged documentation corpus — ready for human review and manual upload to
[Flowise](https://flowiseai.com/).

It harvests mobile-hardware documentation (PDF manuals and HTML support pages), converts it to
clean text/Markdown, removes duplicates, and generates structured `.meta.json` files aligned with
HMR's **Five Product Pillars**. A human reviews the result before anything is uploaded to the
`HMR-Chatbot-V4` knowledge base.

---

## Why this exists

Building a retrieval knowledge base from scattered manufacturer manuals and support pages is
repetitive, error-prone work: finding the document links, downloading files, stripping page chrome,
avoiding duplicates, naming things consistently, and tagging each document so it can be retrieved
later. This skill automates the mechanical parts deterministically and reserves the **judgement**
parts (summaries, topic tagging) for the language model — then hands a clean corpus to a human for
final approval.

### Design principle: deterministic engine + model judgement

The work is split into layers so every run is reproducible. Two small, decoupled engines own all the
mechanical work; the model owns only judgement; a human owns approval.

| Layer | Owner | Responsibilities |
|-------|-------|------------------|
| **Discovery** *(optional)* | `scripts/crawl.py` | crawl a listing page, find document URLs, normalize + filter them, write `targets_discovered.txt` |
| **Ingestion** | `scripts/ingest.py` | fetch, content-type sniff, PDF text extraction, HTML→Markdown, SHA-256 dedup, filename sanitization, `doc_id` generation, real timestamps, crash-safe state |
| **Judgement** | Claude | clean title, executive summary, pillar mapping, semantic tags, device model |
| **Approval** | Human | review accuracy, drop bad scrapes, move approved files, upload to Flowise |

The model never re-implements HTTP, hashing, or state handling by hand, so two runs of the same
input produce the same files and IDs. `crawl.py` is a thin module that **imports** its User-Agent,
timeouts, polite delay, and brand map from `ingest.py` — one source of truth, nothing to keep in
sync by hand.

---

## How a run flows

```
targets.txt (curated seeds — listing pages and/or direct document links)
      │
      │  ── optional discovery step ──
      ▼
  crawl.py  discover → append        ──►  targets_discovered.txt
      │                                          │
      └──────────────┬───────────────────────────┘
                     ▼
            ingest.py reads BOTH files
                     │
        next ─► fetch ─► (model fills metadata) ─► commit       (loop, one URL at a time)
                     │
                     ▼
            validate_meta.py  ─►  human review  ─►  Ready_For_Flowise/  ─►  manual upload
```

If your `targets.txt` already contains direct document links, skip the discovery step entirely — the
crawler is only there to expand *listing* pages into the individual documents they link to.

---

## Features

- **Optional link discovery** — `scripts/crawl.py` turns a *listing* page (a brand's support-portal
  landing page) into individual document URLs. It stays on the seed's own domain, respects
  `robots.txt`, normalizes URLs deterministically, and writes to `targets_discovered.txt` so your
  curated `targets.txt` is never touched. Stdlib-only.
- **Resumable** — progress is tracked in `agent_state.json`, written after *every* URL, so an
  interrupted run continues cleanly.
- **Content-hash deduplication** — SHA-256 is computed on the fetched **bytes before anything is
  written**, so duplicate content is never saved to disk (no orphaned files).
- **Explicit PDF text extraction** — PDFs are extracted to text that feeds metadata generation;
  failures are flagged, never silently skipped.
- **Collision-free naming** — every artifact (content file, extracted text, metadata) shares a
  unique `doc_id` stem, so two documents that share a basename can't overwrite each other.
- **Retry cap** — failed URLs accumulate an attempt counter and are abandoned after 3 tries, so
  dead links don't retry forever.
- **Brand organization with fallback** — files are foldered by detected brand; anything unmatched
  lands in `Misc/` instead of stalling.
- **Polite fetching** — descriptive User-Agent, request timeout, and a courtesy delay between
  remote requests, shared across both engines.
- **Schema validation** — `scripts/validate_meta.py` gates the corpus before the human handoff.
- **Human-in-the-loop** — the pipeline never touches Flowise; a person approves every file.

---

## The Five Product Pillars

Each document is tagged with one or more pillars (see
[`hmr-kb-ingestion-pipeline/references/pillars.md`](hmr-kb-ingestion-pipeline/references/pillars.md)
for the full mapping guide):

| Pillar key | Covers |
|------------|--------|
| `1_new_phone_buying_guide` | specs comparison, purchase advice, choosing a new device |
| `2_used_phone_fraud_detection` | counterfeit detection, used-phone inspection, IMEI/serial checks |
| `3_hardware_troubleshooting` | fault diagnosis, repair steps, error codes, hardware issues |
| `4_hardware_education` | how components work, teardowns, technical education |
| `5_accessories_guidance` | chargers, cables, cases, compatibility, accessories |

---

## Installation

### As a Claude Skill

Copy the `hmr-kb-ingestion-pipeline/` folder into your Claude skills directory, or package it as a
`.skill` archive and install it. A pre-built archive is included at the repository root:
`hmr-kb-ingestion-pipeline.skill`.

Once installed, Claude consults the skill automatically when you ask it to ingest manuals, crawl a
support page, process a `targets.txt`, or build the HMR knowledge base.

### Dependencies

The scripts run on the **Python 3.8+ standard library** alone — including the crawler, which uses
only `html.parser` and `urllib`. Two optional packages dramatically improve *extraction* quality
(not discovery) and are used automatically when present:

```bash
pip install pypdf trafilatura
```

- `pypdf` — extracts text from PDF manuals.
- `trafilatura` — extracts clean article content from HTML (strips nav, ads, cookie banners).

Without them the pipeline still runs: PDFs are flagged `EXTRACTION_FAILED` for the reviewer, and
HTML falls back to a crude tag-strip. Running the test suite additionally needs `pytest`
(dev-only).

---

## Usage

### 1. First-run setup

On first use the skill asks for a staging directory and the path to your `targets.txt`, then writes
`agent_config.json`:

```json
{
  "staging_dir": "C:/HMR_Staging",
  "targets_file": "C:/HMR_Staging/targets.txt",
  "ready_for_flowise_dir": "C:/HMR_Staging/Ready_For_Flowise"
}
```

Populate `targets.txt` with one URL per line (see [`examples/targets.txt`](examples/targets.txt)).
Lines beginning with `#` are ignored.

### 2. (Optional) Discover document URLs from listing pages

If a target is a *listing* page rather than a single document, discover its document links first.
`crawl.py` discovers URLs only — it never downloads content or writes metadata — and it writes its
results to `targets_discovered.txt` next to `targets.txt`. `ingest.py` reads **both** files, so
discoveries flow straight into the loop in step 3.

```bash
# Discover document links from one listing page (prints JSON; writes nothing)
python hmr-kb-ingestion-pipeline/scripts/crawl.py discover --config agent_config.json --url "https://brand.example/support/"

# Persist the discovery into targets_discovered.txt
python hmr-kb-ingestion-pipeline/scripts/crawl.py append   --config agent_config.json --url "https://brand.example/support/"

# Or treat every URL already in targets.txt as a seed and discover from each, in one pass
python hmr-kb-ingestion-pipeline/scripts/crawl.py batch    --config agent_config.json

# Review what has been crawled so far
python hmr-kb-ingestion-pipeline/scripts/crawl.py status   --config agent_config.json
```

`append` and `batch` take `--output-mode {append,replace,dry-run}` (default `replace`, which
rewrites `targets_discovered.txt`; `dry-run` prints what would be added without writing). The
crawler stays on the seed's domain, respects `robots.txt`, and visits a single page per seed (no
recursion, no JavaScript rendering — pages that build their link list in the browser are a known
gap).

### 3. Run the ingestion loop

Claude drives the loop below, one URL at a time. You can also run the engine directly:

```bash
# Show the next URL to process (derived live from targets.txt + targets_discovered.txt, minus done/abandoned)
python hmr-kb-ingestion-pipeline/scripts/ingest.py next    --config agent_config.json

# Fetch + extract + dedup + save one URL (writes a .meta.json stub)
python hmr-kb-ingestion-pipeline/scripts/ingest.py fetch   --config agent_config.json --url "https://..."

# Record the outcome in state (crash-safe, written immediately)
python hmr-kb-ingestion-pipeline/scripts/ingest.py commit  --config agent_config.json --url "https://..." --status processed
python hmr-kb-ingestion-pipeline/scripts/ingest.py commit  --config agent_config.json --url "https://..." --status failed --reason "HTTP 404"

# Print a session summary
python hmr-kb-ingestion-pipeline/scripts/ingest.py summary  --config agent_config.json
```

Between `fetch` and `commit`, Claude reads the generated `*.extracted.txt` and fills the metadata
stub (title, summary, pillars, tags, device model).

### 4. Validate before handoff

```bash
python hmr-kb-ingestion-pipeline/scripts/validate_meta.py --corpus "C:/HMR_Staging/Corpus"
```

The validator checks JSON validity, that every model-filled field is complete, that pillar keys are
valid, that the tag count is 6–12, and that the described content file exists. It exits non-zero if
anything fails, so it can gate a CI step.

---

## The two targets files

| File | Maintained by | Purpose |
|------|---------------|---------|
| `targets.txt` | **You** (curated) | the source list — seeds and/or direct document links. The crawler never edits it. |
| `targets_discovered.txt` | `crawl.py` (generated) | URLs the crawler found from your seeds, with `# Crawled from <seed>` headers. Safe to delete or regenerate. |

`ingest.py` reads both and de-duplicates across them, so a URL present in both is processed once.
Keeping them separate means your hand-curated list stays clean while machine discoveries remain
fully reproducible and disposable.

---

## Output layout

```
<staging_dir>/
├── Corpus/
│   └── <Brand>/
│       ├── samsung_s24_manual_pdf_001.pdf            # original content
│       ├── samsung_s24_manual_pdf_001.extracted.txt  # text for the model to read
│       └── samsung_s24_manual_pdf_001.meta.json      # structured metadata
├── Ready_For_Flowise/        # files a human has approved
├── agent_state.json          # ingestion progress (script-managed)
├── crawl_state.json          # discovery progress (only if crawl.py is used)
└── targets_discovered.txt    # crawled URLs (only if crawl.py is used)
```

### Metadata schema (`.meta.json`)

```json
{
  "doc_id": "samsung_galaxy_s24_ultra_manual_001",
  "brand": "Samsung",
  "device_model": "Galaxy S24 Ultra",
  "source_type": "pdf_manual",
  "source_url": "https://...",
  "local_file_name": "samsung_galaxy_s24_ultra_manual_001.pdf",
  "ingested_timestamp": "2026-06-26T12:00:00Z",
  "content_sha256": "<hash>",
  "hmr_target_pillars": ["3_hardware_troubleshooting"],
  "ai_clean_title": "Samsung Galaxy S24 Ultra User Guide",
  "ai_executive_summary": "Three-paragraph conceptual summary in English.",
  "semantic_tags": [
    "OLED burn-in", "screen ghosting", "battery health",
    "phone won't charge", "overheating", "fake charger"
  ]
}
```

Mechanical fields (`doc_id`, `content_sha256`, `ingested_timestamp`, `source_*`, `local_file_name`,
`source_type`, `brand`) are produced by the engine. The remaining fields are completed by the model.

---

## Human-in-the-loop handoff

The pipeline never uploads anything. After ingestion a person:

1. Reviews files under `Corpus/<Brand>/` and checks `.meta.json` accuracy.
2. Deletes poorly scraped files (and their companion `.meta.json` / `.extracted.txt`).
3. Moves approved files to `Ready_For_Flowise/`.
4. Manually uploads them in the Flowise dashboard → `HMR-Chatbot-V4` → Document Loader node.

---

## Configuration

Tunable constants live at the top of each engine.

**`scripts/ingest.py`** (shared — `crawl.py` imports several of these):

| Constant | Default | Purpose |
|----------|---------|---------|
| `MAX_ATTEMPTS` | `3` | retries before a failing URL is abandoned |
| `POLITE_DELAY_SECONDS` | `1.0` | courtesy pause between remote requests |
| `REQUEST_TIMEOUT` | `30` | per-request timeout (seconds) |
| `BRAND_MAP` | — | host substring → brand name; extend as your source list grows |
| `FALLBACK_BRAND` | `Misc` | folder for unmatched hosts |

**`scripts/crawl.py`** (discovery-only):

| Constant | Default | Purpose |
|----------|---------|---------|
| `SAME_DOMAIN_ONLY` | `True` | never follow links off the seed's host |
| `MAX_DISCOVERED_PER_SEED` | `200` | hard cap on URLs kept from one seed page |
| `ALLOWED_PATH_PATTERNS` | `/support/`, `/manual/`, … | path segments that mark a documentation page |
| `BLOCKED_PATH_PATTERNS` | `/cart/`, `/login/`, … | path segments to reject (deny wins over allow) |

---

## Testing

`crawl.py`'s discovery core is pure (no network), so its tests run fully offline against local HTML
fixtures:

```bash
pytest hmr-kb-ingestion-pipeline/tests/ -q
```

For an end-to-end wiring check, serve the fixtures over `python -m http.server` and run
`discover → append → ingest.py next` (a `file://` seed can't exercise the same-domain link filter,
so use localhost HTTP). The ingestion engine itself is exercised with `file://` URLs in a throwaway
staging directory — see [`CLAUDE.md`](CLAUDE.md) for the invariants to check.

---

## Limitations

- **Exact-duplicate detection only.** SHA-256 catches byte-identical content. Pages whose markup
  changes between crawls (timestamps, dynamic blocks) won't be recognized as duplicates.
- **Extraction quality depends on optional libraries.** Install `pypdf` and `trafilatura` for best
  results; without them some documents are flagged for manual handling.
- **The crawler is intentionally shallow.** Single page per seed, same-domain only, no JavaScript
  rendering. It is a curated-seed expander, *not* a broad web crawler.
- **robots.txt: discovery respects it, ingestion does not.** `crawl.py` checks `robots.txt` before
  fetching a seed (failing open only when it's unreachable); `ingest.py` fetches exactly the URLs you
  curate. Be sure you have the right to download and store the documents you list.

---

## Repository structure

```
HMR-Knowledge-Base-Ingestion-Pipeline/
├── README.md
├── CLAUDE.md                       # guidance for Claude working in this repo
├── LICENSE
├── .gitignore
├── examples/
│   └── targets.txt                 # sample input
└── hmr-kb-ingestion-pipeline/      # the installable skill
    ├── SKILL.md
    ├── scripts/
    │   ├── ingest.py               # fetch → extract → dedup → save → state engine
    │   ├── crawl.py                # optional: discover document URLs from listing pages
    │   └── validate_meta.py        # metadata schema validator
    ├── references/
    │   └── pillars.md              # Five Pillars mapping + tagging guide
    └── tests/                      # offline pytest suite for crawl.py
        ├── test_crawl.py
        └── fixtures/               # static HTML listing pages
```

---

## License

[MIT](LICENSE) © 2026 wikigoo
