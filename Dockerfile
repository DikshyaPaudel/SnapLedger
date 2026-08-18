# Base image: lightweight Python 3.12
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (separate layer, so Docker caches this
# and doesn't reinstall everything every time you change your code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual application code
COPY . .

# Default command -- overridden for the worker in docker-compose.yml
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]