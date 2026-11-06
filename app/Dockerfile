# The chat page and its small API. The browser only talks to this service; the
# Elastic credentials stay here.
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN pip install "fastapi>=0.110" "uvicorn>=0.27" "requests>=2.32"
WORKDIR /app
COPY . /app
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
