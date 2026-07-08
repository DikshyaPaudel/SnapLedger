"""
Day 6: /extract now enqueues a background job instead of blocking on Gemini.
The actual extraction logic lives in worker.py (runs as a separate process).

Run the API with:
    uvicorn main:app --reload

Run the worker in a SEPARATE terminal:
    arq worker.WorkerSettings

Then open http://127.0.0.1:8000/docs to try it.
"""

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job

from database import init_db, get_db, Receipt

MAX_FILE_SIZE_MB = 10

app = FastAPI(
    title="SnapLedger API",
    description="Upload a receipt image, get back structured spending data.",
    version="0.3.0",
)


@app.on_event("startup")
async def on_startup():
    """Create database tables and connect to Redis when the app starts."""
    init_db()
    app.state.redis = await create_pool(RedisSettings(host="localhost", port=6379))


@app.on_event("shutdown")
async def on_shutdown():
    await app.state.redis.close()


@app.get("/")
def root():
    """Simple health check -- confirms the API is alive."""
    return {"status": "ok", "message": "SnapLedger API is running"}


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
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

    # Hand off to the background worker -- returns instantly, doesn't wait for Gemini
    job = await app.state.redis.enqueue_job(
        "extract_receipt_task", image_bytes, file.content_type
    )

    return {"job_id": job.job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Check the status of a background extraction job. Poll this until status is 'complete'."""
    job = Job(job_id, app.state.redis)
    status = await job.status()

    if status == "not_found":
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")

    if status == "complete":
        result = await job.result()
        return {"status": "complete", "result": result}

    return {"status": str(status)}


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