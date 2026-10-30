"""CLI: see vam/evaluation/__init__.py."""
from __future__ import annotations

import argparse
import json

from vam.evaluation import dataset as ds


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m vam.evaluation')
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check', help='validate a question set (and its fixture references)')
    check.add_argument('--dataset', required=True)
    check.add_argument('--fixture')
    run = sub.add_parser('run', help='run experiment configs and write results')
    run.add_argument('--dataset', required=True)
    run.add_argument('--out', required=True)
    run.add_argument('--configs', default='all', help='comma separated, or "all"')
    run.add_argument('--models', choices=['simulator', 'real'], default='simulator')
    run.add_argument('--fixture')
    run.add_argument('--data-root')
    run.add_argument('--split', choices=['all', 'tune', 'holdout'], default='all')
    run.add_argument('--top-k', type=int, default=5)
    run.add_argument('--followup', choices=['raw', 'history'], default='history')
    run.add_argument('--repeat', type=int, default=1)
    run.add_argument('--allow-paid', action='store_true')
    run.add_argument('--max-calls', type=int)
    run.add_argument('--max-tokens', type=int)
    run.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                     help='settings override, e.g. --set route_min_similarity=0.4')
    args = parser.parse_args(argv)
    if args.command == 'check':
        questions = ds.load(args.dataset)
        print(json.dumps(ds.describe(questions), ensure_ascii=False, indent=1))
        if args.fixture:
            from vam.evaluation.fixture import check_references
            problems = check_references(args.fixture, questions)
            print('\n'.join(problems) or 'references match the fixture')
            return 1 if problems else 0
        return 0
    from vam.evaluation.runner import run as run_eval
    overrides = {}
    for item in args.set:
        key, value = item.split('=', 1)
        try:
            overrides[key] = json.loads(value)
        except ValueError:
            overrides[key] = value
    summaries, metadata = run_eval(dataset_path=args.dataset, out=args.out, configs=args.configs.split(','), models=args.models,
                                   fixture=args.fixture, data_root=args.data_root, split=args.split, top_k=args.top_k,
                                   followup=args.followup, repeat=args.repeat, allow_paid=args.allow_paid,
                                   max_calls=args.max_calls, max_tokens=args.max_tokens, overrides=overrides)
    print((open(f'{args.out}/comparison.md', encoding='utf-8').read()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
