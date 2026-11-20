import copy


def test_checker_rejects_every_evidence_and_identity_mismatch(checker, cases):
    case = cases['cases'][0]
    result = copy.deepcopy(case['expected_details'])
    assert checker.verify(case, result, cases['history_limit']) == []
    for field, bad in [('status', 'unknown'), ('matches', []), ('reverts', ['d' * 40]),
                       ('coverage', {}), ('reason', 'other'), ('strict_manifest', {})]:
        assert field in checker.verify(case, {**result, field: bad}, cases['history_limit'])
    for key in ('candidate', 'target', 'start'):
        assert 'identity.' + key in checker.verify({**case, key: 'f' * 40}, result, cases['history_limit'])
    assert 'history_limit' in checker.verify(case, result, 1)


def test_checker_verifies_raw_paths_modes_and_hashes_independent_of_array_order(checker, cases):
    case = next(c for c in cases['cases'] if c['expected'] == 'strict_patch_evidence'
                and len(c['expected_details']['strict_manifest']['candidate']['files']) > 1)
    result = copy.deepcopy(case['expected_details'])
    manifest = result['strict_manifest']
    for change in [manifest['candidate'], *manifest['matches']]:
        change['files'].reverse()
    assert checker.verify(case, result, cases['history_limit']) == []
    for field, bad in [('path_b64', 'eA=='), ('before', {}), ('after', {})]:
        broken = copy.deepcopy(result)
        broken['strict_manifest']['matches'][0]['files'][0][field] = bad
        assert 'strict_manifest' in checker.verify(case, broken, cases['history_limit'])


def test_checker_rejects_removed_strict_evidence(checker, cases):
    case = next(c for c in cases['cases'] if c['expected'] == 'strict_patch_evidence')
    result = {**case['expected_details'], 'strict_manifest': None}
    assert 'strict_manifest' in checker.verify(case, result, cases['history_limit'])
