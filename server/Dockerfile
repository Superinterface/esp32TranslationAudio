# ==============================================================================
# Ultra-lightweight Docker Image for ESP32 Audio Transcription Server
# Base: python:3.11-slim (Debian Bookworm, glibc native, perfect for x86_64 / arm64)
# ==============================================================================
FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    RECORDINGS_DIR=/app/recordings \
    SERVER_PORT=8000 \
    NUM_THREADS=2

# Install minimal OS dependencies (curl for healthcheck/downloads, bzip2 for model unpack)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    bzip2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install lean Python dependencies (Only SenseVoice INT8 + VAD stack, NO PyTorch/bloat)
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# Copy application files and static web assets
COPY app.py download_models.py entrypoint.sh ./
COPY static/ ./static/

# Copy models directory (if models are already present, avoids re-downloading)
COPY models/ ./models/

# Ensure entrypoint is executable and create recordings directory
RUN chmod +x ./entrypoint.sh && mkdir -p /app/recordings

VOLUME ["/app/recordings"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/status || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "app.py"]
