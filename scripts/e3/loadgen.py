"""E3 load generator for the retrieval path (``POST /api/videos/{id}/search``).

Two load models, reported separately:
  closed  -- C concurrent clients, each sends its next request when the previous one returns (closed loop);
  open    -- requests start at a fixed arrival rate R/s regardless of completions (open loop), bounded by
             --max-inflight so an overloaded server shows up as queueing/timeouts, not as a stalled client.

Videos: ``--same-video`` sends everything to one video (hot video); otherwise requests round-robin over all
seeded videos. Targets: one or more base URLs (round-robin), e.g. two API instances or one load balancer.
Output: one JSON document per run with throughput, latency percentiles, status counts and the server-side
breakdown from ``/__bench__/metrics`` when the target is the bench server.

  python scripts/e3/loadgen.py --url http://127.0.0.1:8890 --mode closed --concurrency 10 --duration 20 --same-video
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import platform
import random
import time

import httpx

QUERIES = ['spanning tree properties', 'what did the teacher say about graph search', 'formula on the slide',
           'overview of the lecture', 'what happened in the event about trees', 'topic 3 explanation']


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))]


async def one(client, url, video, body, results, headers):
    started = time.perf_counter()
    try:
        response = await client.post(f'{url}/api/videos/{video}/search', json=body, headers=headers)
        status = response.status_code
    except httpx.TimeoutException:
        status = 'timeout'
    except httpx.HTTPError as exc:
        status = type(exc).__name__
    results.append((started, time.perf_counter() - started, status))


async def run(args):
    urls = args.url
    headers = {'Authorization': 'Bearer ' + args.token} if args.token else {}
    async with httpx.AsyncClient(timeout=args.timeout, limits=httpx.Limits(max_connections=max(64, args.concurrency * 2))) as client:
        videos = args.video or [v['video_id'] for v in (await client.get(f'{urls[0]}/__bench__/videos')).json()['videos']]
        if args.same_video:
            videos = videos[:1]
        if args.reset:
            for url in urls:
                try:
                    await client.post(f'{url}/__bench__/reset')
                except httpx.HTTPError:
                    pass
        url_cycle, video_cycle, query_cycle = itertools.cycle(urls), itertools.cycle(videos), itertools.cycle(QUERIES)
        body = lambda: {'query': next(query_cycle), 'top_k': 5, 'sources': args.sources}
        results = []
        deadline = time.perf_counter() + args.duration
        started = time.perf_counter()
        if args.mode == 'closed':
            async def worker():
                while time.perf_counter() < deadline:
                    await one(client, next(url_cycle), next(video_cycle), body(), results, headers)
            await asyncio.gather(*[worker() for _ in range(args.concurrency)])
        else:
            inflight, tasks, sent, dropped = asyncio.Semaphore(args.max_inflight), set(), 0, 0
            interval = 1 / args.rate
            next_at = time.perf_counter()
            while next_at < deadline:
                await asyncio.sleep(max(0, next_at - time.perf_counter()))
                next_at += interval
                if inflight.locked():
                    dropped += 1          # the client refuses to exceed max-inflight: counted, not hidden
                    continue
                await inflight.acquire()
                sent += 1
                task = asyncio.create_task(one(client, next(url_cycle), next(video_cycle), body(), results, headers))
                task.add_done_callback(lambda _: inflight.release())
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started
        server = {}
        for url in urls:
            try:
                server[url] = (await client.get(f'{url}/__bench__/metrics')).json()
            except Exception:
                server[url] = None
    ok = [d for _, d, s in results if s == 200]
    statuses = {}
    for _, _, s in results:
        statuses[str(s)] = statuses.get(str(s), 0) + 1
    rejected = sum(n for s, n in statuses.items() if s in ('429', '503'))
    report = {
        'load': {'mode': args.mode, 'concurrency': args.concurrency if args.mode == 'closed' else None,
                 'rate_per_s': args.rate if args.mode == 'open' else None, 'duration_s': args.duration,
                 'same_video': args.same_video, 'videos': len(videos), 'targets': urls, 'sources': args.sources},
        'requests': len(results), 'ok': len(ok), 'statuses': statuses,
        'rejection_rate': round(rejected / len(results), 4) if results else None,
        'throughput_ok_per_s': round(len(ok) / elapsed, 3),
        'latency_ms': {k: round(1000 * v, 1) if v is not None else None for k, v in
                       {'p50': percentile(ok, .5), 'p95': percentile(ok, .95), 'p99': percentile(ok, .99),
                        'max': max(ok) if ok else None}.items()},
        'client': {'python': platform.python_version(), 'host': platform.platform()},
        'server_breakdown': server,
    }
    if args.mode == 'open':
        report['load']['client_dropped_over_max_inflight'] = dropped
        report['load']['max_inflight'] = args.max_inflight
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', action='append', required=True)
    parser.add_argument('--video', action='append')
    parser.add_argument('--mode', choices=['closed', 'open'], default='closed')
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--rate', type=float, default=5.0)
    parser.add_argument('--max-inflight', type=int, default=200)
    parser.add_argument('--duration', type=float, default=20.0)
    parser.add_argument('--timeout', type=float, default=60.0)
    parser.add_argument('--same-video', action='store_true')
    parser.add_argument('--sources', nargs='+', default=['frame', 'event', 'transcript'])
    parser.add_argument('--token', default='')
    parser.add_argument('--reset', action='store_true')
    parser.add_argument('--label', default='')
    parser.add_argument('--out', default='')
    args = parser.parse_args()
    report = asyncio.run(run(args))
    report['label'] = args.label
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write(text)
    print(json.dumps({k: report[k] for k in ('label', 'load', 'requests', 'ok', 'statuses', 'throughput_ok_per_s', 'latency_ms')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
