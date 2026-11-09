import copy
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[4] / 'scripts'
SCRIPT_MODULES = ('commit_case_oracle', 'commit_acceptance', '_commits_case_checker')


@contextmanager
def loaded_scripts():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, 'path', sys.path.copy())

        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            patch.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
            return module

        oracle = load('commit_case_oracle', 'commit_case_oracle.py')
        runner = load('commit_acceptance', 'commit_acceptance.py')
        checker = load('_commits_case_checker', 'check-commit-cases.py')
        yield SimpleNamespace(oracle=oracle, runner=runner, checker=checker)


@pytest.fixture
def scripts():
    with loaded_scripts() as modules:
        yield modules


def test_checker_rejects_each_non_status_evidence_mismatch(scripts):
    checker = scripts.checker
    identity = {key: char * 40 for key, char in [('candidate', 'a'), ('target', 'b'), ('start', 'c')]}
    expected = dict(identity=identity, status='original_reachable', reason=None,
                    matches=[identity['candidate']], reverts=[], proofs=[],
                    coverage=dict(target=identity['target'], start=identity['start'], limit=128,
                                  checked=3, complete=True, ancestry_complete=True))
    case = {**identity, 'expected': 'original_reachable', 'expected_details': expected}
    result = {key: value for key, value in expected.items() if key not in ('identity', 'proofs')}
    assert checker.verify(case, result, 128) == []
    for field, bad in [('matches', []), ('reverts', ['d' * 40]), ('coverage', {}), ('reason', 'other')]:
        assert field in checker.verify(case, {**result, field: bad}, 128)
    bad_identity = copy.deepcopy(case)
    bad_identity['candidate'] = 'f' * 40
    assert 'identity.candidate' in checker.verify(bad_identity, result, 128)


def test_projection_preserves_all_evidence_fields(scripts):
    projection = scripts.runner.projection
    snapshot = {'generation': 12, 'commits_records': {'x': 1}, 'commits_scans': {'x': 2},
                'commits_evidence': {'x': {'strict_manifest': {'version': 1}}}, 'commits_batches': {'x': 4}}
    assert projection(snapshot) == {key: value for key, value in snapshot.items() if key != 'generation'}


def test_fixed_fixture_is_deterministic_and_oracle_reads_full_raw_bytes(tmp_path, scripts):
    fixed_fixture = scripts.runner.fixed_fixture
    RawGit = scripts.oracle.RawGit
    first = fixed_fixture(tmp_path / 'first')
    second = fixed_fixture(tmp_path / 'second')
    assert first['oids'] == second['oids']
    raw = RawGit(first['path'])
    case = dict(candidate=first['oids'][3], target=first['initial_tip'], start=first['base'])
    expected = raw.expected(case, 128)
    assert expected['status'] == 'original_reachable'
    assert expected['matches'] == [first['oids'][3]]
    assert expected['coverage']['checked'] == 24
    assert expected['coverage']['complete'] is True
    assert expected['candidate_change']['files'][0]['before']['bytes'] > 0
    assert expected['source']['commands']


def test_manifest_oracle_is_independent_of_application_implementation():
    text = (SCRIPTS / 'commit_case_oracle.py').read_text()
    assert 'from tracker' not in text
    assert 'import tracker' not in text
    assert 'analyze(' not in text


def test_benchmark_minimum_and_immutable_outputs(tmp_path, scripts):
    main = scripts.runner.main
    with pytest.raises(SystemExit):
        main(['--work-root', str(tmp_path / 'work'), '--output', str(tmp_path / 'report.json'), '--repeats', '4'])
    output = tmp_path / 'exists.json'
    output.write_text('{}')
    with pytest.raises(SystemExit):
        main(['--only', 'recovery', '--work-root', str(tmp_path / 'work'), '--output', str(output)])
    assert json.loads(output.read_text()) == {}


def test_isolated_worker_fetches_exact_objects_without_future_target_prefetch(tmp_path, scripts):
    spawn = scripts.runner.spawn
    fixed_fixture = scripts.runner.fixed_fixture
    RawGit = scripts.oracle.RawGit
    fixture = fixed_fixture(tmp_path / 'origin')
    outcome = spawn(tmp_path / 'worker', fixture, scenario='first_scan', cache='cold', benchmark=True)
    assert outcome['returncode'] == 0, (tmp_path / 'worker' / 'process-0.log').read_text()
    result = outcome['result']
    assert result['projection']['commits_scans']['fixture']['active']['tip'] == fixture['initial_tip']
    assert result['metrics']['scanned_objects'] == 24
    cache = RawGit(tmp_path / 'worker' / 'git-cache')
    assert cache.run('cat-file', '-e', fixture['append_tip'], allowed=(0, 1, 128))[1] != 0
    assert result['metrics']['git_cache_delta_bytes'] > 0


def test_raw_patch_id_uses_parent_header_even_at_shallow_boundary(tmp_path, scripts):
    fixed_fixture = scripts.runner.fixed_fixture
    RawGit = scripts.oracle.RawGit
    fixture = fixed_fixture(tmp_path / 'origin')
    raw = RawGit(fixture['path'])
    candidate = fixture['oids'][3]
    normal = raw.patch_id(candidate)
    assert normal is not None
    (Path(fixture['path']) / '.git' / 'shallow').write_text(candidate + '\n')
    bounded = RawGit(fixture['path'])
    assert bounded.text('rev-list', '--parents', '-n', '1', candidate) == candidate
    assert bounded.patch_id(candidate) == normal


