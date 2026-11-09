#!/usr/bin/env python3
"""Replay frozen raw-Git annotations; never derive expectations from analyze()."""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

from commit_case_oracle import ORACLE_VERSION, environment, fingerprint, sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def verify(case, result, limit):
    expected = case['expected_details']
    differences = []
    for key in ('candidate', 'target', 'start'):
        if expected['identity'].get(key) != case[key]:
            differences.append(f'identity.{key}')
    if expected['status'] != case['expected']:
        differences.append('status_annotation')
    for key in ('status', 'reason', 'matches', 'reverts', 'coverage'):
        if result.get(key) != expected[key]:
            differences.append(key)
    if expected['coverage']['limit'] != limit:
        differences.append('history_limit')
    manifest = result.get('strict_manifest')
    if expected['status'] == 'strict_patch_evidence':
        proofs = expected['proofs']

        def change(source):
            return {'oid': source['commit'], 'parent_oid': source['parent'], 'files': [
                {'path_b64': row['path_b64'],
                 **{side: {'mode': row[side + '_mode'], 'blob_oid': row[side]['oid'],
                           'sha256': row[side]['sha256']} for side in ('before', 'after')}}
                for row in source['files']]}

        wanted = {'version': 1, 'candidate': change(proofs[0]['candidate']),
                  'matches': [change(proof['matched']) for proof in proofs]}
        def normalized(value):
            if not isinstance(value, dict):
                return value
            def ordered(change):
                return {**change, 'files': sorted(change['files'],
                    key=lambda row: base64.b64decode(row['path_b64'], validate=True))}
            return {**value, 'candidate': ordered(value['candidate']),
                    'matches': sorted((ordered(change) for change in value['matches']),
                                      key=lambda change: change['oid'])}

        if normalized(manifest) != normalized(wanted):
            differences.append('strict_manifest')
    elif manifest is not None:
        differences.append('unexpected_strict_manifest')
    return differences


def main(argv=None):
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
    if manifest.get('oracle_version') != ORACLE_VERSION or any('expected_details' not in c for c in manifest['cases']):
        parser.error('frozen independent raw-Git expectations are required')
    source_before = fingerprint(ROOT)
    repositories = {name: Repository(args.cache_root / policy['cache'], timeout=90,
                    disk_limit=4 * 1024 * 1024 * 1024) for name, policy in manifest['repositories'].items()}
    results = []
    for index, case in enumerate(manifest['cases']):
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
    source_after = fingerprint(ROOT)
    report = {'schema': 2, 'source': source_before, 'source_after': source_after,
              'source_unchanged': source_before == source_after, 'environment': environment(),
              'method_version': METHOD_VERSION, 'oracle_version': ORACLE_VERSION,
              'manifest_sha256': sha256(raw), 'history_limit': manifest['history_limit'],
              'annotation_status': 'machine_raw_git_not_human_reviewed',
              'passed': passed, 'total': len(results), 'cases': results}
    with args.output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(report, indent=2) + '\n')
    print(f'{passed}/{len(results)} evidence cases passed; source stable={report["source_unchanged"]}; report: {args.output}')
    return 0 if passed == len(results) and report['source_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
