#!/usr/bin/env python3
"""Replay fixed, independently annotated Git cases against prepared offline caches."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main(argv=None):
    from tracker.monitors.commits.analysis import analyze
    from tracker.monitors.commits.git import Repository

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-root', required=True, type=Path)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'backend/tests/fixtures/commits-real-cases.json')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output must not already exist')
    manifest = json.loads(args.manifest.read_text())
    repositories = {name: Repository(args.cache_root / policy['cache'], timeout=60,
                    disk_limit=2 * 1024 * 1024 * 1024) for name, policy in manifest['repositories'].items()}
    results = []
    for case in manifest['cases']:
        repository = repositories[case['repository']]
        repository.deadline = time.monotonic() + 60
        started = time.monotonic()
        result = analyze(repository, case['candidate'], case['target'], history_start=case['start'],
                         history_limit=manifest['history_limit'])
        results.append({**case, 'result': result, 'seconds': round(time.monotonic() - started, 3),
                        'passed': result['status'] == case['expected']})
    args.output.write_text(json.dumps(results, indent=2) + '\n')
    passed = sum(case['passed'] for case in results)
    print(f'{passed}/{len(results)} cases passed; report: {args.output}')
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