def test_raw_manifest_sorts_original_path_bytes_not_base64_text(tmp_path, scripts):
    import base64

    fixed_fixture = scripts.runner.fixed_fixture
    RawGit = scripts.oracle.RawGit

    fixture = fixed_fixture(tmp_path / 'origin')
    path = Path(fixture['path'])
    for name in ('aa0', 'aa?'):
        (path / name).write_text('full byte content\n')
    raw = RawGit(path)
    raw.run('add', '.')
    raw.run('commit', '-q', '-m', 'path byte order control')
    evidence = raw.file_evidence(raw.text('rev-parse', 'HEAD'))
    assert [base64.b64decode(row['path_b64']) for row in evidence['files']] == [b'aa0', b'aa?']


def test_script_module_context_restores_existing_modules_and_path():
    original_path = sys.path
    original_values = list(sys.path)
    with pytest.MonkeyPatch.context() as patch:
        sentinels = {name: ModuleType(name) for name in SCRIPT_MODULES}
        for name, module in sentinels.items():
            patch.setitem(sys.modules, name, module)
        for fail in (False, True):
            try:
                with loaded_scripts() as scripts:
                    assert sys.path is not original_path
                    assert scripts.runner is not sentinels['commit_acceptance']
                    assert scripts.oracle is not sentinels['commit_case_oracle']
                    if fail:
                        raise RuntimeError('test import-context cleanup')
            except RuntimeError as error:
                assert fail and str(error) == 'test import-context cleanup'
            assert sys.path is original_path
            assert sys.path == original_values
            assert all(sys.modules[name] is module for name, module in sentinels.items())


def test_checker_accepts_equivalent_raw_file_order(tmp_path, scripts):
    from tracker.monitors.commits.analysis import analyze
    from tracker.monitors.commits.git import Repository

    fixture = scripts.runner.fixed_fixture(tmp_path / 'origin')
    path = Path(fixture['path'])
    raw = scripts.oracle.RawGit(path)
    base = fixture['base']
    raw.run('checkout', '-q', '-b', 'raw-path-candidate', base)
    for name in ('aa0', 'aa?'):
        (path / name).write_text('full byte content\n')
    raw.run('add', '.')
    raw.run('commit', '-q', '-m', 'two-file candidate')
    candidate = raw.text('rev-parse', 'HEAD')
    raw.run('checkout', '-q', '-b', 'raw-path-backport', base)
    (path / 'unrelated').write_text('unrelated\n')
    raw.run('add', '.')
    raw.run('commit', '-q', '-m', 'different parent')
    raw.run('cherry-pick', '-x', candidate)
    case = {'candidate': candidate, 'target': raw.text('rev-parse', 'HEAD'), 'start': base}
    case['expected_details'] = scripts.oracle.RawGit(path).expected(case, 128)
    case['expected'] = 'strict_patch_evidence'
    result = analyze(Repository(path), candidate, case['target'], history_start=base, history_limit=128)
    assert result['status'] == 'strict_patch_evidence'
    assert scripts.checker.verify(case, result, 128) == []


def test_independent_fixture_rejects_successful_empty_matcher(tmp_path, scripts, monkeypatch):
    from tracker.monitors.commits import collector
    from tracker.monitors.commits.git import Repository

    fixture = scripts.runner.fixed_fixture(tmp_path / 'origin')
    config, snapshot = scripts.runner.config_snapshot(fixture)
    raw = scripts.oracle.RawGit(fixture['path'])
    raw.run('update-ref', 'refs/heads/acceptance', fixture['initial_tip'])
    repo = Repository(fixture['path'])
    policy = collector.settings(config)
    ids = fixture['oids'][1:25]
    scan = {'ids': ids, 'cursor': len(ids), 'complete': True, 'history_complete': True,
            'start': fixture['base'], 'tip': fixture['initial_tip'], 'observed_at': scripts.runner.FIXED_AT}
    records = {oid: repo.read_commit(oid) for oid in ids}
    monkeypatch.setattr(collector, 'match_rules', lambda *args, **kwargs: [])
    progress = None
    for _ in range(8):
        batch, _, progress = collector.build_batch(config, snapshot, 'binutils',
            collector.package_policies(config)['binutils'], policy, scan, records, {}, repo,
            scripts.runner.FIXED_AT, progress)
    assert batch['status'] == 'ok' and batch['total'] == 0
    broken = {'commits_scans': {'fixture': {'active': scan}},
              'commits_batches': {'binutils': {'active': batch}}}
    with pytest.raises(AssertionError, match='candidate OID'):
        scripts.runner.verify_fixture(broken, fixture, fixture['initial_tip'])


@pytest.mark.parametrize('inconsistent', [True, False])
def test_label_failure_cannot_pass_top_level_acceptance(tmp_path, scripts, monkeypatch, inconsistent):
    runner = scripts.runner
    row = {'technical_scope_label': True, 'machine_prediction': False}
    evaluation = {'tp': 0, 'fp': 0, 'fn': 1, 'tn': 0, 'denominator': 1}
    if inconsistent:
        evaluation['fn'] = 0
    monkeypatch.setattr(runner, 'labels', lambda *args: [
        {'rows': [row], 'complete_window_count': 1, 'technical_evaluation': evaluation}])
    monkeypatch.setattr(runner, 'limits', lambda: None)
    monkeypatch.setattr(runner, 'fingerprint', lambda root: {'fixed': True})
    output = tmp_path / 'labels.json'
    assert runner.main(['--only', 'labels', '--cache-root', str(tmp_path),
        '--work-root', str(tmp_path / 'work'), '--output', str(output)]) == 1
    assert json.loads(output.read_text())['labels_consistent'] is False
