"""Celery wiring for stage workers (Linux/containers; Celery does not support native Windows workers).

    celery -A vam.pipeline.celery_app worker -Q media,asr,embed,control --concurrency 1

Delivery semantics actually relied on (verified in tests/test_e2_faults.py, not assumed):
* task_acks_late: the message is ACKed only after execute() returned, i.e. after the ledger commit.
  A worker killed mid-stage leaves the message unacked -> RabbitMQ redelivers it.
* task_reject_on_worker_lost: a pool child killed by a signal requeues instead of acking.
* worker_prefetch_multiplier=1: a worker holds at most one unacked message per process, so a long
  stage does not hoard others; long and short work use separate queues/workers.
* No Celery-level retries: retry/backoff/attempt limits live in PostgreSQL (vam.pipeline.repository).
* RabbitMQ consumer_timeout must exceed the longest stage (deploy/e2/rabbitmq.conf), otherwise the
  broker closes the channel of a legitimately long unacked delivery.
Redelivery can duplicate work but never duplicates effects: execute() claims with a lease and commits
with the attempt token. The queue does not and cannot provide business exactly-once execution.
"""
from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_process_init
from kombu import Queue

from vam.config import get_settings
from vam.pipeline.graph import ALL_QUEUES

logger = logging.getLogger(__name__)


def make_app(settings=None) -> Celery:
    settings = settings or get_settings()
    app = Celery("evireel", broker=settings.broker_url or "amqp://localhost//")
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=True,   # execute() records failures itself; never requeue forever
        worker_prefetch_multiplier=1,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        task_time_limit=settings.pipeline_task_time_limit_s,
        # Soft limit first (raises inside the task -> recorded as retryable "stage_timeout"); the
        # hard limit kills the pool child as a backstop (lease expiry then re-dispatches).
        task_soft_time_limit=settings.pipeline_task_time_limit_s - max(5, min(60, settings.pipeline_task_time_limit_s // 10)),
        task_ignore_result=True,
        task_serializer="json", accept_content=["json"],
        task_queues=[Queue(name, durable=True) for name in ALL_QUEUES],
        task_default_queue="control",
        task_create_missing_queues=False,
        broker_connection_retry_on_startup=True,
        # Publisher confirms: send_task raises unless RabbitMQ confirmed the (persistent) message.
        broker_transport_options={"confirm_publish": True},
        task_publish_retry=False,
        worker_hijack_root_logger=False,
    )
    return app


app = make_app()
_context = None


@worker_process_init.connect
def _reset_context(**_):
    global _context
    _context = None   # each pool process builds its own DB/S3 clients after fork


def context():
    global _context
    if _context is None:
        from vam.pipeline.context import WorkerContext
        _context = WorkerContext(get_settings())
    return _context


@app.task(name="vam.pipeline.run_stage", acks_late=True, reject_on_worker_lost=True)
def run_stage(job_id: str, stage: str) -> str:
    from vam.pipeline.runner import execute
    return execute(context(), job_id, stage)
