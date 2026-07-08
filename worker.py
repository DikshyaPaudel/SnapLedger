"""
Day 6: Background worker.

This file defines the actual extraction task that runs in the background,
separate from the FastAPI request/response cycle. arq picks up jobs from
Redis and runs them here, outside the main API process.

Run this worker in a SEPARATE terminal from your FastAPI server:
    arq worker.WorkerSettings
"""

import os
import json
import google.generativeai as genai
from arq.connections import RedisSettings

from database import SessionLocal, Receipt

# ---- SETUP (same as main.py) ----
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


def _clean_and_parse(raw_text: str) -> dict:
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        raw_text = raw_text.replace("json\n", "", 1).strip()
    return json.loads(raw_text)


async def extract_receipt_task(ctx, image_bytes: bytes, mime_type: str) -> dict:
    """
    The actual background job. arq calls this with the image data,
    runs the slow Gemini call here (away from the API's request/response cycle),
    saves the result to the database, and returns the final structured data.
    """
    response = model.generate_content(
        [PROMPT, {"mime_type": mime_type, "data": image_bytes}]
    )

    result = None
    try:
        result = _clean_and_parse(response.text)
    except json.JSONDecodeError:
        return {"error": "Model did not return valid JSON."}

    if result.get("is_receipt") is False:
        return {"error": f"Not a receipt: {result.get('reason', '')}"}

    # Save to database -- worker uses its own session, separate from the API's
    db = SessionLocal()
    try:
        receipt = Receipt(
            vendor=result.get("vendor"),
            date=result.get("date"),
            total_amount=result.get("total_amount"),
            currency=result.get("currency"),
            category=result.get("category"),
            confidence=result.get("confidence"),
            line_items=result.get("line_items"),
        )
        db.add(receipt)
        db.commit()
        db.refresh(receipt)
        result["id"] = receipt.id
    finally:
        db.close()

    return result


class WorkerSettings:
    """arq reads this class to know what functions it can run and how to connect to Redis."""
    functions = [extract_receipt_task]
    redis_settings = RedisSettings(host="localhost", port=6379)