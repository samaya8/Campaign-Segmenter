# Campaign Segmenter API: FastAPI + saved K-Means segments + Gemini email drafts.
# Build:  docker build -t campaign-segmenter .
# Run:    docker run --rm -p 8000:8000 --env-file .env campaign-segmenter
#         (the API key is supplied at RUNTIME, it is never copied into the image)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1) Dependencies first: this layer is cached and only rebuilt when requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) Application code and the saved model outputs it serves. Copied explicitly, so .env can never get in.
COPY headlines.py ./
COPY src/ ./src/
COPY config/ ./config/
COPY data/processed/cluster_profiles.json data/processed/segment_emails.json ./data/processed/

# 3) Run as a non-root user; reports/ is the only writable folder (token/cost log).
RUN useradd --create-home --uid 10001 app && mkdir -p reports && chown app:app reports
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
