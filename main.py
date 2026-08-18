"""
Day 6: /extract now enqueues a background job instead of blocking on Gemini.
The actual extraction logic lives in worker.py (runs as a separate process).

Run the API with:
    uvicorn main:app --reload

Run the worker in a SEPARATE terminal:
    arq worker.WorkerSettings

Then open http://127.0.0.1:8000/docs to try it.
"""

import os
import uuid
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job

from database import init_db, get_db, Receipt
from extraction import run_extraction

MAX_FILE_SIZE_MB = 10
ALLOWED_DOCUMENT_TYPES = {"receipt", "sales_invoice", "purchase_invoice"}

# Which job backend to use for background extraction:
# - "redis" (default): the "real" architecture -- a separate arq worker
#   process, connected via Redis. Used locally and in Docker.
# - "inline": no Redis/worker service required -- extraction runs via
#   FastAPI's BackgroundTasks in the same process, with an in-memory dict
#   standing in for the job store. Used for the free-tier deployed demo,
#   where provisioning a separate paid worker + Redis isn't justified for
#   a low-traffic portfolio project. Same non-blocking pattern (the request
#   still returns instantly), just without a second process.
JOB_BACKEND = os.environ.get("JOB_BACKEND", "redis")

# Only used in "inline" mode. Lives in memory, so it resets on restart/redeploy
# (acceptable for a demo; a real always-on deployment would still use Redis/a
# real queue instead of this).
inline_jobs: dict = {}


class LineItem(BaseModel):
    description: Optional[str] = None
    amount: Optional[float] = None


class ReceiptConfirm(BaseModel):
    """
    What the frontend sends once the user has reviewed (and possibly corrected)
    the extracted data. This is the ONLY place a receipt actually gets written
    to the database -- extraction itself no longer saves anything.
    """
    vendor: Optional[str] = None
    date: Optional[str] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    category: Optional[str] = None
    confidence: Optional[str] = None
    line_items: Optional[List[LineItem]] = None
    document_type: str = "receipt"  # receipt | sales_invoice | purchase_invoice

app = FastAPI(
    title="SnapLedger API",
    description="Upload a receipt image, get back structured spending data.",
    version="0.3.0",
)

# Allow the frontend to call this API. Wide open ("*") is fine for local/portfolio
# use -- a real production deployment would restrict this to specific domains.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    """Create database tables and, in 'redis' mode, connect to Redis."""
    init_db()
    if JOB_BACKEND == "redis":
        app.state.redis = await create_pool(
            RedisSettings(host=os.environ.get("REDIS_HOST", "localhost"), port=6379)
        )


@app.on_event("shutdown")
async def on_shutdown():
    if JOB_BACKEND == "redis":
        await app.state.redis.close()


@app.get("/")
def root():
    """Simple health check -- confirms the API is alive."""
    return {"status": "ok", "message": "SnapLedger API is running", "job_backend": JOB_BACKEND}


def _run_inline_job(job_id: str, image_bytes: bytes, mime_type: str):
    """Runs in the background (inline mode only) -- same extraction logic as
    the arq worker uses, just dispatched via FastAPI's BackgroundTasks instead
    of a separate process."""
    result = run_extraction(image_bytes, mime_type)
    inline_jobs[job_id] = {"status": "complete", "result": result}


