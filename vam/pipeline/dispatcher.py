"""Outbox dispatcher + recovery scanner (its own process).

    python -m vam.pipeline.dispatcher            # loop
    python -m vam.pipeline.dispatcher --once     # one publish + scan pass (cron / tests)

Publishing waits for the broker confirm before marking the outbox row sent; a crash in between
re-publishes (at-least-once). Several dispatchers may run: rows are taken with SKIP LOCKED.
"""
from __future__ import annotations

import argparse
import logging
import time

from vam.config import get_settings
from vam.pipeline.context import fault
from vam.pipeline.repository import JobRepository
from vam.platform.db import PlatformDB

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, settings, app=None):
        from vam.pipeline.celery_app import app as default_app
        self.settings = settings
        db = PlatformDB(settings.database_url)
        db.require_schema()
        self.repo = JobRepository(db, settings)
        self.app = app or default_app
        self.last_scan = 0.0

    def send(self, row):
        fault("dispatcher_before_publish")
        with self.app.producer_or_acquire() as producer:
            self.app.send_task("vam.pipeline.run_stage", args=[row["job_id"], row["stage"]], queue=row["queue"],
                               task_id=row["dedupe_key"], producer=producer, retry=False)
        fault("dispatcher_after_publish")   # broker confirmed, outbox row not yet marked sent

    def once(self) -> dict:
        summary = self.repo.publish_due(self.send)
        if time.monotonic() - self.last_scan >= max(self.settings.pipeline_dispatch_interval_s * 5, 2):
            summary |= self.repo.scan()
            self.last_scan = time.monotonic()
        return summary

    def run(self):
        logger.info("dispatcher_started")
        while True:
            try:
                summary = self.once()
                if any(summary.values()):
                    logger.info("dispatch %s", summary)
            except Exception:
                logger.exception("dispatch_pass_failed")   # DB/broker outage: keep looping, intent is durable
            time.sleep(self.settings.pipeline_dispatch_interval_s)


def main(argv=None):
    parser = argparse.ArgumentParser(description="EviReel E2 outbox dispatcher and recovery scanner")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dispatcher = Dispatcher(get_settings())
    if args.once:
        dispatcher.last_scan = -1e9
        print(dispatcher.once())
    else:
        dispatcher.run()


if __name__ == "__main__":
    main()
