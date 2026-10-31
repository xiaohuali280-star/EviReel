| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 56% | 70% | 0.670 | 67% | 100% | 48% | 0.000 | 0.594 / 0.625 | 1.6 | 942 |
| fusion | 19% | 85% | 0.410 | 61% | 100% | 44% | 0.000 | 0.781 / 0.812 | 6.5 | 827 |
| fusion_keyword | 63% | 81% | 0.735 | 67% | 100% | 44% | 0.000 | 0.844 / 0.875 | 6.5 | 1063 |
| fusion_rerank | 19% | 89% | 0.477 | 60% | 100% | 44% | 0.000 | 0.953 / 0.985 | 7.5 | 1169 |
| deep | 19% | 85% | 0.410 | 45% | 100% | 19% | 0.000 | 1.609 / 1.657 | 7.8 | 3387 |
| auto | 19% | 85% | 0.410 | 50% | 100% | 19% | 0.000 | 0.797 / 2.032 | 7.3 | 2269 |
| auto_keyword | 63% | 81% | 0.735 | 50% | 100% | 22% | 0.000 | 0.875 / 2.109 | 7.3 | 2694 |
| agent | 19% | 85% | 0.417 | 45% | 100% | 15% | 0.000 | 3.156 / 3.188 | 10.8 | 22726 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 84a85c6a0e502dc6379f7c511d08f5aff5a97111 · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