@app.post("/extract")
async def extract(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """
    Upload a receipt image (jpg/png). Queues a background extraction job
    and returns immediately with a job_id -- check GET /jobs/{job_id}
    to see the result once processing finishes.
    """
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

    if JOB_BACKEND == "redis":
        # Hand off to the background worker -- returns instantly, doesn't wait for Gemini
        job = await app.state.redis.enqueue_job(
            "extract_receipt_task", image_bytes, file.content_type
        )
        return {"job_id": job.job_id, "status": "queued"}

    # inline mode: no separate worker/Redis -- FastAPI runs this after the
    # response is sent, same "returns instantly" behavior for the client
    job_id = str(uuid.uuid4())
    inline_jobs[job_id] = {"status": "in_progress", "result": None}
    background_tasks.add_task(_run_inline_job, job_id, image_bytes, file.content_type)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Check the status of a background extraction job. Poll this until status is 'complete'."""
    if JOB_BACKEND == "redis":
        job = Job(job_id, app.state.redis)
        status = await job.status()

        if status == "not_found":
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")

        if status == "complete":
            result = await job.result()
            return {"status": "complete", "result": result}

        return {"status": str(status)}

    # inline mode
    job = inline_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return job


@app.post("/receipts")
def confirm_and_save_receipt(payload: ReceiptConfirm, db: Session = Depends(get_db)):
    """
    Save a receipt/invoice AFTER the user has reviewed it.

    Nothing gets written to the database during /extract anymore -- that
    endpoint only returns what the model read off the image. This endpoint
    is the actual "store" step: the frontend calls it once the user has
    checked (and optionally corrected) the fields and confirms.
    """
    doc_type = payload.document_type if payload.document_type in ALLOWED_DOCUMENT_TYPES else "receipt"

    receipt = Receipt(
        vendor=payload.vendor,
        date=payload.date,
        total_amount=payload.total_amount,
        currency=payload.currency,
        category=payload.category,
        confidence=payload.confidence,
        line_items=[item.dict() for item in payload.line_items] if payload.line_items else None,
        document_type=doc_type,
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


@app.get("/receipts")
def list_receipts(db: Session = Depends(get_db)):
    """Return all saved receipts, most recent first."""
    receipts = db.query(Receipt).order_by(Receipt.created_at.desc()).all()
    return receipts


@app.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int, db: Session = Depends(get_db)):
    """Return a single saved receipt by its ID."""
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"Receipt {receipt_id} not found.")
    return receipt


@app.get("/duplicates")
def find_duplicates(db: Session = Depends(get_db)):
    """
    Scan all saved receipts and group together ones that look like likely
    duplicates: same vendor, same amount, and dates within 3 days of each other.

    This mirrors real-world payment reconciliation logic -- catching the
    same transaction submitted or recorded more than once.
    """
    receipts = db.query(Receipt).order_by(Receipt.created_at.asc()).all()

    duplicate_groups = []
    checked_ids = set()

    for i, receipt in enumerate(receipts):
        if receipt.id in checked_ids:
            continue
        if receipt.vendor is None or receipt.total_amount is None:
            continue  # can't compare confidently without these fields

        group = [receipt]

        for other in receipts[i + 1:]:
            if other.id in checked_ids:
                continue
            if other.vendor is None or other.total_amount is None:
                continue

            same_vendor = other.vendor.strip().lower() == receipt.vendor.strip().lower()
            same_amount = abs(other.total_amount - receipt.total_amount) < 0.01

            dates_close = False
            if receipt.date and other.date:
                try:
                    d1 = datetime.strptime(receipt.date, "%Y-%m-%d")
                    d2 = datetime.strptime(other.date, "%Y-%m-%d")
                    dates_close = abs((d1 - d2).days) <= 3
                except ValueError:
                    dates_close = False  # unparseable date -- don't guess

            if same_vendor and same_amount and dates_close:
                group.append(other)
                checked_ids.add(other.id)

        if len(group) > 1:
            checked_ids.add(receipt.id)
            duplicate_groups.append(
                {
                    "vendor": receipt.vendor,
                    "total_amount": receipt.total_amount,
                    "receipt_ids": [r.id for r in group],
                    "count": len(group),
                }
            )

    return {
        "duplicate_groups_found": len(duplicate_groups),
        "groups": duplicate_groups,
    }


ALLOWED_CATEGORIES = {"food", "transport", "utilities", "shopping", "health", "other"}


@app.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    """
    Quick spending overview: total spend, receipt count, and a breakdown
    by category. Just an aggregation query over what's already saved --
    no new extraction or external calls involved.

    Category values come straight from the LLM's judgment at extraction
    time (see worker.py's prompt), not from any rule-based logic here.
    Since that's a judgment call rather than a strict enum, anything the
    model returns outside the known category set gets folded into "other"
    so this endpoint's output stays predictable even if the model drifts.
    """
    total_amount = db.query(func.sum(Receipt.total_amount)).scalar() or 0
    total_receipts = db.query(func.count(Receipt.id)).scalar() or 0

    by_category = (
        db.query(Receipt.category, func.sum(Receipt.total_amount), func.count(Receipt.id))
        .group_by(Receipt.category)
        .all()
    )

    # Normalize: fold nulls and any unexpected/unrecognized values into "other"
    normalized_totals = {}
    for category, amount, count in by_category:
        key = category if category in ALLOWED_CATEGORIES else "other"
        if key not in normalized_totals:
            normalized_totals[key] = {"total": 0.0, "count": 0}
        normalized_totals[key]["total"] += amount or 0
        normalized_totals[key]["count"] += count

    return {
        "total_spent": round(total_amount, 2),
        "total_receipts": total_receipts,
        "by_category": [
            {"category": category, "total": round(data["total"], 2), "count": data["count"]}
            for category, data in normalized_totals.items()
        ],
    }


# Serves the frontend (index.html + assets) at /app, straight from FastAPI.
# Mounted at "/app" rather than "/" so it never collides with the API routes
# above -- e.g. GET /app returns the page, GET /receipts still hits the API.
app.mount("/app", StaticFiles(directory="static", html=True), name="static")