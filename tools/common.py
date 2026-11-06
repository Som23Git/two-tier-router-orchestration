"""Shared settings and clients. Every value comes from the environment (.env)."""

import os
import sys
import time

import requests

# The OpenRouter models used as LLM baselines and as the router's fallback.
# role -> (inference endpoint ID, OpenRouter preset slug, model, extra preset settings)
LLMS = {
    "sonnet": ("router-sonnet", "router-sonnet", "anthropic/claude-sonnet-4.6", {"reasoning": {"enabled": False}}),
    "haiku": ("router-haiku", "router-haiku", "anthropic/claude-haiku-5.5", {"reasoning": {"enabled": False}}),
    "llama": ("router-llama", "router-llama", "meta-llama/llama-3.1-8b-instruct", {"provider": {"quantizations": ["bf16"]}}),
}
FALLBACK_LLM = "haiku"

INDEX_EXPENSES = "expenses"
INDEX_MESSAGES = "bench-messages"
INDEX_RESULTS = "bench-results"
INDEX_ROUTER_LOG = "router-log"
WORKFLOW_ID = "finance-router"


def env(name, default=None, required=False):
    value = (os.getenv(name) or "").strip() or default
    if required and not value:
        sys.exit(f"Missing {name}: set it in .env (see .env.example)")
    return value


def es():
    from elasticsearch import Elasticsearch

    return Elasticsearch(env("ES_URL", "http://elasticsearch:9200"),
                         basic_auth=("elastic", env("ELASTIC_PASSWORD", required=True)),
                         request_timeout=120)


def kibana(method, path, **kwargs):
    """Call the Kibana API as the elastic user."""
    url = env("KIBANA_URL", "http://kibana:5601").rstrip("/") + path
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json", **kwargs.pop("headers", {})}
    return requests.request(method, url, auth=("elastic", env("ELASTIC_PASSWORD", required=True)),
                            headers=headers, timeout=kwargs.pop("timeout", 120), **kwargs)


def wait_for(name, check, timeout=900, interval=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except Exception:  # not up yet
            pass
        time.sleep(interval)
    sys.exit(f"Timed out waiting for {name}")
