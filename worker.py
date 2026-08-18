"""
The arq background worker -- used locally and in Docker, where a real
Redis instance + separate worker process are available.

Run this in a SEPARATE terminal/container from your FastAPI server:
    arq worker.WorkerSettings

The actual Gemini call/parsing logic lives in extraction.py, shared with
the deployed (free-tier) version, which uses FastAPI BackgroundTasks
instead of arq/Redis -- see main.py's JOB_BACKEND switch.
"""

import os
from arq.connections import RedisSettings
from extraction import run_extraction


async def extract_receipt_task(ctx, image_bytes: bytes, mime_type: str) -> dict:
    """arq calls this with the image data; the real work happens in extraction.py."""
    return run_extraction(image_bytes, mime_type)


class WorkerSettings:
    """arq reads this class to know what functions it can run and how to connect to Redis."""
    functions = [extract_receipt_task]
    redis_settings = RedisSettings(
        host=os.environ.get("REDIS_HOST", "localhost"), port=6379
    )