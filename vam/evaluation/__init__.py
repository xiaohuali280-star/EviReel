"""E4 evaluation: a reproducible question set, metrics, a runner and comparable experiment configs.

    python -m vam.evaluation run --dataset eval/synthetic/questions.jsonl --fixture eval/synthetic/fixture.json \
        --models simulator --configs all --out docs/engineering/e4-eval/<run>
    python -m vam.evaluation run --dataset <real.jsonl> --data-root <DATA_ROOT> --models real \
        --allow-paid --max-calls 200 --max-tokens 400000 --configs transcript,fusion --out <dir>

Two model modes, never mixed in one run and always written into the run metadata:

* ``simulator`` -- deterministic, offline (vam.evaluation.simulator): lexical embeddings, an overlap reranker,
  a caption-reading "VLM" and an extractive "LLM". It exercises the real retrieval, fusion, versions, routing,
  ModelGateway accounting and the metrics. Its scores say NOTHING about real answer quality.
* ``real`` -- the configured providers through ModelGateway; refused unless ``--allow-paid`` with explicit
  call/token caps. It runs on the API's platform wiring (vam.server.assembly: PostgreSQL read leases, shared
  Redis rate windows, the persistent usage ledger and workspace budgets, object restore); the run's ledger
  adds per-question attribution and the caps on top. Without DATABASE_URL it refuses unless ``--standalone``.

Configs compare one change at a time; ``agent`` runs the product's multi-turn tool-calling loop on the same
questions, versions and usage records (bounded turns, candidates, deadline and budget). A config whose
questions end in errors (budget exhausted, timeout, failure) is reported as incomplete, never as finished.

Labels carry their origin (``label.source``: human | synthetic | model); summaries report each separately.
"""
