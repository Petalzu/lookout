import json
import re
from collections import defaultdict
from pathlib import Path


def test_fixed_real_cases_have_disjoint_groups_and_full_identity():
    path = Path(__file__).resolve().parents[2] / 'fixtures/commits-real-cases.json'
    data = json.loads(path.read_text())
    assert len(data['cases']) >= 20
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
