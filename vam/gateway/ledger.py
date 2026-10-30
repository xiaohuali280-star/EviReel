"""Persistent model usage ledger (E3): reserve before the call, settle after it (PostgreSQL).

* ``reserve`` writes a ``reserved`` row with the token estimate and the price basis used, after checking
  the workspace's daily token budget under a per-workspace advisory lock -- two API instances cannot both
  admit the last remaining tokens.
* ``settle`` records provider-reported usage (``usage_source='provider'``) or, when the provider returned
  no usage, keeps the estimate (``usage_source='estimate'``).
* ``fail`` -- the provider certainly did not bill (refused before sending, connection never established,
  4xx validation error).
* ``unknown`` -- the request may have reached the provider (timeout or disconnect after sending). Unknown
  and expired reservations keep counting against the budget at their estimate until an operator reconciles
  them with the provider's bill (``reconcile`` turns expired reservations into ``unknown``).
Redis rate windows are a throttle only; this table is the record.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from vam.video_registry import DomainError

logger = logging.getLogger(__name__)


def price_basis(settings, model):
    prices = settings.model_prices_map().get(model)
    if not prices:
        return {'source': 'unconfigured', 'model': model}, None
    return {'source': 'MODEL_PRICES', 'model': model, **prices}, prices


def cost_of(prices, input_tokens, output_tokens):
    if not prices:
        return None
    return (Decimal(str(prices.get('input_per_mtok', 0))) * input_tokens
            + Decimal(str(prices.get('output_per_mtok', 0))) * output_tokens) / Decimal(1_000_000)


class NullLedger:
    """Single-user mode without PostgreSQL: counts in memory only (reported, not persisted)."""
    persistent = False

    def __init__(self):
        self.counts = {'reserved': 0, 'settled': 0, 'failed': 0, 'unknown': 0}

    def reserve(self, call):
        self.counts['reserved'] += 1

    def settle(self, call, input_tokens, output_tokens, source):
        self.counts['settled'] += 1

    def fail(self, call, code):
        self.counts['failed'] += 1

    def unknown(self, call, code):
        self.counts['unknown'] += 1

    def reconcile(self):
        return 0


class PgLedger:
    persistent = True

    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def _limit(self, conn, workspace):
        row = conn.execute("SELECT daily_token_limit FROM model_budgets WHERE workspace_id=%s", (workspace,)).fetchone()
        return int(row['daily_token_limit']) if row else int(self.settings.model_daily_tokens_workspace)

    def reserve(self, call):
        basis, prices = price_basis(self.settings, call.model)
        estimate = call.est_input_tokens + call.est_output_tokens
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7033))", (call.workspace,))
            limit = self._limit(conn, call.workspace)
            if limit > 0:
                used = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(input_tokens,0)+COALESCE(output_tokens,0) "
                    "ELSE est_input_tokens+est_output_tokens END), 0) AS n FROM model_usage "
                    "WHERE workspace_id=%s AND status <> 'failed' AND created_at >= date_trunc('day', now())",
                    (call.workspace,)).fetchone()['n']
                if int(used) + estimate > limit:
                    raise DomainError(429, 'model_budget_exceeded', 'The daily model budget of this workspace is used up')
            conn.execute(
                "INSERT INTO model_usage(call_id, workspace_id, provider, model, operation, lane, status, attempt, "
                "est_input_tokens, est_output_tokens, est_cost, price_basis, deadline) VALUES "
                "(%s,%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s,%s, now() + make_interval(secs => %s))",
                (call.call_id, call.workspace, call.provider, call.model, call.operation, call.lane, call.attempt,
                 call.est_input_tokens, call.est_output_tokens,
                 cost_of(prices, call.est_input_tokens, call.est_output_tokens), json.dumps(basis), call.deadline_s))

    def settle(self, call, input_tokens, output_tokens, source):
        _, prices = price_basis(self.settings, call.model)
        with self.db.transaction() as conn:
            # A late settlement still wins over a reconciler's 'unknown' (the provider's own numbers).
            conn.execute("UPDATE model_usage SET status='settled', input_tokens=%s, output_tokens=%s, cost=%s, "
                         "usage_source=%s, settled_at=now() WHERE call_id=%s AND status IN ('reserved','unknown')",
                         (input_tokens, output_tokens, cost_of(prices, input_tokens, output_tokens), source, call.call_id))

    def fail(self, call, code):
        self._close(call, 'failed', code)

    def unknown(self, call, code):
        self._close(call, 'unknown', code)

    def _close(self, call, status, code):
        try:
            with self.db.transaction() as conn:
                conn.execute("UPDATE model_usage SET status=%s, error_code=%s, settled_at=now() "
                             "WHERE call_id=%s AND status='reserved'", (status, code, call.call_id))
        except Exception:
            # The reservation stays 'reserved' and the reconciler turns it into 'unknown' (counted).
            logger.warning('model_usage_close_failed call=%s status=%s', call.call_id, status)

    def reconcile(self):
        with self.db.transaction() as conn:
            rows = conn.execute("UPDATE model_usage SET status='unknown', error_code='reservation_expired' "
                                "WHERE status='reserved' AND deadline < now() RETURNING call_id").fetchall()
        return len(rows)

    def summary(self, workspace=None):
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT workspace_id, provider, model, operation, lane, status, count(*) AS calls, "
                "SUM(est_input_tokens) AS est_in, SUM(est_output_tokens) AS est_out, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(est_cost) AS est_cost, SUM(cost) AS cost FROM model_usage "
                + ("WHERE workspace_id=%s " if workspace else "")
                + "GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4,5,6", ((workspace,) if workspace else ())).fetchall()
        return [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in rows]
