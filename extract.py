"""
Day 1: Prove the core idea works.
Send a receipt image to Gemini, get back clean structured JSON.
No FastAPI, no database yet -- just the raw extraction logic.
"""

import os
import json
import google.generativeai as genai

# ---- SETUP ----
# 1. Go to https://aistudio.google.com/apikey
# 2. Create a free API key (no credit card needed)
# 3. Set it as an environment variable before running this script:
#      export GEMINI_API_KEY="your-key-here"        (Mac/Linux)
#      set GEMINI_API_KEY=your-key-here              (Windows cmd)
#
# Do NOT hardcode your key directly in this file.

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("Set the GEMINI_API_KEY environment variable first.")

genai.configure(api_key=API_KEY)

# Gemini 2.5/3 Flash is the right free-tier model for this: fast, multimodal, generous limits.
model = genai.GenerativeModel("gemini-2.5-flash")

PROMPT = """
You are a receipt-parsing engine. Look at this receipt image and extract the data.

Respond with ONLY valid JSON, no explanation, no markdown fences, no extra text.
Use exactly this schema:

{
  "vendor": string,
  "date": string (format YYYY-MM-DD, or null if not visible),
  "total_amount": number,
  "currency": string (3-letter code if you can tell, else best guess),
  "category": string (one of: food, transport, utilities, shopping, health, other),
  "line_items": [
    {"description": string, "amount": number}
  ]
}

If a field is not visible or unclear, use null. Do not invent data that isn't on the receipt.
"""


def extract_receipt(image_path: str) -> dict:
    """Send a receipt image to Gemini and return parsed structured data."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    # Basic mime type guess -- good enough for jpg/png test images
    mime_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"

    response = model.generate_content(
        [
            PROMPT,
            {"mime_type": mime_type, "data": image_bytes},
        ]
    )

    raw_text = response.text.strip()

    # Gemini sometimes wraps JSON in ```json fences even when told not to -- strip them defensively
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        raw_text = raw_text.replace("json\n", "", 1).strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        print("--- Raw model output (could not parse as JSON) ---")
        print(raw_text)
        raise


if __name__ == "__main__":
    # Put a test receipt image in this same folder and update the filename below.
    test_image = "receipt_sample.png"

    result = extract_receipt(test_image)
    print(json.dumps(result, indent=2))