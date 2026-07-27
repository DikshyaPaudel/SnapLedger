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

## Day 6 — Background Job Processing with arq + Redis

**What I built:** Split the extraction logic into two separate processes: the FastAPI app (`main.py`) now only handles HTTP requests, and a new background worker (`worker.py`) does the actual slow Gemini call. Redis sits between them as the message queue. `/extract` now returns instantly with a `job_id` instead of making the client wait ~20 seconds, and a new `GET /jobs/{job_id}` endpoint lets the client check when processing is done.

**Why this approach:** The synchronous version worked, but made every client wait the full LLM response time before getting anything back — unacceptable for a real API, especially if multiple people upload receipts at once. This is a standard pattern in production systems for any slow operation (video processing, large file exports, AI inference): respond immediately, do the real work in the background, let the client poll for completion.

**How it works:**
1. `POST /extract` validates the upload, then calls `enqueue_job()`, which writes a job request into Redis and returns instantly with a unique `job_id`
2. A separate always-running worker process (`arq worker.WorkerSettings`) constantly watches Redis for new jobs
3. When it picks one up, it runs `extract_receipt_task()` — the same Gemini call and JSON cleanup logic from before, now living in the worker — and saves the result to the database using its own independent database session
4. The client calls `GET /jobs/{job_id}` to check status (`queued` → `in_progress` → `complete`), and once complete, retrieves the actual extracted data

**What I validated:**
- Confirmed Redis was installed and running (`redis-cli ping` → `PONG`)
- Ran the API and worker as two separate terminal processes simultaneously
- [To fill in once tested: confirmed `/extract` returns a job_id instantly, and `/jobs/{job_id}` transitions from pending to complete with the correct extracted data, while the worker terminal visibly logs the job being picked up and processed]

**What I noted for later:**
- This pattern (instant response + job ID + polling) is invisible to a real end user — a real frontend would poll automatically in the background via JavaScript, not require anyone to manually copy a job ID. The manual copying is only because `/docs` (Swagger UI) is a raw developer testing tool, not a real user interface.
- Still using SQLite, not Postgres yet — planned swap during Dockerization (Day 9-10), since the `database.py` structure makes that a small, isolated change.
- Learned a practical gotcha: environment variables set with `export` only apply to the terminal session they were run in — needing two terminals running simultaneously (API + worker) means the API key has to be exported in both, or better, loaded from a `.env` file via `python-dotenv`.

**Interview-ready one-liner:** "I moved LLM extraction out of the request/response cycle entirely, using Redis and arq as a job queue — the API now responds in milliseconds with a job ID while a separate worker process handles the actual ~20-second AI call, which is the same async pattern production systems use for any slow operation like video processing or report generation."

---

## Day 8 — Automated Testing with pytest

**What I built:** A test suite (`test_main.py`) covering the API's core endpoints: health check, file upload validation (rejecting non-images, empty files, oversized files), job queuing, receipt retrieval, and 404 handling for missing resources. 8 tests total, all passing.

**Why this approach:** As the project grows (Postgres, Docker, new features), it becomes easy to accidentally break existing functionality while adding something new. Automated tests act as a safety net — running `pytest` after any change immediately confirms whether existing behavior still works, instead of manually re-clicking through Swagger UI every time, which doesn't scale and gets skipped under time pressure (exactly when bugs slip through). Tests are also one of the clearest signals of production-readiness in an interview, since untested code is hard for a team to trust or build on.

**How it works:**
1. Used FastAPI's `TestClient` to call endpoints directly in Python, simulating real HTTP requests without needing a running server
2. Used `unittest.mock` to fake out the Redis connection for the job-queuing test, so the test suite runs fast and doesn't spend real Gemini API credits or require Redis to be running every time tests execute
3. Tested both "happy path" behavior (valid uploads succeed) and failure cases (invalid file types, empty files, oversized files, non-existent IDs) — failure-case coverage is what separates thorough tests from superficial ones

**What I validated:**
- Initial run: 7 passed, 1 failed
- The failure (`AttributeError: 'State' object has no attribute 'redis'`) was a real bug in the test setup, not the app: FastAPI's startup event (which connects to Redis) only fires when `TestClient` is used as a context manager (`with TestClient(app) as c:`). The original test file created the client without that context manager, so the startup event never ran and `app.state.redis` was never initialized.
- Fixed with an `autouse=True` pytest fixture that wraps client creation in the required `with` block, ensuring startup/shutdown events fire before every test
- Final result: all 8 tests passing

**What I noted for later:**
- Noticed a `DeprecationWarning`: FastAPI's `@app.on_event("startup"/"shutdown")` pattern is deprecated in favor of "lifespan handlers." Not urgent — logged as a small polish item for Week 3 cleanup, not worth context-switching for mid-testing.
- Mocking external services (Redis, and eventually Gemini) rather than calling them for real in tests is a deliberate choice: tests should verify *my code's logic*, not re-test third-party services I already trust to work.

**Interview-ready one-liner:** "I wrote a pytest suite covering both success and failure paths, and hit a real bug in my own test setup along the way — FastAPI's startup events don't fire with TestClient unless you use it as a context manager, which is a subtle but common gotcha. Debugging that taught me more about FastAPI's lifecycle than the passing tests did."

---

## Day 9 — Containerization with Docker + Postgres Migration

**What I built:** Wrapped the entire application in Docker: a `Dockerfile` for the app code, and a `docker-compose.yml` orchestrating four services together — the FastAPI app, the arq worker, PostgreSQL, and Redis. Also migrated the database from SQLite to Postgres.

