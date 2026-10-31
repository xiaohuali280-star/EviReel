Split: **holdout**

| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 69% | 69% | 0.721 | 67% | 100% | 46% | 0.000 | 0.531 / 0.562 | 1.6 | 884 |
| fusion | 8% | 85% | 0.333 | 47% | 100% | 46% | 10.000 | 0.719 / 0.750 | 6.5 | 1017 |
| fusion_keyword | 69% | 69% | 0.718 | 47% | 100% | 46% | 0.000 | 0.781 / 0.797 | 6.5 | 1364 |
| fusion_rerank | 8% | 85% | 0.397 | 44% | 100% | 46% | 0.000 | 0.891 / 0.922 | 7.5 | 1390 |
| deep | 8% | 85% | 0.333 | 35% | 100% | 15% | 15.000 | 1.547 / 1.578 | 7.6 | 3323 |
| auto | 8% | 85% | 0.333 | 47% | 100% | 46% | 10.000 | 0.734 / 0.750 | 6.5 | 1017 |
| auto_keyword | 69% | 69% | 0.718 | 47% | 100% | 46% | 0.000 | 0.781 / 0.797 | 6.5 | 1364 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 294366164ecb01058d0e10cd29006ddb12a4794b · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
