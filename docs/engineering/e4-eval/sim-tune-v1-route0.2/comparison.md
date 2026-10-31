| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| auto | 29% | 86% | 0.481 | 50% | 100% | 43% | 0.000 | 0.735 / 0.765 | 6.9 | 1178 |
| auto_keyword | 57% | 93% | 0.750 | 61% | 100% | 43% | 0.000 | 0.781 / 0.812 | 6.9 | 1421 |

models: **simulator** · dataset 17 questions ({'synthetic': 17}) · commit 294366164ecb01058d0e10cd29006ddb12a4794b · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
