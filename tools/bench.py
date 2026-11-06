"""Benchmark: Laya vs LLM classifiers vs the router, on the same labeled messages.

For every message in data/messages.jsonl it asks each classifier the same three
questions (intent, category, period) and records the answer, latency and
tokens. LLMs are called through the Elasticsearch inference endpoints that
setup.py created, so this measures the same path a workflow uses.

Then it scores:
  accuracy     intent (all), category (index and search), period (search),
               and "all correct"
  consistency  share of reworded twins that got a different answer (flip rate)
  latency      p50 and p95 per message
  cost         per 1M decisions. LLMs: tokens x OpenRouter list price, read
               live. Laya: LAYA_HOURLY_USD divided by measured throughput,
               plus the fixed monthly cost of keeping one machine up.
  router       Laya first, the fallback LLM below a probability threshold,
               for several thresholds
and writes results/report.md, results/summary.json, results/per_message.csv
and one document per message and classifier to the bench-results index.

Usage (from the repo root):
  docker compose run --rm bench
  docker compose run --rm bench python bench.py --limit 40 --only laya,haiku
"""

import argparse
import csv
import json
import re
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from elasticsearch import helpers

import labels
from common import FALLBACK_LLM, INDEX_RESULTS, LLMS, env, es

APP = Path(__file__).resolve().parent
OUT = APP / "results"
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
HOURS_PER_MONTH = 730


# ------------------------------------------------------------------ classifiers

def needed_fields(intent):
    """Which answers matter for a message: period only for search, category not for other."""
    return ["intent"] + (["category"] if intent != "other" else []) + (["period"] if intent == "search" else [])


