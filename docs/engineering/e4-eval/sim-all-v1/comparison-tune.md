Split: **tune**

| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 43% | 71% | 0.622 | 67% | 100% | 50% | 7.500 | 0.532 / 0.562 | 1.6 | 993 |
| fusion | 29% | 86% | 0.481 | 50% | 100% | 43% | 0.000 | 0.734 / 0.765 | 6.9 | 1178 |
| fusion_keyword | 57% | 93% | 0.750 | 61% | 100% | 43% | 0.000 | 0.781 / 0.813 | 6.9 | 1421 |
| fusion_rerank | 29% | 93% | 0.550 | 53% | 100% | 43% | 0.000 | 0.906 / 0.938 | 7.9 | 1556 |
| deep | 29% | 86% | 0.481 | 55% | 100% | 21% | 0.000 | 1.547 / 1.578 | 8.0 | 3443 |
| auto | 29% | 86% | 0.481 | 50% | 100% | 43% | 0.000 | 0.735 / 0.766 | 6.9 | 1178 |
| auto_keyword | 57% | 93% | 0.750 | 61% | 100% | 43% | 0.000 | 0.781 / 0.797 | 6.9 | 1421 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 294366164ecb01058d0e10cd29006ddb12a4794b · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
