| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| transcript | 56% | 70% | 0.670 | 67% | 100% | 48% | 0.000 | 0.531 / 0.562 | 1.6 | 942 |
| fusion | 19% | 85% | 0.410 | 48% | 100% | 44% | 0.000 | 0.734 / 0.750 | 6.7 | 1103 |
| fusion_keyword | 63% | 81% | 0.735 | 54% | 100% | 44% | 0.000 | 0.781 / 0.797 | 6.7 | 1394 |
| fusion_rerank | 19% | 89% | 0.477 | 48% | 100% | 44% | 0.000 | 0.906 / 0.922 | 7.7 | 1478 |
| deep | 19% | 85% | 0.410 | 45% | 100% | 19% | 0.000 | 1.547 / 1.578 | 7.8 | 3387 |
| auto | 19% | 85% | 0.410 | 48% | 100% | 44% | 0.000 | 0.734 / 0.766 | 6.7 | 1103 |
| auto_keyword | 63% | 81% | 0.735 | 54% | 100% | 44% | 0.000 | 0.781 / 0.797 | 6.7 | 1394 |

models: **simulator** · dataset 32 questions ({'synthetic': 32}) · commit 294366164ecb01058d0e10cd29006ddb12a4794b · top_k 5

Simulator run: the numbers verify the tooling and the routing logic; they are not answer quality.