def ask_laya(text):
    started = time.monotonic()
    resp = requests.post(env("LAYA_URL", "http://laya:8000") + "/v1/systemone", timeout=120,
                         json={"model": "english", "state": text, "questions": labels.laya_questions()})
    latency = (time.monotonic() - started) * 1000
    resp.raise_for_status()
    answers = resp.json()["answers"]
    out = {q: answers[q]["choice"] for q in labels.QUESTIONS}
    probs = {q: answers[q]["probabilities"][answers[q]["choice"]] for q in labels.QUESTIONS}
    out["min_probability"] = min(probs[f] for f in needed_fields(out["intent"]))
    out["probabilities"] = {q: answers[q]["probabilities"] for q in labels.QUESTIONS}
    usage = resp.json().get("usage") or {}
    out.update(latency_ms=latency, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
    return out


def ask_llm(inference_id, text, attempts=6):
    """Stream one chat completion through Elasticsearch and parse the JSON answer."""
    url = f"{env('ES_URL', 'http://elasticsearch:9200')}/_inference/chat_completion/{inference_id}/_stream"
    body = {"messages": [{"role": "user", "content": labels.llm_prompt(text)}], "max_completion_tokens": 100, "temperature": 0}
    for attempt in range(attempts):
        started = time.monotonic()
        chunks, usage, error = [], {}, None
        with requests.post(url, json=body, stream=True, timeout=180,
                           auth=("elastic", env("ELASTIC_PASSWORD", required=True))) as resp:
            if resp.status_code != 200:
                error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            else:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    if "error" in event:
                        error = json.dumps(event["error"])[:300]
                        break
                    chunk = event.get("chat_completion", event)
                    chunks += [(c.get("delta") or {}).get("content") or "" for c in chunk.get("choices", [])]
                    usage = chunk.get("usage") or usage
        latency = (time.monotonic() - started) * 1000
        if error and "429" in error and attempt < attempts - 1:
            time.sleep(2 * 2 ** attempt)
            continue
        if error:
            raise RuntimeError(error)
        match = re.search(r"\{[\s\S]*\}", "".join(chunks))
        if not match:
            raise RuntimeError(f"no JSON in answer: {''.join(chunks)[:200]!r}")
        parsed = json.loads(match.group(0))
        out = {q: str(parsed.get(q, "")).strip() for q in labels.QUESTIONS}
        out.update(latency_ms=latency, input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"))
        return out


def classify_all(name, fn, messages, workers):
    def one(message):
        try:
            return message["id"], fn(message["text"])
        except Exception as e:  # recorded per message, the run continues
            return message["id"], {"error": str(e)[:300]}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(pool.map(one, messages))
    errors = sum(1 for r in results.values() if "error" in r)
    print(f"  {name:8} {len(messages)} messages in {time.monotonic() - started:5.1f}s, {errors} errors", flush=True)
    return results


# ------------------------------------------------------------------ scoring

def score(message, answer):
    if "error" in answer:
        return {"intent_ok": False, "category_ok": False, "period_ok": False, "all_ok": False}
    gold = message["intent"]
    checks = {
        "intent_ok": answer.get("intent") == gold,
        "category_ok": answer.get("category") == message["category"] if gold != "other" else None,
        "period_ok": answer.get("period") == message["period"] if gold == "search" else None,
    }
    checks["all_ok"] = all(v for v in checks.values() if v is not None)
    return checks


def rate(values):
    values = [v for v in values if v is not None]
    return 100.0 * sum(values) / len(values) if values else float("nan")


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return float("nan")
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def flip_rate(messages, answers):
    by_id = {m["id"]: m for m in messages}
    flips = []
    for m in messages:
        src = m.get("twin_of")
        if not src or src not in answers or m["id"] not in answers:
            continue
        a, b = answers[m["id"]], answers[src]
        if "error" in a or "error" in b:
            continue
        fields = needed_fields(by_id[src]["intent"])
        flips.append(any(a.get(f) != b.get(f) for f in fields))
    return rate(flips), len(flips)


def openrouter_prices():
    resp = requests.get("https://openrouter.ai/api/v1/models", timeout=60)
    resp.raise_for_status()
    return {m["id"]: (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"])) for m in resp.json()["data"]}


# ------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only the first N messages (twins are kept only if their source is in)")
    parser.add_argument("--only", help="comma list of classifiers: laya," + ",".join(LLMS))
    parser.add_argument("--workers", type=int, default=4, help="parallel requests per LLM")
    args = parser.parse_args()

    messages = [json.loads(line) for line in (APP / "data" / "messages.jsonl").read_text().splitlines() if line.strip()]
    if args.limit:
        keep = {m["id"] for m in messages[: args.limit]}
        messages = [m for m in messages if m["id"] in keep or m.get("twin_of") in keep]
    chosen = (args.only or "laya," + ",".join(LLMS)).split(",")
    client = es()
    missing = [r for r in chosen if r in LLMS and
               client.options(ignore_status=[404]).inference.get(inference_id=LLMS[r][0]).meta.status == 404]
    if missing:
        print(f"Skipping {missing}: their inference endpoints do not exist (check OPENROUTER_API_KEY, rerun setup)")
        chosen = [c for c in chosen if c not in missing]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    print(f"Run {run_id}: {len(messages)} messages, classifiers {chosen}")

    answers = {}
    if "laya" in chosen:
        # laya-serve answers one request at a time, so parallel calls only queue up.
        answers["laya"] = classify_all("laya", ask_laya, messages, workers=1)
    for role in [c for c in chosen if c in LLMS]:
        answers[role] = classify_all(role, lambda text, r=role: ask_llm(LLMS[r][0], text), messages, args.workers)

    # Router: Laya when its probability for every needed answer >= threshold, else the fallback LLM.
    if "laya" in answers and FALLBACK_LLM in answers:
        for t in THRESHOLDS:
            routed = {}
            for m in messages:
                la = answers["laya"].get(m["id"], {"error": "missing"})
                use_laya = "error" not in la and la["min_probability"] >= t
                routed[m["id"]] = {**(la if use_laya else answers[FALLBACK_LLM].get(m["id"], {"error": "missing"})),
                                   "decided_by": "laya" if use_laya else FALLBACK_LLM}
            answers[f"router@{t}"] = routed

    prices = openrouter_prices()
    laya_hourly = float(env("LAYA_HOURLY_USD", "0.10"))
    rows, summary = [], {}
    for name, results in answers.items():
        scored = {mid: score(next(m for m in messages if m["id"] == mid), a) for mid, a in results.items()}
        ok = list(scored.values())
        valid = [a for a in results.values() if "error" not in a]
        latencies = [a.get("latency_ms") for a in valid]
        entry = {
            "messages": len(results),
            "errors": len(results) - len(valid),
            "intent_acc": rate([s["intent_ok"] for s in ok]),
            "category_acc": rate([s["category_ok"] for s in ok]),
            "period_acc": rate([s["period_ok"] for s in ok]),
            "all_correct": rate([s["all_ok"] for s in ok]),
            "flip_rate": flip_rate(messages, results)[0],
            "latency_p50_ms": percentile(latencies, 50),
            "latency_p95_ms": percentile(latencies, 95),
            "avg_input_tokens": statistics.mean([a.get("input_tokens") or 0 for a in valid]) if valid else 0,
            "avg_output_tokens": statistics.mean([a.get("output_tokens") or 0 for a in valid]) if valid else 0,
        }
        if name in LLMS:
            p_in, p_out = prices.get(LLMS[name][2], (0, 0))
            entry["cost_per_1m_usd"] = (entry["avg_input_tokens"] * p_in + entry["avg_output_tokens"] * p_out) * 1e6
            entry["price"] = f"{LLMS[name][2]}: ${p_in * 1e6:.2f} / ${p_out * 1e6:.2f} per 1M tokens in / out"
        elif name == "laya":
            per_hour = 3_600_000 / entry["latency_p50_ms"] if entry["latency_p50_ms"] else 0
            entry["throughput_per_hour"] = per_hour
            entry["cost_per_1m_usd"] = laya_hourly / per_hour * 1e6 if per_hour else float("nan")
            entry["fixed_monthly_usd"] = laya_hourly * HOURS_PER_MONTH
        else:  # router
            escalated = sum(1 for a in results.values() if a.get("decided_by") == FALLBACK_LLM)
            entry["escalation_rate"] = 100.0 * escalated / len(results)
            entry["cost_per_1m_usd"] = None  # filled below, once Laya and the fallback are priced
        summary[name] = entry
        for mid, a in results.items():
            rows.append({"run_id": run_id, "message_id": mid, "classifier": name,
                         **{q: a.get(q) for q in labels.QUESTIONS}, **scored[mid],
                         "min_probability": a.get("min_probability"), "latency_ms": a.get("latency_ms"),
                         "input_tokens": a.get("input_tokens"), "output_tokens": a.get("output_tokens"),
                         "decided_by": a.get("decided_by"), "error": a.get("error"),
                         "@timestamp": datetime.now(timezone.utc).isoformat()})
    for name, entry in summary.items():
        if name.startswith("router@"):
            entry["cost_per_1m_usd"] = summary["laya"]["cost_per_1m_usd"] + entry["escalation_rate"] / 100 * summary[FALLBACK_LLM]["cost_per_1m_usd"]

    # Break-even: monthly decisions at which keeping one Laya machine up costs the same as the LLM.
    if "laya" in summary:
        for role in [r for r in LLMS if r in summary]:
            per_decision = summary[role]["cost_per_1m_usd"] / 1e6
            summary["laya"].setdefault("break_even_monthly", {})[role] = (
                summary["laya"]["fixed_monthly_usd"] / per_decision if per_decision else None)

    OUT.mkdir(exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps({"run_id": run_id, "summary": summary, "laya_hourly_usd": laya_hourly,
                                                  "prices_read": datetime.now(timezone.utc).date().isoformat()}, indent=2))
    with open(OUT / "per_message.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    helpers.bulk(es(), ({"_index": INDEX_RESULTS, "_source": r} for r in rows), refresh="wait_for")
    (OUT / "report.md").write_text(report(run_id, summary, laya_hourly, len(messages)))
    print((OUT / "report.md").read_text())


def fmt(v, digits=1):
    if v is None or v != v:
        return "-"
    return f"{v:,.{digits}f}"


def report(run_id, summary, laya_hourly, n):
    lines = [f"# Benchmark {run_id}", "",
             f"{n} labeled messages. Accuracy in %. Flip rate: share of reworded twins answered differently. "
             f"Cost per 1M decisions in USD: LLMs at OpenRouter list prices read today; Laya at ${laya_hourly:.2f}/hour "
             f"divided by its measured throughput.", "",
             "| Classifier | Intent | Category | Period | All correct | Flip rate | p50 ms | p95 ms | Tokens in/out | Cost per 1M |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, e in summary.items():
        lines.append(f"| {name} | {fmt(e['intent_acc'])} | {fmt(e['category_acc'])} | {fmt(e['period_acc'])} | "
                     f"{fmt(e['all_correct'])} | {fmt(e['flip_rate'])} | {fmt(e['latency_p50_ms'], 0)} | {fmt(e['latency_p95_ms'], 0)} | "
                     f"{fmt(e['avg_input_tokens'], 0)}/{fmt(e['avg_output_tokens'], 0)} | {fmt(e['cost_per_1m_usd'], 2)} |")
    if "laya" in summary:
        laya = summary["laya"]
        lines += ["", f"Laya: about {fmt(laya['throughput_per_hour'], 0)} decisions per hour on one CPU container; "
                      f"keeping one machine up costs about ${fmt(laya['fixed_monthly_usd'], 0)} per month.", ""]
        for role, volume in (laya.get("break_even_monthly") or {}).items():
            lines.append(f"- Break-even against {role}: about {fmt(volume, 0)} decisions per month.")
    routers = {k: v for k, v in summary.items() if k.startswith("router@")}
    if routers:
        lines += ["", "## Router: Laya first, " + FALLBACK_LLM + " when Laya is below the threshold", "",
                  "| Threshold | Escalated to LLM % | All correct % | Cost per 1M |", "|---|---|---|---|"]
        for name, e in routers.items():
            lines.append(f"| {name.split('@')[1]} | {fmt(e['escalation_rate'])} | {fmt(e['all_correct'])} | {fmt(e['cost_per_1m_usd'], 2)} |")
    prices = [e["price"] for e in summary.values() if e.get("price")]
    if prices:
        lines += ["", "Prices: " + "; ".join(prices) + "."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
