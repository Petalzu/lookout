#!/usr/bin/env python3
"""Replay frozen raw-Git annotations; never derive expectations from analyze()."""
import argparse
import base64
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_cases(manifest):
    cases = []
    for raw in manifest['cases']:
        window = manifest['windows'][raw['window']]
        identity = {key: window[key] for key in ('target', 'start')}
        identity['candidate'] = raw['candidate']
        expected = {**manifest['expectation_defaults'], **raw['expected']}
        ancestry = expected.pop('ancestry_complete')
        proof = expected.pop('strict_manifest', None)
        expected.update(identity=identity, coverage=dict(target=window['target'], start=window['start'],
            limit=manifest['history_limit'], checked=len(window['history']), complete=True, ancestry_complete=ancestry),
            strict_manifest=None if proof is None else dict(version=proof['version'],
                candidate=dict(oid=raw['candidate'], parent_oid=proof['candidate_parent'], files=proof['files']),
                matches=[{**change, 'files': proof['files']} for change in proof['matches']]))
        cases.append({**identity, 'group': raw['group'], 'split': raw['split'], 'repository': window['repository'],
                      'basis': manifest['bases'][raw['basis']], 'expected': expected['status'], 'expected_details': expected})
    return cases


def verify(case, result, limit):
    expected = case['expected_details']
    differences = [f'identity.{key}' for key in ('candidate', 'target', 'start')
                   if expected['identity'].get(key) != case[key]]
    if expected['status'] != case['expected']:
        differences.append('status_annotation')
    for key in ('status', 'reason', 'matches', 'reverts', 'coverage'):
        if result.get(key) != expected[key]:
            differences.append(key)
    if expected['coverage']['limit'] != limit:
        differences.append('history_limit')

    def normalized(value):
        if not isinstance(value, dict):
            return value
        def ordered(change):
            return {**change, 'files': sorted(change['files'],
                key=lambda row: base64.b64decode(row['path_b64'], validate=True))}
        return {**value, 'candidate': ordered(value['candidate']),
                'matches': sorted((ordered(change) for change in value['matches']), key=lambda change: change['oid'])}

    try:
        strict_equal = normalized(result.get('strict_manifest')) == normalized(expected['strict_manifest'])
    except (KeyError, TypeError, ValueError):
        strict_equal = False
    if not strict_equal:
        differences.append('strict_manifest')
    return differences


def source_identity():
    files = sorted((ROOT / 'backend/tracker').rglob('*.py'))
    content = b''.join(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() for path in files)
    revision = subprocess.run(['git', '-c', f'safe.directory={ROOT}', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=10, check=False)
    return {'head': revision.stdout.strip() or None, 'backend_sha256': hashlib.sha256(content).hexdigest()}


def main(argv=None):
    sys.path.insert(0, str(ROOT / 'backend'))
    from tracker.monitors.commits.analysis import analyze
    from tracker.monitors.commits.git import Repository
    from tracker.monitors.commits.model import METHOD_VERSION

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-root', required=True, type=Path)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'backend/tests/fixtures/commits-real-cases.json')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output must not already exist (historical evidence is immutable)')
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    if manifest.get('schema') != 4 or manifest.get('oracle_version') != 'raw-git-full-bytes-v1':
        parser.error('frozen independent raw-Git expectations are required')
    before = source_identity()
    repositories = {name: Repository(args.cache_root / policy['cache'], timeout=90,
                    disk_limit=4 * 1024 * 1024 * 1024) for name, policy in manifest['repositories'].items()}
    results = []
    for index, case in enumerate(load_cases(manifest)):
        repository = repositories[case['repository']]
        repository.begin_round()
        started = time.monotonic()
        result = analyze(repository, case['candidate'], case['target'], history_start=case['start'],
                         history_limit=manifest['history_limit'])
        differences = verify(case, result, manifest['history_limit'])
        results.append({'index': index, 'group': case['group'], 'split': case['split'],
                        'repository': case['repository'], 'identity': case['expected_details']['identity'],
                        'result': result, 'seconds': round(time.monotonic() - started, 6),
                        'differences': differences, 'passed': not differences})
    passed = sum(case['passed'] for case in results)
    after = source_identity()
    report = {'schema': 3, 'source': before, 'source_after': after, 'source_unchanged': before == after,
              'environment': {'python': sys.version, 'platform': platform.platform()},
              'method_version': METHOD_VERSION, 'oracle_version': manifest['oracle_version'],
              'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'history_limit': manifest['history_limit'],
              'annotation_status': manifest['annotation_status'], 'passed': passed, 'total': len(results), 'cases': results}
    with args.output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(report, indent=2) + '\n')
    print(f'{passed}/{len(results)} evidence cases passed; source stable={report["source_unchanged"]}; report: {args.output}')
    return 0 if passed == len(results) and report['source_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
