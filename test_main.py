"""
Day 8: Tests for the SnapLedger API.

Run with:
    pytest

These tests use FastAPI's TestClient, which lets us call our endpoints
directly in-process -- no need for the server or worker to actually be running.

Note: tests that would need the real Gemini API or a running worker are
kept separate and mocked, so the test suite runs fast and doesn't cost
API credits every time you run it.
"""

import io
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _run_startup_events():
    """
    Ensures FastAPI's startup event (which connects to Redis) actually runs
    before each test. Without this, app.state.redis is never set, and any
    endpoint touching it (like /jobs/{job_id}) fails with an AttributeError.
    """
    with TestClient(app) as c:
        global client
        client = c
        yield


# ---- Basic health check ----

def test_root_returns_ok():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---- /extract validation (no real Gemini call needed for these) ----

def test_extract_rejects_non_image_file():
    fake_pdf = io.BytesIO(b"not a real pdf, just bytes for testing")
    response = client.post(
        "/extract",
        files={"file": ("test.pdf", fake_pdf, "application/pdf")},
    )
    assert response.status_code == 422
    assert "Expected an image file" in response.json()["detail"]


def test_extract_rejects_empty_file():
    empty_file = io.BytesIO(b"")
    response = client.post(
        "/extract",
        files={"file": ("empty.png", empty_file, "image/png")},
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


def test_extract_rejects_oversized_file():
    # Create a fake file just over the 10MB limit
    big_file = io.BytesIO(b"0" * (11 * 1024 * 1024))
    response = client.post(
        "/extract",
        files={"file": ("big.png", big_file, "image/png")},
    )
    assert response.status_code == 422
    assert "too large" in response.json()["detail"].lower()


@patch("main.app.state")
def test_extract_enqueues_job_on_valid_image(mock_state):
    """
    Confirms /extract queues a job and returns a job_id, without
    actually calling Gemini or needing Redis running.
    """
    mock_job = AsyncMock()
    mock_job.job_id = "test-job-123"
    mock_state.redis.enqueue_job = AsyncMock(return_value=mock_job)

    fake_image = io.BytesIO(b"fake png bytes for testing")
    response = client.post(
        "/extract",
        files={"file": ("receipt.png", fake_image, "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert "job_id" in response.json()


# ---- /receipts ----

def test_list_receipts_returns_a_list():
    response = client.get("/receipts")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_get_nonexistent_receipt_returns_404():
    response = client.get("/receipts/999999")
    assert response.status_code == 404


# ---- /jobs/{job_id} ----

def test_get_nonexistent_job_returns_404():
    response = client.get("/jobs/this-job-id-does-not-exist")
    assert response.status_code == 404