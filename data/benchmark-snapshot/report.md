# Benchmark 20261106T143919-9b2b75

200 labeled messages. Accuracy in %. Flip rate: share of reworded twins answered differently. Cost per 1M decisions in USD: LLMs at OpenRouter list prices read today; Laya at $0.10/hour divided by its measured throughput.

| Classifier | Intent | Category | Period | All correct | Flip rate | p50 ms | p95 ms | Tokens in/out | Cost per 1M |
|---|---|---|---|---|---|---|---|---|---|
| laya | 72.0 | 69.8 | 93.9 | 55.0 | 36.7 | 1,297 | 2,218 | 270/0 | 36.01 |
| sonnet | 100.0 | 98.8 | 100.0 | 99.0 | 0.0 | 1,995 | 2,326 | 352/23 | 1,401.27 |
| haiku | 99.5 | 98.2 | 98.8 | 98.0 | 0.0 | 1,997 | 2,254 | 462/27 | 59.85 |
| llama | 38.5 | 95.9 | 98.8 | 37.0 | 23.3 | 2,000 | 2,193 | 330/28 | 18.73 |
| router@0.5 | 93.5 | 91.7 | 97.6 | 86.5 | 6.7 | 1,956 | 2,270 | 400/18 | 76.41 |
| router@0.6 | 99.0 | 96.4 | 100.0 | 96.5 | 6.7 | 1,985 | 2,303 | 435/24 | 87.48 |
| router@0.7 | 99.0 | 97.6 | 100.0 | 97.5 | 3.3 | 1,991 | 2,303 | 450/26 | 92.27 |
| router@0.8 | 99.5 | 98.2 | 98.8 | 98.0 | 0.0 | 1,997 | 2,254 | 461/27 | 95.56 |
| router@0.9 | 99.5 | 98.2 | 98.8 | 98.0 | 0.0 | 1,997 | 2,254 | 462/27 | 95.86 |
| router@0.95 | 99.5 | 98.2 | 98.8 | 98.0 | 0.0 | 1,997 | 2,254 | 462/27 | 95.86 |

Laya: about 2,777 decisions per hour on one CPU container; keeping one machine up costs about $73 per month.

- Break-even against sonnet: about 52,096 decisions per month.
- Break-even against haiku: about 1,219,787 decisions per month.
- Break-even against llama: about 3,896,627 decisions per month.

## Router: Laya first, haiku when Laya is below the threshold

| Threshold | Escalated to LLM % | All correct % | Cost per 1M |
|---|---|---|---|
| 0.5 | 67.5 | 86.5 | 76.41 |
| 0.6 | 86.0 | 96.5 | 87.48 |
| 0.7 | 94.0 | 97.5 | 92.27 |
| 0.8 | 99.5 | 98.0 | 95.56 |
| 0.9 | 100.0 | 98.0 | 95.86 |
| 0.95 | 100.0 | 98.0 | 95.86 |

Prices: anthropic/claude-sonnet-4.6: $3.00 / $15.00 per 1M tokens in / out; anthropic/claude-haiku-5.5: $0.10 / $0.50 per 1M tokens in / out; meta-llama/llama-3.1-8b-instruct: $0.05 / $0.08 per 1M tokens in / out.
