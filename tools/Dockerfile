# Small Python image for the one-shot setup, the password init and the benchmark.
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN pip install "elasticsearch>=9.0,<10" "requests>=2.32" "pyyaml>=6.0"
WORKDIR /app
COPY . /app
