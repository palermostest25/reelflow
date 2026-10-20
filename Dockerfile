FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/palermostest25/reelflow"
LABEL org.opencontainers.image.title="Reelflow"
LABEL org.opencontainers.image.description="Instagram posting desk with an integrated web UI and scheduler"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data FOOTAGE_DIR=/footage
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata && rm -rf /var/lib/apt/lists/* && useradd -m -u 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
RUN mkdir -p /data /footage && chown app:app /data /footage
USER app
EXPOSE 8080
CMD ["python", "-m", "app.launch"]
