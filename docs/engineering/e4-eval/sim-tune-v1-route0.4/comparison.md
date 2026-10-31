| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| auto | 29% | 86% | 0.481 | 55% | 100% | 57% | 0.000 | 0.734 / 1.547 | 6.6 | 907 |
| auto_keyword | 57% | 93% | 0.750 | 54% | 100% | 57% | 0.000 | 0.766 / 1.593 | 6.6 | 1011 |

models: **simulator** · dataset 17 questions ({'synthetic': 17}) · commit 294366164ecb01058d0e10cd29006ddb12a4794b · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
