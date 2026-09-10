# SENTINEL VISION backend.
#
# The image is built around one awkward fact: this service does real computer
# vision. Ultralytics pulls torch, easyocr pulls its own model weights, and
# OpenCV needs system libraries that are not in a slim base. Those are handled
# explicitly here rather than hoping the base image happens to carry them.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# opencv-python-headless still needs libGL/glib at runtime, and ffmpeg is what
# actually decodes RTSP. Installed before the source copy so this layer is
# cached across code changes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, as their own layer: application code changes far more
# often than requirements.txt, and torch alone is a multi-minute install.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# NOTE ON MODEL WEIGHTS: `*.pt` is excluded by .dockerignore (it is gitignored
# too, so a clean checkout has none), which means ultralytics downloads
# yolov8n.pt, and EasyOCR its recognition models, on FIRST USE — not at build
# time. Consequences, stated rather than discovered in production:
#   * the first camera started in a fresh container needs outbound internet;
#   * that first start is slow (weights are tens to hundreds of MB);
#   * the downloads land in the sentinel user's home, which is why the account
#     below is created WITH a home directory.
# /api/health does not depend on any of it — model loading is lazy and
# per-camera — so the healthcheck stays honest about whether the API is serving.
# An air-gapped deployment should bake the weights in or mount them instead.

# Runs as a non-root user. The container writes real evidence files and an
# uploads directory, so those are owned by that user rather than root.
RUN useradd --create-home --uid 10001 sentinel \
    && mkdir -p /app/uploads /app/evidence_store \
    && chown -R sentinel:sentinel /app
USER sentinel

EXPOSE 8000

# Uses the app's own health endpoint rather than a bare port check: the port is
# open the moment uvicorn binds, which is well before the model weights have
# loaded and the service can actually serve.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# No --reload: that is a development flag and it doubles memory by running a
# reloader process alongside the app.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
