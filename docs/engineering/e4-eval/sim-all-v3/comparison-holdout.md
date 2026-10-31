Split: **holdout**

| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 69% | 69% | 0.721 | 67% | 100% | 46% | 0.000 | 0.594 / 0.625 | 1.6 | 884 |
| fusion | 8% | 85% | 0.333 | 64% | 100% | 46% | 15.000 | 0.781 / 0.797 | 6.2 | 727 |
| fusion_keyword | 69% | 69% | 0.718 | 62% | 100% | 46% | 0.000 | 0.844 / 0.875 | 6.2 | 981 |
| fusion_rerank | 8% | 85% | 0.397 | 58% | 100% | 46% | 0.000 | 0.953 / 0.985 | 7.2 | 1066 |
| deep | 8% | 85% | 0.333 | 35% | 100% | 15% | 15.000 | 1.609 / 1.672 | 7.6 | 3323 |
| auto | 8% | 85% | 0.333 | 39% | 100% | 15% | 15.000 | 0.797 / 2.015 | 7.0 | 2022 |
| auto_keyword | 69% | 69% | 0.718 | 37% | 100% | 31% | 10.000 | 0.875 / 2.094 | 7.0 | 2543 |
| agent | 8% | 85% | 0.335 | 32% | 100% | 15% | 30.000 | 3.140 / 3.188 | 10.6 | 22657 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 84a85c6a0e502dc6379f7c511d08f5aff5a97111 · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
