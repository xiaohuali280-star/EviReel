# EviReel API / dispatcher / worker image (one codebase, different commands; see compose.yml).
FROM python:3.11.14-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml setup.cfg README.md run.py ./
COPY vam ./vam
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -e ".[platform,pipeline]"
ENV DATA_ROOT=/data/evireel PIPELINE_MODE=worker
RUN useradd --create-home --uid 10001 evireel && mkdir -p /data/evireel && chown -R evireel /data
USER evireel
EXPOSE 8000
CMD ["python", "run.py"]
