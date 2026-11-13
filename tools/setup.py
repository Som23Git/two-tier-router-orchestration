"""One-shot setup, run by the `setup` service after Kibana and Laya are healthy.

Creates everything the demo needs and is safe to run again:
  1. turns on Workflows in Kibana
  2. OpenRouter presets (temperature 0, reasoning off) and Elasticsearch
     inference endpoints that point at them
  3. indices: expenses, bench-messages, bench-results, router-log
  4. 13 months of sample expenses, so search questions return real numbers
  5. the labeled benchmark messages from data/messages.jsonl
  6. the finance-router workflow, then a smoke test with two messages
"""

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from elasticsearch import ApiError, helpers

import labels
from common import (FALLBACK_LLM, INDEX_EXPENSES, INDEX_MESSAGES, INDEX_RESULTS, INDEX_ROUTER_LOG, LLMS,
                    WORKFLOW_ID, env, es, kibana, wait_for)

OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
APP = Path(__file__).resolve().parent


def step(title):
    print(f"\n== {title}", flush=True)


# ------------------------------------------------------------------ Kibana

def enable_workflows():
    # The public /api/kibana/settings route is not available on this
    # configuration; the internal route is what the Advanced Settings page uses.
    resp = kibana("POST", "/internal/kibana/settings", headers={"x-elastic-internal-origin": "kibana"},
                  json={"changes": {"workflows:ui:enabled": True}})
    resp.raise_for_status()
    print("workflows:ui:enabled = true")


# ------------------------------------------------------------------ models

def openrouter_key_ok():
    key = env("OPENROUTER_API_KEY")
    if not key or key.startswith("<"):
        return False, "OPENROUTER_API_KEY is not set"
    resp = requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    return resp.ok, f"OpenRouter answered HTTP {resp.status_code}"


def openrouter_headers():
    return {"Authorization": f"Bearer {env('OPENROUTER_API_KEY', required=True)}"}


def upsert_preset(slug, model, extra):
    """An OpenRouter preset holds the settings Elasticsearch's OpenAI-compatible inference service cannot send."""
    config = {"model": model, "temperature": 0, "max_tokens": 256, **extra}
    resp = requests.post(f"https://openrouter.ai/api/v1/presets/{slug}/chat/completions",
                         headers=openrouter_headers(), json={**config, "messages": [{"role": "user", "content": "preset"}]},
                         timeout=60)
    resp.raise_for_status()


def ensure_endpoint(client, inference_id, slug):
    """Create the chat_completion endpoint if missing. Never delete a working one first."""
    if client.options(ignore_status=[404]).inference.get(inference_id=inference_id).meta.status == 200:
        return "exists"
    for attempt in range(6):
        try:
            client.inference.put(
                task_type="chat_completion",
                inference_id=inference_id,
                inference_config={
                    "service": "openai",
                    "service_settings": {
                        "api_key": env("OPENROUTER_API_KEY", required=True),
                        "model_id": f"@preset/{slug}",
                        "url": OPENROUTER_CHAT,
                        "rate_limit": {"requests_per_minute": int(env("OPENROUTER_RPM", "120"))},
                    },
                },
            )
            return "created"
        except ApiError as e:
            # Creating an endpoint sends one test request; providers throttle now and then.
            if "429" not in str(e) or attempt == 5:
                raise
            time.sleep(5 * 2 ** attempt)


# ------------------------------------------------------------------ indices and data

MAPPINGS = {
    INDEX_EXPENSES: {"properties": {
        "@timestamp": {"type": "date"}, "amount": {"type": "double"}, "currency": {"type": "keyword"},
        "merchant": {"type": "keyword"}, "category": {"type": "keyword"}, "description": {"type": "text"},
        "source": {"type": "keyword"}}},
    INDEX_MESSAGES: {"properties": {
        "id": {"type": "keyword"}, "text": {"type": "text"}, "intent": {"type": "keyword"},
        "category": {"type": "keyword"}, "period": {"type": "keyword"}, "amount": {"type": "double"},
        "merchant": {"type": "keyword"}, "twin_of": {"type": "keyword"}}},
    INDEX_RESULTS: {"dynamic_templates": [
        {"probabilities": {"path_match": "probabilities.*", "mapping": {"type": "object", "enabled": False}}}],
        "properties": {
            "run_id": {"type": "keyword"}, "message_id": {"type": "keyword"}, "classifier": {"type": "keyword"},
            "intent": {"type": "keyword"}, "category": {"type": "keyword"}, "period": {"type": "keyword"},
            "intent_ok": {"type": "boolean"}, "category_ok": {"type": "boolean"}, "period_ok": {"type": "boolean"},
            "all_ok": {"type": "boolean"}, "min_probability": {"type": "float"}, "latency_ms": {"type": "float"},
            "input_tokens": {"type": "long"}, "output_tokens": {"type": "long"}, "error": {"type": "text"},
            "@timestamp": {"type": "date"}}},
    INDEX_ROUTER_LOG: {"properties": {
        "@timestamp": {"type": "date"}, "text": {"type": "text"}, "intent": {"type": "keyword"},
        "category": {"type": "keyword"}, "period": {"type": "keyword"}, "decided_by": {"type": "keyword"},
        "laya_min_probability": {"type": "float"}, "answer": {"type": "text"},
        "workflow_execution_id": {"type": "keyword"}}},
}

