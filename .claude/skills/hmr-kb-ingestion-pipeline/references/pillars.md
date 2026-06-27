# HMR Five Product Pillars — Mapping & Tagging Guide

Read this when filling the **judgement** fields of a `.meta.json` stub
(`hmr_target_pillars`, `ai_clean_title`, `ai_executive_summary`, `semantic_tags`,
`device_model`). The mechanical fields are already filled by `ingest.py`.

The companion text to analyse is written next to each saved file as
`<filename>.extracted.txt`. Read that, not the raw PDF.

---

## Pillar assignment

Assign **one or more** pillars based on what the document actually covers. Most docs map
to a single dominant pillar; assign extras only when a section genuinely serves that pillar.

| Pillar | Assign when the document covers… |
|--------|----------------------------------|
| `1_new_phone_buying_guide` | specs comparison, purchase advice, choosing a new device |
| `2_used_phone_fraud_detection` | counterfeit detection, used-phone inspection, IMEI/serial checks |
| `3_hardware_troubleshooting` | fault diagnosis, repair steps, error codes, hardware issues |
| `4_hardware_education` | how components work, teardowns, technical education |
| `5_accessories_guidance` | chargers, cables, cases, compatibility, accessories |

Use the **exact** string keys above — `validate_meta.py` rejects anything else.

If the document is a generic user manual touching several areas, prefer the pillar that
matches the chapters with the most actionable content (usually `3_hardware_troubleshooting`
for manuals) rather than tagging all five.

---

## ai_clean_title

A clean, human-readable title for the document, e.g.
`Samsung Galaxy S24 Ultra User Guide`. Strip site cruft, SEO suffixes, and file-name noise.

## device_model

The specific model the doc is about (e.g. `Galaxy S24 Ultra`). If it spans a whole
line-up or isn't device-specific, use the series name or `General`.

## ai_executive_summary

Three short paragraphs, in **English**, summarising the document conceptually — what it is,
what problems it helps solve, and which HMR pillar(s) it serves. Write for a knowledge-base
retrieval context: dense with the concepts a user might ask about, not marketing prose.

---

## semantic_tags (English only)

6–12 English tags that reflect how people actually search for this content. Mix:

- **Technical terms** — the precise vocabulary (`OLED burn-in`, `battery calibration`,
  `IMEI verification`, `USB-C PD`).
- **Colloquial / symptom phrasing** — how a non-expert describes the same thing
  (`screen ghosting`, `phone won't charge`, `fake charger`, `bad battery`).

Aim for coverage across the way different users would phrase the same need, so retrieval
catches both the expert query and the layperson query. Keep each tag short (1–4 words).

**Example for a Galaxy S24 Ultra troubleshooting manual:**

```json
"semantic_tags": [
  "OLED burn-in", "screen ghosting", "battery health", "phone won't charge",
  "overheating", "USB-C charging", "fake charger", "factory reset", "water damage"
]
```

`validate_meta.py` enforces the 6–12 count — fewer hurts recall, more dilutes relevance.
