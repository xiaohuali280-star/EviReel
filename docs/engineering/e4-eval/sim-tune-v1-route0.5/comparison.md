| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| auto | 29% | 86% | 0.481 | 44% | 100% | 71% | 27.500 | 0.344 / 1.578 | 6.6 | 975 |
| auto_keyword | 57% | 93% | 0.750 | 33% | 100% | 71% | 0.000 | 0.390 / 1.593 | 6.6 | 993 |

models: **simulator** · dataset 17 questions ({'synthetic': 17}) · commit 294366164ecb01058d0e10cd29006ddb12a4794b · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
