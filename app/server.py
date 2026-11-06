"""Chat page backend: runs the finance-router workflow for each message and
explains what happened.

The browser calls only this service. It holds the Elastic credentials, starts
the workflow through the Kibana API, waits for it, and returns the answer plus
who decided (Laya or the LLM), Laya's probabilities, timings, tokens and an
estimated cost. The Benchmark tab reads results/summary.json from your own run
if there is one, otherwise the snapshot committed in data/benchmark-snapshot.
"""

import json
import os
import re
import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP = Path(__file__).resolve().parent
KIBANA = os.getenv("KIBANA_URL", "http://kibana:5601").rstrip("/")
ES = os.getenv("ES_URL", "http://elasticsearch:9200").rstrip("/")
PUBLIC_KIBANA = os.getenv("KIBANA_PUBLIC_URL", "http://localhost:5601").rstrip("/")
AUTH = ("elastic", os.getenv("ELASTIC_PASSWORD", ""))
WORKFLOW = "finance-router"
DEFAULT_MIN_P = float(os.getenv("ROUTER_MIN_PROBABILITY", "0.7"))
FALLBACK_MODEL = "anthropic/claude-haiku-5.5"
TERMINAL = {"completed", "failed", "cancelled", "timed_out"}

app = FastAPI(title="Two-tier finance router")
app.mount("/static", StaticFiles(directory=APP / "static"), name="static")


def kibana(method, path, **kwargs):
    return requests.request(method, KIBANA + path, auth=AUTH, headers={"kbn-xsrf": "true"}, timeout=60, **kwargs)


def benchmark():
    for path, source in ((Path("/app/results/summary.json"), "your latest local benchmark run"),
                         (APP / "data" / "benchmark-snapshot" / "summary.json", "the benchmark snapshot shipped with the repo")):
        if path.exists():
            data = json.loads(path.read_text())
            data["source"] = source
            return data
    return None


def per_token_prices():
    """Fallback model price per token, from the benchmark's price strings (no network needed)."""
    data = benchmark() or {}
    for entry in (data.get("summary") or {}).values():
        match = re.match(r"(\S+): \$([\d.]+) / \$([\d.]+)", entry.get("price") or "")
        if match and match.group(1) == FALLBACK_MODEL:
            return float(match.group(2)) / 1e6, float(match.group(3)) / 1e6
    return 0.10 / 1e6, 0.50 / 1e6


def cost_per_decision(name):
    summary = (benchmark() or {}).get("summary") or {}
    value = (summary.get(name) or {}).get("cost_per_1m_usd")
    return value / 1e6 if isinstance(value, (int, float)) else None


class Ask(BaseModel):
    text: str
    min_probability: float | None = None


@app.get("/")
def index():
    return FileResponse(APP / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"min_probability": DEFAULT_MIN_P, "kibana_url": PUBLIC_KIBANA, "fallback_model": FALLBACK_MODEL,
            "cost_per_decision": {name: cost_per_decision(name) for name in ("laya", "haiku", "sonnet", "llama")}}


@app.get("/api/benchmark")
def get_benchmark():
    data = benchmark()
    if not data:
        raise HTTPException(404, "No benchmark results yet: run `docker compose run --rm bench`.")
    return data


@app.get("/api/recent")
def recent():
    def esql(query):
        resp = requests.post(f"{ES}/_query", auth=AUTH, json={"query": query}, timeout=30)
        if not resp.ok:
            return []
        body = resp.json()
        cols = [c["name"] for c in body["columns"]]
        return [dict(zip(cols, row)) for row in body["values"]]
    return {
        "expenses": esql('FROM expenses | WHERE source == "router" | SORT @timestamp DESC '
                         "| KEEP @timestamp, amount, merchant, category | LIMIT 6"),
        "decisions": esql("FROM router-log | STATS messages = COUNT(*) BY decided_by"),
    }


