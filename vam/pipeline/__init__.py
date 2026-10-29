"""E2 durable processing pipeline.

PostgreSQL (``vam.pipeline.repository``) is the execution ledger; the outbox dispatcher
(``vam.pipeline.dispatcher``) publishes "run stage S of job J" to RabbitMQ; Celery workers
(``vam.pipeline.celery_app``) claim stages with leases and attempt tokens and commit results
conditionally (``vam.pipeline.runner``). Stage bodies live in ``vam.pipeline.stages``.
See docs/engineering/E2_DELIVERY.md.
"""
