# SnapLedger

An API and web app that turns a photo of a receipt or invoice into structured, reviewed data — vendor, date, amount, line items, category — using an LLM vision model instead of traditional OCR + rule-based parsing.

Built to solve a gap that shows up in every ERP/finance system: the system is great at handling data once it's inside, but getting a messy real-world document into structured form in the first place is still manual almost everywhere.

## What it does

1. Upload a photo of a document → `POST /extract`
2. Get an instant job ID back — extraction runs in the background (~15-20s), the request doesn't block
3. Poll `GET /jobs/{job_id}` → structured data comes back: vendor, date, total, currency, category, line items, confidence score
4. **Nothing is saved yet.** The frontend shows an editable review screen — correct anything the model got wrong
5. Confirm → `POST /receipts` actually writes it to the database
6. `GET /receipts` / `GET /receipts/{id}` — browse everything saved
7. `GET /summary` — total spend, receipt count, breakdown by category
8. `GET /duplicates` — flags likely-duplicate submissions (same vendor, same amount, dates within a few days)

A small frontend (`static/index.html`, served by FastAPI at `/app`) wraps this into a usable flow: pick a document type, upload, watch it get read, review and correct the extracted fields, confirm, and see it land in a running "Saved Documents" list.

## Tech stack

- **FastAPI** — async API layer, serves both the JSON API and the static frontend
- **Gemini vision API** — extraction (see "Why an LLM instead of OCR" below)
- **Redis + arq** — background job processing, so uploads return instantly instead of blocking for ~20s
- **PostgreSQL + SQLAlchemy** — persistence
- **Docker Compose** — local dev environment (api, worker, db, redis as separate services)
- **pytest** — test suite for endpoints, validation, and job flow (mocked Redis/Gemini calls)
- **Vanilla HTML/CSS/JS frontend** — no framework; talks to the API directly, polls for job completion, renders an editable review form before anything is stored

## Why extraction and storage are two separate steps

Early versions of this saved straight to the database the moment extraction finished. That's a mistake in any real system handling financial data: an LLM's read of a blurry or unusual document can be wrong, and writing that straight to a ledger with no human checkpoint means errors go in silently.

So the architecture is deliberately split:
- **`POST /extract`** only reads the document and returns what the model saw. Nothing is persisted.
- **`POST /receipts`** is the only place a record actually gets written — and it's called with whatever the user confirmed, which may differ from what the model originally extracted.

This mirrors how any reasonable ERP handles OCR/AI-assisted data entry: extraction proposes, a human disposes.

## Why an LLM instead of traditional OCR

Traditional OCR (Tesseract, cloud OCR APIs) reads pixels and outputs raw text — it has no understanding of what that text *means*. Pointed at a receipt, it returns something like:

```
WALMART
GV WHITE BREAD 3.84
HDMI 3FT CABLE 14.99
TOTAL 18.83
```

Turning that into structured fields (`vendor`, `total_amount`, `line_items`) requires a second layer of custom logic — regex, keyword matching, positional rules — and that logic breaks the moment the layout changes. Every vendor, every country, every handwritten slip looks different, so rule-based parsing means maintaining format-specific rules indefinitely.

An LLM vision model doesn't just read characters — it reasons about structure, the way a human does: "this number near the bottom labeled 'total' is the total, these lines above it are line items." That's the step OCR alone can't do.

**Honest tradeoffs, both directions:**

| | OCR + custom rules | LLM vision (this project) |
|---|---|---|
| Handles varied/unfamiliar formats | Poorly — breaks per format | Well — generalizes |
| Cost per request | Very low | Small, non-zero |
| Latency | Milliseconds | ~15-20s |
| Maintenance | Rules grow per vendor format | Prompt-based, no per-format code |
| Failure mode | Predictable, deterministic | Can occasionally hallucinate on ambiguous input |

OCR + rules is still the better choice for high-volume, fixed-format pipelines (e.g. a company processing thousands of invoices from a handful of known suppliers). LLM vision extraction fits this project's actual scenario better: varied, unpredictable, low-to-medium volume input, where the maintenance cost of per-format rules would outweigh the benefit.

## Known limitations & design decisions

- **Non-receipt detection.** The model first judges whether an uploaded image is actually a receipt/invoice before extracting. Non-receipts are rejected with a plain-language reason instead of returning hallucinated data.
- **Confidence scoring.** Every extraction includes a `confidence` field (high/medium/low). The review screen surfaces this directly — low-confidence extractions are visually flagged so they get a closer look before saving, rather than being silently trusted.
- **Category is LLM-judged; document type is not.** Category (food/transport/etc.) is decided by the model, because getting it wrong is low-stakes — at most a line item sits under the wrong spending label. Document type (Receipt / Sales Invoice / Purchase Invoice) is different: these sit on opposite sides of a ledger — revenue vs. expense — and misclassifying one directly corrupts financial reporting. That's not a field worth delegating to a probabilistic model without confirmation, so it stays as an explicit, mandatory user choice rather than an LLM guess.
- **The "Saved Documents" list currently covers all document types together** (Type / Vendor / Date / Amount). A real system would likely give Sales and Purchase Invoices their own dedicated views with full line-item detail, since they carry more accounting weight than a flat glance-list — noted as a natural next step, not built here.
- **Retry logic.** Malformed JSON from the model (rare) triggers one automatic retry before failing.
- **Gemini free-tier rate limits.** The free tier caps requests per day (20/day at time of writing for `gemini-2.5-flash`). Quota errors from the API are caught explicitly in the worker and surfaced as a clean message rather than crashing the background job.
- **Known edge case:** classification is judgment-based, not rule-based — an image with financial-sounding text but no real transaction data (e.g. a company logo) can occasionally still be misread. This is an accepted tradeoff of using an LLM rather than rigid OCR + regex, which would need separate handling per vendor format anyway.

## Running locally

```bash
cp .env.example .env   # add your GEMINI_API_KEY
docker compose up --build
```

- API + docs: `http://localhost:8000/docs`
- Frontend: `http://localhost:8000/app`

Both are served by the same FastAPI process — the frontend calls the API on the same origin, no CORS setup needed for normal use. (CORS middleware is still enabled in `main.py` in case the frontend is ever opened standalone or hosted separately.)

## Future optimizations (deliberately not built yet)

- **OCR pre-processing to cut token cost at scale.** Right now the full receipt image is sent to Gemini, which costs more tokens than the equivalent text would, since image inputs are tokenized in a resolution-dependent way. A hybrid approach — run OCR first, then send only the extracted text to the LLM — would reduce that cost. Not built here because (a) it removes the model's access to layout/spatial context, which is exactly what makes it more robust than pure OCR on messy or skewed receipts, and (b) at this project's current scale (free-tier usage, portfolio demo), token cost isn't an actual constraint worth the added complexity of a second pipeline stage. It's the natural next optimization if this were handling production volume.
- **Auto-detecting document type from the image**, with the user free to override, was considered and deliberately rejected — see "Known limitations" above for the reasoning.

## Tests

```bash
python -m pytest
```