**Why this approach:** Up to this point, the project only ran reliably on my exact machine, with exactly what I'd installed locally. Docker packages the app with everything it needs (Python version, dependencies, configuration) into portable containers that behave identically anywhere — my laptop, a teammate's machine, or a cloud server. This is also the natural point to move off SQLite, since SQLite isn't built for multiple processes (API + worker) writing concurrently, while Postgres is what's actually used in production systems.

**How it works:**
1. `Dockerfile` builds one image containing the app code and dependencies; `api` and `worker` services both use this same image, just with different startup commands (`uvicorn` vs `arq worker.WorkerSettings`)
2. `docker-compose.yml` defines all four services and puts them on a shared private network, where each container can reach others by service name (e.g. `db`, `redis`) instead of `localhost`
3. `depends_on` with `condition: service_healthy` ensures the API and worker don't start until Postgres and Redis are actually ready to accept connections, avoiding startup race conditions
4. `database.py`'s connection string was changed to read from an environment variable (`DATABASE_URL`), so switching from SQLite to Postgres required no changes to the actual data model or query code — only the connection string

**What I validated:**
- Confirmed all four containers (`db`, `redis`, `api`, `worker`) start successfully together via `docker compose up --build`
- Ran the full end-to-end flow through the containerized stack: upload receipt → job queued → worker processes it → result saved to Postgres → retrievable via `/receipts`

**What I noted for later — real problems debugged, not just theory:**
- **Port conflicts:** Local system-level Redis (installed Day 6) and a local Postgres install were already occupying ports 6379/5432, colliding with Docker's containers trying to use the same ports. Fixed by stopping the system services, since Docker now owns those ports for this project.
- **DNS resolution failures between containers:** Leftover containers from a previous failed run weren't cleanly attached to a fresh Docker network, causing `could not translate host name "db"` errors. Fixed with `docker compose down -v --remove-orphans` followed by a completely fresh `up --build`.
- **Missing environment variable:** `docker-compose.yml` initially didn't pass `REDIS_HOST` to the containers, so the code fell back to its default (`localhost`) — which inside a container refers to itself, not the separate Redis container. Fixed by explicitly setting `REDIS_HOST: redis` in the compose file.
- This day involved genuine infrastructure debugging (distinguishing "my code is wrong" vs. "my environment has a port conflict" vs. "the network needs a clean restart") rather than application logic — this is realistic day-to-day engineering work, not just following a tutorial.

**Interview-ready one-liner:** "Containerizing the app surfaced real infrastructure issues — port conflicts with local services, Docker networking/DNS timing, and a missing environment variable — that taught me more about how distributed systems actually fail than the working version would have. Debugging those, rather than avoiding them, is what made the deployment genuinely production-shaped."

---

## Day 11 — Duplicate Detection + Docker Networking Fix

**What I built:** A `GET /duplicates` endpoint that scans all saved receipts and groups together likely duplicates — same vendor (case-insensitive), same amount (within rounding tolerance), and dates within 3 days of each other. Also resolved a deep Docker networking issue that was silently blocking all outbound API calls from inside containers.

**Why this approach:** This feature mirrors real payment reconciliation work from professional experience (Fonepay integration) — catching the same transaction submitted or recorded more than once. Exact-match comparison isn't enough in practice, since duplicates are often submitted a day or two apart, not on the identical date — hence the 3-day tolerance window rather than requiring an exact date match. Receipts missing vendor, amount, or date are skipped entirely, since flagging a false duplicate is worse than missing a real one when the data isn't confident enough to compare.

**How it works:**
1. Fetches all saved receipts, ordered oldest to newest
2. For each unchecked receipt, compares it against all later receipts on vendor + amount + date proximity
3. Matching receipts get grouped together and marked as checked, so they're not re-compared or double-counted in a later group
4. Returns a summary: how many duplicate groups were found, and which receipt IDs belong to each group

**What I validated:**
- Uploaded the same receipt image twice — confirmed both entries appeared correctly grouped together in `/duplicates`
- Uploaded a genuinely different receipt afterward — confirmed it did not get incorrectly grouped with the earlier ones

**What I noted for later — the Docker networking deep-dive:**
- After moving to Docker, background jobs got permanently stuck at `in_progress`, with no error — the worker had successfully connected to Redis and picked up the job, but silently hung trying to reach Gemini's API
- Diagnosed step by step: confirmed Redis/Postgres connectivity was fine (internal Docker networking worked), then tested raw outbound internet access from inside the container directly (`urllib.request.urlopen(...)`), which failed with `OSError: Network is unreachable` — isolating the problem to outbound network access specifically, not DNS or the app code
- Root cause: Ubuntu's UFW firewall has a default forward policy that blocks Docker's container traffic from reaching the internet, even though UFW's visible rules only appeared to control inbound ports. This is a known, documented conflict between UFW and Docker's independent iptables management — not something obvious from the app logs alone
- Fixed by adjusting UFW's forward policy and explicitly allowing routing on Docker's network interface

**What I noted for later:**
- This class of bug (silent hang, no error, no stack trace) is genuinely harder to debug than a crash, since there's no error message pointing anywhere — the fix came from systematically testing each network hop (Redis ✅, Postgres ✅, external internet ❌) rather than guessing
- Deployment platforms (Railway/Render) manage their own container networking, so this specific local firewall conflict likely won't reappear once deployed — but the debugging process (isolating which network hop fails) is a transferable skill regardless of where it runs

**Interview-ready one-liner:** "I built duplicate detection that mirrors real reconciliation logic from my Fonepay integration work, but the more interesting problem that day was a silent Docker networking failure — jobs would hang indefinitely with zero errors. I traced it by testing connectivity at each network hop individually, which isolated it to a UFW firewall policy silently blocking Docker's outbound traffic, a known but non-obvious conflict between how UFW and Docker each manage iptables."

---

