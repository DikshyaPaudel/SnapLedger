"""
Day 2: Wrap yesterday's extraction logic into a real API.

Run with:
    uvicorn main:app --reload

Then open http://127.0.0.1:8000/docs in your browser -- FastAPI
auto-generates an interactive UI where you can upload a receipt image
directly and see the response, no Postman needed.
"""

import os
import json
import tempfile
import google.generativeai as genai

from fastapi import FastAPI, UploadFile, File, HTTPException

# ---- SETUP ----
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("Set the GEMINI_API_KEY environment variable first.")

genai.configure(api_key=API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

PROMPT = """
You are a receipt-parsing engine. Look at this image.

First, decide: is this actually a photo of a receipt or invoice?

If it is NOT a receipt or invoice (e.g. it's a random photo, a document,
a screenshot, anything else), respond with ONLY this JSON:
{"is_receipt": false, "reason": "brief explanation of what the image actually shows"}

If it IS a receipt or invoice, respond with ONLY valid JSON, no explanation,
no markdown fences, no extra text, using exactly this schema:

{
  "is_receipt": true,
  "vendor": string,
  "date": string (format YYYY-MM-DD, or null if not visible),
  "total_amount": number,
  "currency": string (3-letter code if you can tell, else best guess),
  "category": string (one of: food, transport, utilities, shopping, health, other),
  "line_items": [
    {"description": string, "amount": number}
  ],
  "confidence": string (one of: high, medium, low -- based on image clarity)
}

If a field is not visible or unclear, use null. Do not invent data that isn't
on the receipt. If the image is blurry or partially unreadable, still extract
what you can, set confidence to "low" or "medium", and use null for anything
you genuinely cannot read.
"""

app = FastAPI(
    title="SnapLedger API",
    description="Upload a receipt image, get back structured spending data.",
    version="0.1.0",
)


def extract_receipt_from_bytes(image_bytes: bytes, mime_type: str) -> dict:
    """Send image bytes to Gemini and return parsed structured data."""
    response = model.generate_content(
        [
            PROMPT,
            {"mime_type": mime_type, "data": image_bytes},
        ]
    )

    raw_text = response.text.strip()

    # Defensive cleanup: Gemini sometimes wraps JSON in ```json fences anyway
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        raw_text = raw_text.replace("json\n", "", 1).strip()

    return json.loads(raw_text)


@app.get("/")
def root():
    """Simple health check -- confirms the API is alive."""
    return {"status": "ok", "message": "SnapLedger API is running"}


MAX_FILE_SIZE_MB = 10


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    """
    Upload a receipt image (jpg/png). Returns structured extraction:
    vendor, date, total amount, currency, category, and line items.

    If the image isn't a receipt, returns a 422 explaining what was detected instead.
    """
    # Basic validation: only accept image files
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=422,
            detail=f"Expected an image file, got content type: {file.content_type}",
        )

    image_bytes = await file.read()

    if len(image_bytes) == 0:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    size_mb = len(image_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=422,
            detail=f"File too large ({size_mb:.1f}MB). Max size is {MAX_FILE_SIZE_MB}MB.",
        )

    # Try extraction once, retry once on bad JSON (models occasionally glitch)
    result = None
    last_error = None
    for attempt in range(2):
        try:
            result = extract_receipt_from_bytes(image_bytes, file.content_type)
            break
        except json.JSONDecodeError as e:
            last_error = e
            continue

    if result is None:
        raise HTTPException(
            status_code=502,
            detail="Model did not return valid JSON after retry. Try a clearer image.",
        )

    # Handle the "this isn't a receipt" case cleanly
    if result.get("is_receipt") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Image does not appear to be a receipt. {result.get('reason', '')}",
        )

    return result