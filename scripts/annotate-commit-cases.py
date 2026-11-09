#!/usr/bin/env python3
"""Freeze technical raw-Git labels, not human semantic relevance or review approval."""
import argparse
import json
import sys
from pathlib import Path

from commit_case_oracle import ORACLE_VERSION, RawGit, environment, fingerprint, sha256

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence-output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.evidence_output.exists():
        parser.error('outputs must be new paths; review and replace the manifest separately')
    data = json.loads(args.manifest.read_text())
    repositories = {name: RawGit(args.cache_root / policy['cache']) for name, policy in data['repositories'].items()}
    evidence = {'schema': 1, 'oracle_version': ORACLE_VERSION, 'source': fingerprint(ROOT),
                'environment': environment(), 'cases': []}
    for index, case in enumerate(data['cases']):
        expected = repositories[case['repository']].expected(case, data['history_limit'])
        commands = expected['source'].pop('commands')
        expected['source']['command_count'] = len(commands)
        expected['source']['commands_sha256'] = sha256(json.dumps(commands, sort_keys=True).encode())
        expected['source']['reproduction'] = 'python scripts/annotate-commit-cases.py --cache-root CACHE --manifest MANIFEST --output NEW_MANIFEST --evidence-output NEW_REPORT'
        expected['source']['repository_url'] = data['repositories'][case['repository']]['url']
        evidence['cases'].append({'index': index, 'group': case['group'], 'commands': commands})
        if case['expected'] != expected['status']:
            raise ValueError(f'case {index}: prior annotation {case["expected"]} disagrees with raw Git {expected["status"]}')
        case['expected_details'] = expected
    data.update(schema=2, oracle_version=ORACLE_VERSION,
                annotation_status='machine_raw_git_not_human_reviewed',
                split_policy='modification-group disjoint; technical holdout, not blinded human ground truth')
    with args.evidence_output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(evidence, indent=2) + '\n')
    data['annotation_evidence'] = {'filename': args.evidence_output.name,
                                   'sha256': sha256(args.evidence_output.read_bytes())}
    with args.output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(data, indent=2) + '\n')
    print(f'Frozen {len(data["cases"])} raw-Git cases; no analyze import or human review claim')
    return 0


if __name__ == '__main__':
    sys.exit(main())