@app.post("/api/ask")
def ask(body: Ask):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Type a message first.")
    min_p = body.min_probability if body.min_probability is not None else DEFAULT_MIN_P
    started = time.monotonic()
    resp = kibana("POST", f"/api/workflows/workflow/{WORKFLOW}/run", json={"inputs": {"text": text, "min_probability": min_p}})
    if not resp.ok:
        raise HTTPException(502, f"Kibana did not start the workflow: HTTP {resp.status_code} {resp.text[:200]}")
    execution_id = resp.json()["workflowExecutionId"]

    # The step list catches up a moment after the status turns terminal, so a
    # completed run is read only once its last step (log) is listed.
    execution = {}
    while time.monotonic() - started < 180:
        execution = kibana("GET", f"/api/workflows/executions/{execution_id}").json()
        status = execution.get("status")
        logged = any(s["stepId"] == "log" and s.get("status") in TERMINAL for s in execution.get("stepExecutions", []))
        if status in TERMINAL and (status != "completed" or logged):
            break
        time.sleep(0.4)
    elapsed_ms = (time.monotonic() - started) * 1000

    # Executions are stored in Elasticsearch and become searchable about a
    # second after they are written, so read with short retries: the final
    # labels and answer from the router-log document the workflow writes, and
    # Laya's probabilities and token counts from the step details.
    log_doc = {}
    for _ in range(20):
        hit = requests.post(f"{ES}/router-log/_search", auth=AUTH, timeout=30,
                            json={"size": 1, "query": {"term": {"workflow_execution_id": execution_id}}}).json()
        hits = (hit.get("hits") or {}).get("hits") or []
        if hits or execution.get("status") != "completed":
            log_doc = hits[0]["_source"] if hits else {}
            break
        time.sleep(0.5)

    wanted = {"laya", "llm_classify", "merchant"}
    steps = {}
    for _ in range(10):
        execution = kibana("GET", f"/api/workflows/executions/{execution_id}").json()
        for s in execution.get("stepExecutions", []):
            if s["stepId"] in wanted and s["stepId"] not in steps and s.get("status") == "completed":
                output = kibana("GET", f"/api/workflows/executions/{execution_id}/step/{s['id']}").json().get("output")
                if output:
                    steps[s["stepId"]] = {"ms": s.get("executionTimeMs"), "output": output}
        ran = {s["stepId"] for s in execution.get("stepExecutions", [])} & wanted
        if ran <= set(steps):
            break
        time.sleep(0.5)

    labels_out = log_doc
    answer = log_doc.get("answer")
    laya_answers = (steps.get("laya", {}).get("output", {}).get("data") or {}).get("answers") or {}
    laya = {q: {"choice": a.get("choice"), "probability": (a.get("probabilities") or {}).get(a.get("choice")),
                "probabilities": a.get("probabilities")} for q, a in laya_answers.items()}

    p_in, p_out = per_token_prices()
    llm_calls = []
    for step_id, purpose in (("llm_classify", "classify (Laya was unsure)"), ("merchant", "extract the merchant name")):
        if step_id in steps:
            usage = (steps[step_id]["output"].get("metadata") or {}).get("tokenUsage") or {}
            tin, tout = usage.get("promptTokens") or 0, usage.get("completionTokens") or 0
            llm_calls.append({"purpose": purpose, "ms": steps[step_id]["ms"], "input_tokens": tin,
                              "output_tokens": tout, "cost_usd": tin * p_in + tout * p_out})
    laya_cost = cost_per_decision("laya") or 0
    cost = laya_cost + sum(c["cost_usd"] for c in llm_calls)

    error = execution.get("error")
    return JSONResponse({
        "execution_id": execution_id,
        "status": execution.get("status"),
        "error": (error or {}).get("message") if isinstance(error, dict) else error,
        "answer": answer,
        "decided_by": labels_out.get("decided_by"),
        "labels": {k: labels_out.get(k) for k in ("intent", "category", "period")},
        "laya": laya,
        "min_probability": min_p,
        "timings_ms": {"total": elapsed_ms, "laya": steps.get("laya", {}).get("ms"),
                       "workflow": sum((s.get("executionTimeMs") or 0) for s in execution.get("stepExecutions", [])
                                       if s["stepId"] in {"laya", "llm_classify", "merchant", "run_query", "save_expense"})},
        "llm_calls": llm_calls,
        "cost_usd": cost,
        "baselines_usd": {"haiku": cost_per_decision("haiku"), "sonnet": cost_per_decision("sonnet")},
        "kibana_link": f"{PUBLIC_KIBANA}/app/workflows/{WORKFLOW}?tab=executions&executionId={execution_id}",
    })
