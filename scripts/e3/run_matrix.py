"""Run the E3 scenario matrix against a running bench server and write one JSON file per scenario.

Scenarios (all with the model simulator; see bench_server.py):
  same-c{1,5,10,20}   closed loop, one hot video
  diff-c{1,5,10,20}   closed loop, round-robin over all seeded videos
  open-r{R}           open loop (fixed arrival rate), one hot video
  mixed-c10           closed loop C=10 on the hot video while one client keeps writing derived summaries

  python scripts/e3/run_matrix.py --url http://127.0.0.1:8890 --out docs/engineering/e3-bench/baseline --duration 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import httpx

HERE = Path(__file__).parent


def environment():
    info = {'os': platform.platform(), 'python': platform.python_version(), 'cpu_count': os.cpu_count(),
            'machine': platform.machine(), 'processor': platform.processor()}
    try:
        import psutil
        info['memory_gb'] = round(psutil.virtual_memory().total / 2**30, 1)
    except ImportError:
        pass
    try:
        info['git_commit'] = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True,
                                            cwd=HERE.parents[1]).stdout.strip()
        info['git_dirty'] = bool(subprocess.run(['git', 'status', '--porcelain', 'vam'], capture_output=True, text=True,
                                                cwd=HERE.parents[1]).stdout.strip())
    except OSError:
        pass
    return info


def loadgen(url, out, label, *extra):
    command = [sys.executable, str(HERE / 'loadgen.py'), '--url', url, '--reset', '--label', label, '--out', str(out), *extra]
    subprocess.run(command, check=True)


async def summarize_loop(url, video, stop):
    async with httpx.AsyncClient(timeout=120) as client:
        n = 0
        while not stop.is_set():
            start = (n % 10) * 60.0
            await client.post(f'{url}/__bench__/summarize/{video}', params={'start': start, 'end': start + 60, 'granularity': 30})
            n += 1


def mixed(url, out, duration, video):
    async def main():
        stop = asyncio.Event()
        writer = asyncio.create_task(summarize_loop(url, video, stop))
        proc = await asyncio.create_subprocess_exec(sys.executable, str(HERE / 'loadgen.py'), '--url', url, '--reset',
                                                    '--label', 'mixed-c10', '--out', str(out), '--mode', 'closed',
                                                    '--concurrency', '10', '--duration', str(duration), '--same-video')
        await proc.wait()
        stop.set()
        await writer
    asyncio.run(main())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--duration', type=float, default=20)
    parser.add_argument('--rates', nargs='+', type=float, default=[2, 5])
    parser.add_argument('--only', nargs='*', default=[])
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for _ in range(300):   # wait for the server to listen; the first call also seeds the indexes
        try:
            seeded = httpx.get(f'{args.url}/__bench__/videos', timeout=600).json()
            break
        except httpx.TransportError:
            time.sleep(1)
    else:
        raise SystemExit('bench server did not start')
    (out / 'environment.json').write_text(json.dumps({'environment': environment(), 'bench': seeded['config'],
                                                      'mode': seeded['mode'], 'videos': len(seeded['videos']),
                                                      'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}, indent=1), encoding='utf-8')
    hot = seeded['videos'][0]['video_id']
    plan = [(f'same-c{c}', ['--mode', 'closed', '--concurrency', str(c), '--same-video']) for c in (1, 5, 10, 20)]
    plan += [(f'diff-c{c}', ['--mode', 'closed', '--concurrency', str(c)]) for c in (1, 5, 10, 20)]
    plan += [(f'open-r{r:g}', ['--mode', 'open', '--rate', str(r), '--same-video']) for r in args.rates]
    for label, extra in plan:
        if args.only and label not in args.only:
            continue
        loadgen(args.url, out / f'{label}.json', label, '--duration', str(args.duration), *extra)
    if not args.only or 'mixed-c10' in args.only:
        mixed(args.url, out / 'mixed-c10.json', args.duration, hot)


if __name__ == '__main__':
    main()
