# Laya (open-source, non-generative decision model) served over the
# Jev-compatible POST /v1/systemone API. CPU build: Docker on macOS cannot use
# the Apple GPU, and a CPU image also runs on any Linux or Windows machine.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models

# CPU-only torch first, so pip does not pull the much larger CUDA build.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install "laya[serve]==0.3.11"

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=10 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["laya-serve"]