MERCHANTS = {
    "coffee": (["Starbucks", "Blue Bottle", "Peet's", "Dunkin", "Philz"], 3, 9, 14),
    "groceries": (["Whole Foods", "Trader Joe's", "Costco", "Safeway", "Kroger"], 25, 140, 6),
    "dining": (["Chipotle", "DoorDash", "Olive Garden", "Uber Eats", "Sushi House"], 12, 90, 8),
    "transport": (["Uber", "Lyft", "Shell", "City Parking", "Metro Transit"], 3, 70, 7),
    "travel": (["United", "Marriott", "Airbnb", "Amtrak", "Hertz"], 80, 600, 1),
    "housing": (["Landlord", "City Power", "Comcast", "Water Utility"], 40, 2100, 4),
    "shopping": (["Amazon", "Target", "Uniqlo", "Best Buy", "IKEA"], 15, 220, 4),
    "entertainment": (["Netflix", "Spotify", "AMC", "Steam", "Ticketmaster"], 10, 120, 4),
    "health": (["CVS", "Walgreens", "Gym Co", "City Dental"], 15, 160, 2),
}


def seed_expenses(client):
    """About 400 expenses over the last 13 months; a fixed seed keeps them reproducible."""
    if client.count(index=INDEX_EXPENSES, query={"term": {"source": "seed"}})["count"]:
        return "already seeded"
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    docs = []
    for day in range(395):
        date = now - timedelta(days=day)
        for category, (merchants, low, high, per_month) in MERCHANTS.items():
            if rng.random() < per_month / 30:
                amount = round(rng.uniform(low, high) if category != "housing" else rng.choice([2100, 96.4, 69.99, 41.15]), 2)
                when = date.replace(hour=rng.randint(7, 22), minute=rng.randint(0, 59))
                docs.append({"_index": INDEX_EXPENSES, "_source": {
                    "@timestamp": when.isoformat(), "amount": amount, "currency": "USD",
                    "merchant": rng.choice(merchants), "category": category,
                    "description": f"{category} at {rng.choice(merchants)}", "source": "seed"}})
    helpers.bulk(client, docs, refresh="wait_for")
    return f"{len(docs)} seeded"


def load_messages(client):
    rows = [json.loads(line) for line in (APP / "data" / "messages.jsonl").read_text().splitlines() if line.strip()]
    helpers.bulk(client, ({"_index": INDEX_MESSAGES, "_id": r["id"], "_source": r} for r in rows), refresh="wait_for")
    return len(rows)


# ------------------------------------------------------------------ workflow

def indent_block(text, spaces):
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


