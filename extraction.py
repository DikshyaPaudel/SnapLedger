"""
The actual Gemini extraction logic, kept independent of HOW it gets run.

This exists as its own module because the project runs in two modes:
- Locally/Docker: a separate arq worker process picks this up via Redis
  (see worker.py) -- the "real" architecture, demonstrating async job
  processing with a dedicated worker.
- Deployed (free tier): no Redis/worker service is provisioned, so main.py
  calls this function directly via FastAPI's BackgroundTasks instead.

Same extraction logic either way -- only the dispatch mechanism differs.
"""

import os
import json
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

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


def run_extraction(image_bytes: bytes, mime_type: str) -> dict:
    """
    Calls Gemini, parses the response, and returns structured data (or a
    plain error dict). Synchronous and side-effect-free -- doesn't touch the
    database, doesn't know or care who's calling it (arq worker or FastAPI
    BackgroundTasks). Nothing gets saved here; storage happens separately
    via POST /receipts once the user reviews and confirms.
    """
    try:
        response = model.generate_content(
            [PROMPT, {"mime_type": mime_type, "data": image_bytes}]
        )
    except google_exceptions.ResourceExhausted:
        # Free-tier quota hit (20 requests/day for gemini-2.5-flash). Return a
        # plain dict -- exception objects aren't always safely serializable
        # across process/queue boundaries, so never return the raw exception.
        return {"error": "Gemini API quota exceeded for today. Try again later, or check your plan at https://ai.google.dev/gemini-api/docs/rate-limits."}
    except Exception as e:
        return {"error": f"Extraction failed: {str(e)}"}

    try:
        result = _clean_and_parse(response.text)
    except json.JSONDecodeError:
        return {"error": "Model did not return valid JSON."}

    if result.get("is_receipt") is False:
        return {"error": f"Not a receipt: {result.get('reason', '')}"}

    return result