Split: **tune**

| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 43% | 71% | 0.622 | 67% | 100% | 50% | 7.500 | 0.594 / 0.625 | 1.6 | 993 |
| fusion | 29% | 86% | 0.481 | 58% | 100% | 43% | 0.000 | 0.781 / 0.844 | 6.7 | 915 |
| fusion_keyword | 57% | 93% | 0.750 | 71% | 100% | 43% | 0.000 | 0.844 / 0.890 | 6.7 | 1136 |
| fusion_rerank | 29% | 93% | 0.550 | 62% | 100% | 43% | 0.000 | 0.968 / 1.000 | 7.7 | 1261 |
| deep | 29% | 86% | 0.481 | 55% | 100% | 21% | 0.000 | 1.609 / 1.657 | 8.0 | 3443 |
| auto | 29% | 86% | 0.481 | 61% | 100% | 21% | 0.000 | 0.797 / 2.062 | 7.6 | 2487 |
| auto_keyword | 57% | 93% | 0.750 | 62% | 100% | 14% | 0.000 | 0.891 / 2.109 | 7.6 | 2827 |
| agent | 29% | 86% | 0.493 | 59% | 100% | 14% | 0.000 | 3.156 / 3.188 | 11.0 | 22786 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 84a85c6a0e502dc6379f7c511d08f5aff5a97111 · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