def render_workflow():
    template = (APP / "workflows" / "finance-router.yaml").read_text()
    questions_yaml = yaml.safe_dump(labels.laya_questions(), sort_keys=False, width=1000).rstrip()
    replacements = {
        "__LAYA_URL__": env("LAYA_URL", "http://laya:8000"),
        "__FALLBACK_ENDPOINT__": LLMS[FALLBACK_LLM][0],
        "__EXTRACT_ENDPOINT__": LLMS[FALLBACK_LLM][0],
        "__MIN_PROBABILITY__": env("ROUTER_MIN_PROBABILITY", "0.7"),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    template = re.sub(r"^( *)__LAYA_QUESTIONS__$", lambda m: indent_block(questions_yaml, len(m.group(1))),
                      template, flags=re.M)
    template = re.sub(r"^( *)__LLM_PROMPT__$", lambda m: indent_block(labels.llm_prompt_template(), len(m.group(1))),
                      template, flags=re.M)
    leftover = re.findall(r"__[A-Z_]+__", template)
    if leftover:
        raise SystemExit(f"Unreplaced tokens in workflow template: {leftover}")
    return template


def upsert_workflow(yaml_text):
    existing = kibana("GET", f"/api/workflows/workflow/{WORKFLOW_ID}")
    if existing.status_code == 200:
        resp = kibana("PUT", f"/api/workflows/workflow/{WORKFLOW_ID}", json={"yaml": yaml_text})
    else:
        resp = kibana("POST", "/api/workflows/workflow", json={"id": WORKFLOW_ID, "yaml": yaml_text})
    resp.raise_for_status()
    body = resp.json()
    if body.get("valid") is False:
        raise SystemExit(f"Workflow stored but invalid: {json.dumps(body)[:1500]}")
    if not body.get("enabled", True):
        kibana("PUT", f"/api/workflows/workflow/{WORKFLOW_ID}", json={"enabled": True}).raise_for_status()


def run_workflow(text, timeout=180):
    resp = kibana("POST", f"/api/workflows/workflow/{WORKFLOW_ID}/run", json={"inputs": {"text": text}})
    resp.raise_for_status()
    execution_id = resp.json()["workflowExecutionId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        execution = kibana("GET", f"/api/workflows/executions/{execution_id}").json()
        if execution.get("status") in ("completed", "failed", "cancelled", "timed_out"):
            return execution_id, execution
        time.sleep(2)
    return execution_id, {"status": "still running"}


# ------------------------------------------------------------------ main

def main():
    client = es()
    step("Waiting for Elasticsearch, Kibana and Laya")
    wait_for("Elasticsearch", lambda: client.info())
    wait_for("Kibana", lambda: kibana("GET", "/api/status").json()["status"]["overall"]["level"] == "available")
    wait_for("Laya", lambda: requests.get(env("LAYA_URL", "http://laya:8000") + "/health", timeout=5).ok)
    license_info = client.license.get()["license"]
    print(f"Elasticsearch {client.info()['version']['number']}, license {license_info['type']} ({license_info['status']})")

    step("Workflows")
    enable_workflows()

    step("OpenRouter presets and inference endpoints")
    key_ok, key_status = openrouter_key_ok()
    if key_ok:
        for role, (inference_id, slug, model, extra) in LLMS.items():
            upsert_preset(slug, model, extra)
            print(f"{role:7} preset @{slug} -> {model}; endpoint {inference_id}: {ensure_endpoint(client, inference_id, slug)}")
    else:
        print(f"WARNING: skipped ({key_status}). Laya still works; the LLM fallback, the merchant")
        print("extraction and the LLM baselines need a valid key. Fix .env, then: docker compose up setup")

    step("Indices")
    for index, mappings in MAPPINGS.items():
        if not client.indices.exists(index=index):
            client.indices.create(index=index, mappings=mappings)
            print(f"{index}: created")
        else:
            print(f"{index}: exists")

    step("Data")
    print(f"expenses: {seed_expenses(client)}")
    print(f"bench-messages: {load_messages(client)} labeled messages")

    step("Workflow")
    upsert_workflow(render_workflow())
    print(f"{WORKFLOW_ID}: registered")

    step("Smoke test")
    if not key_ok:
        print("Skipped: the workflow's LLM steps need a valid OpenRouter key.")
        return
    for text in ("I paid $4.50 for a flat white at Blue Bottle", "how much did I spend on coffee last month?"):
        execution_id, execution = run_workflow(text)
        log = client.search(index=INDEX_ROUTER_LOG, size=1, query={"term": {"workflow_execution_id": execution_id}})
        hits = log["hits"]["hits"]
        summary = hits[0]["_source"] if hits else {}
        print(f"[{execution.get('status')}] {text!r} -> intent={summary.get('intent')} category={summary.get('category')} "
              f"period={summary.get('period')} decided_by={summary.get('decided_by')} | {summary.get('answer')}")

    public = env("KIBANA_PUBLIC_URL", "http://localhost:5601")
    print(f"""
== Ready
Kibana:    {public}  (user elastic, password from .env)
Workflow:  {public}/app/workflows/{WORKFLOW_ID}
Benchmark: docker compose run --rm bench""")


if __name__ == "__main__":
    main()
