import base64
import json
import re
from collections import defaultdict
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[2] / 'fixtures/commits-real-cases.json'


def test_fixed_real_cases_have_disjoint_groups_and_full_identity():
    data = json.loads(MANIFEST.read_text())
    assert data['schema'] == 2
    assert data['oracle_version'] == 'raw-git-full-bytes-v1'
    assert data['annotation_status'] == 'machine_raw_git_not_human_reviewed'
    assert len(data['cases']) >= 30
    assert sum(case['split'] == 'independent' for case in data['cases']) >= 10
    groups = defaultdict(set)
    for case in data['cases']:
        groups[case['group']].add(case['split'])
        assert case['repository'] in data['repositories']
        assert all(re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', case[key])
                   for key in ('candidate', 'target', 'start'))
        assert case['basis']
    assert all(len(splits) == 1 for splits in groups.values())
    assert {case['repository'] for case in data['cases']} == {'qemu', 'llvm'}
    assert sum(c['repository'] == 'llvm' for c in data['cases']) >= 6


def test_raw_git_expectations_bind_all_evidence_and_nonempty_negatives():
    data = json.loads(MANIFEST.read_text())
    for case in data['cases']:
        expected = case['expected_details']
        assert expected['identity'] == {key: case[key] for key in ('candidate', 'target', 'start')}
        assert expected['status'] == case['expected']
        assert {'reason', 'matches', 'reverts', 'coverage', 'proofs', 'source'} <= expected.keys()
        coverage = expected['coverage']
        assert coverage == dict(target=case['target'], start=case['start'], limit=data['history_limit'],
                                checked=len(expected['source']['history']), complete=True,
                                ancestry_complete=coverage['ancestry_complete'])
        source = expected['source']
        assert source['method'] == data['oracle_version']
        assert source['label_kind'] == data['annotation_status']
        assert re.fullmatch('[a-f0-9]{64}', source['commands_sha256'])
        assert source['command_count'] > 0
        assert source['repository_url'] == data['repositories'][case['repository']]['url']
        assert 'analyze' not in source['reproduction']
        if case['expected'] == 'no_evidence_in_range':
            assert case['start'] != case['target']
            assert coverage['checked'] > 0
            assert expected['matches'] == expected['reverts'] == []
        for proof in expected['proofs']:
            assert proof['full_bytes_equal'] is True
            assert proof['candidate']['commit'] == case['candidate']
            assert proof['matched']['commit'] in expected['matches']
            assert proof['candidate']['files'] == proof['matched']['files']
            for row in proof['candidate']['files']:
                assert base64.b64encode(base64.b64decode(row['path_b64'], validate=True)).decode() == row['path_b64']
                for side in ('before', 'after'):
                    blob = row[side]
                    if row[side + '_mode'] == '000000':
                        assert blob == dict(oid=None, sha256=None, bytes=None)
                    else:
                        assert re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', blob['oid'])
                        assert re.fullmatch('[a-f0-9]{64}', blob['sha256'])
                        assert blob['bytes'] >= 0


def test_llvm_real_riscv_backport_is_conservative_and_in_holdout_group():
    data = json.loads(MANIFEST.read_text())
    case = next(c for c in data['cases'] if c['candidate'] == '589faedadf141e5e63f7a1e92a0327fc9bdc9b09')
    assert case['repository'] == 'llvm'
    assert case['split'] == 'independent'
    assert case['expected'] == 'backport_needs_review'
    assert case['expected_details']['matches'] == ['3397c37d5d31e4d4cf1226ba1c36824aa2287252']
    assert case['expected_details']['proofs'] == []
    assert case['expected_details']['association_changes']
