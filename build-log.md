# SnapLedger — Build Log

A running record of what was built, why, and what was learned — kept so it can be used directly as interview material and project documentation.

---

## Day 1 — Receipt Extraction Proof of Concept

**What I built:** A standalone Python script that takes a photo of a receipt and returns clean, structured JSON (vendor, date, total amount, currency, category, and itemized line items) using Google's Gemini multimodal LLM API.

**Why this approach:** Traditional receipt parsing relies on OCR plus brittle regex/rule-based logic that breaks on different receipt formats, fonts, or layouts. Instead, I used a vision-capable LLM that reads the image directly and reasons about the content, which handles format variation naturally without needing separate parsing rules per vendor.

**How it works:**
1. The script reads a receipt image as bytes
2. Sends it to Gemini along with a strict prompt specifying an exact JSON schema to return
3. Gemini returns the extracted data as text
4. The script defensively cleans and parses that text into JSON (handling cases where the model wraps output in markdown code fences despite instructions not to)

**What I validated:** Tested against a real Walmart receipt — correctly extracted vendor name, date, total amount, and both line items with accurate individual prices.

**What I noted for later:**
- Response time was ~22 seconds for a single image, which isn't acceptable for a real-time API — this is why the next phase of the project moves extraction into a background job queue (Celery/arq) so the API can respond instantly with a job ID while processing happens asynchronously.
- Categorization (e.g., "shopping" vs "food") is a judgment call the LLM makes, not a hard fact like the total amount — worth refining later with more specific prompting or per-item categorization.

**Interview-ready one-liner:** "I built a receipt intelligence API that uses an LLM's vision capability to extract structured financial data from receipt photos, since it handles varied formats better than traditional OCR-plus-rules approaches — and I designed it with async processing from the start because LLM calls are too slow to block a real API response."

---

## Day 2 — FastAPI Endpoint + Input Validation

**What I built:** Wrapped the Day 1 extraction logic into a real FastAPI application with a `POST /extract` endpoint. Also added a `GET /` health check endpoint, which is standard practice for any deployed API.

**Why this approach:** A standalone script only I can run isn't useful to anyone else. Wrapping it in FastAPI turns it into a real HTTP service that a frontend, another developer, or an automated system could call. FastAPI also auto-generates interactive API documentation (Swagger UI at `/docs`) directly from the code, so there's no separate documentation to maintain and go stale.

**How it works:**
1. Client sends a `POST` request to `/extract` with an image file as multipart form data
2. The endpoint validates the file is actually an image (rejects PDFs, text files, etc. with a clean 422 error instead of crashing)
3. The image bytes are passed to the same Gemini extraction function from Day 1
4. Structured JSON is returned with a 200 status code, or a clean error response if something goes wrong

**What I validated:**
- Uploading a real receipt image through the live API returned the same accurate structured JSON as the Day 1 script (vendor, date, amount, line items all correct)
- Uploading a non-image file (a PDF) correctly returned a `422 Unprocessable Entity` with a clear message ("Expected an image file, got content type: application/pdf") instead of crashing the server

**What I noted for later:**
- The endpoint still runs synchronously — the client waits the full ~20+ seconds for the LLM response. This is the exact problem Week 2's background job queue (Celery/arq) is designed to solve.
- Need to test what happens with a corrupted or non-receipt image (e.g. a random photo) to see how the model behaves versus a bad file type.

**Interview-ready one-liner:** "I turned the extraction logic into a real FastAPI service with proper input validation, so invalid uploads fail gracefully with clear error messages instead of crashing — and FastAPI's automatic OpenAPI docs meant anyone integrating with the API has live, always-accurate documentation without me writing a separate doc page."

---

## Day 3 — Edge Case Handling & Non-Receipt Detection

**What I built:** Hardened the `/extract` endpoint against real-world failure modes: non-receipt images, malformed model output, and oversized files. Added a pre-check where the model itself judges whether the image is actually a receipt before attempting extraction, plus a `confidence` field on results.

**Why this approach:** Early testing showed the model would hallucinate fake vendor/amount data when given a non-receipt image (e.g. a company logo), returning a confident-looking but entirely fabricated response with a 200 status. This is a dangerous failure mode for a financial tool — silent wrong data is worse than an error. Rather than bolting on separate image-classification logic, I extended the same prompt to make the LLM reason about "is this even a receipt?" first, since it's already looking at the image anyway.

**How it works:**
1. Updated prompt asks the model to first decide if the image is a receipt/invoice at all
2. If not, it returns `{"is_receipt": false, "reason": "..."}` — the API turns this into a clean 422 with the model's explanation
3. If it is a receipt, extraction proceeds as before, now also returning a `confidence` field (high/medium/low) based on image clarity
4. Added retry-once logic for malformed JSON responses (rare model glitches)
5. Added a file size limit (10MB) to reject unreasonably large uploads early

**What I validated:**
- Uploaded a company logo image (no transaction data) — correctly rejected with `422` and a clear explanation: "The image displays a company logo... but it contains no transaction details, dates, or amounts typical of a receipt or invoice."
- Confirmed real receipts still extract correctly with the new schema
- Confirmed a non-image file still returns the earlier validation error

**What I noted for later:**
- This detection relies on the LLM's judgment, not a hard rule — it's a probabilistic safeguard, not a guarantee. Documented as a known limitation rather than treated as a solved problem.
- Next: move this synchronous, ~20-second call into a background job so the API responds instantly (Week 2 priority).

**Interview-ready one-liner:** "I noticed the model would confidently hallucinate fake data when given a non-receipt image, which is a serious problem for a financial tool — so I extended the prompt to have the model reason about whether the image even is a receipt first, turning a silent wrong-answer risk into a clear, explained rejection instead."

---

