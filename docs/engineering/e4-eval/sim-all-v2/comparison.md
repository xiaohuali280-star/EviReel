| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 56% | 70% | 0.670 | 67% | 100% | 48% | 0.000 | 0.594 / 0.625 | 1.6 | 942 |
| fusion | 19% | 85% | 0.410 | 61% | 100% | 44% | 0.000 | 0.766 / 0.828 | 6.5 | 827 |
| fusion_keyword | 63% | 81% | 0.735 | 67% | 100% | 44% | 0.000 | 0.828 / 0.938 | 6.5 | 1063 |
| fusion_rerank | 19% | 89% | 0.477 | 60% | 100% | 44% | 0.000 | 0.938 / 1.000 | 7.5 | 1169 |
| deep | 19% | 85% | 0.410 | 45% | 100% | 19% | 0.000 | 1.609 / 1.656 | 7.8 | 3387 |
| auto | 19% | 85% | 0.410 | 50% | 100% | 19% | 0.000 | 0.812 / 2.031 | 7.3 | 2269 |
| auto_keyword | 63% | 81% | 0.735 | 50% | 100% | 22% | 0.000 | 0.875 / 2.109 | 7.3 | 2694 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 0ed66d918fc81806afff16b3a1c50aaa6384f441 · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
