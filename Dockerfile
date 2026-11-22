FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/palermostest25/reelflow"
LABEL org.opencontainers.image.title="Reelflow"
LABEL org.opencontainers.image.description="Instagram and YouTube publishing desk with an integrated web UI and scheduler"
# NVIDIA_DRIVER_CAPABILITIES lets the NVIDIA runtime mount the video encode/decode libraries when a GPU is
# passed in (see compose.yaml); Debian's FFmpeg then uses NVENC and CUDA decoding. Without a GPU it does nothing.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data FOOTAGE_DIR=/footage NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata && rm -rf /var/lib/apt/lists/* && useradd -m -u 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
RUN mkdir -p /data /footage && chown app:app /data /footage
ARG BUILD_VERSION=dev
ARG BUILD_REVISION=unknown
ENV REELFLOW_VERSION=$BUILD_VERSION REELFLOW_REVISION=$BUILD_REVISION
LABEL org.opencontainers.image.version="$BUILD_VERSION" org.opencontainers.image.revision="$BUILD_REVISION"
USER app
EXPOSE 8080
CMD ["python", "-m", "app.launch"]
