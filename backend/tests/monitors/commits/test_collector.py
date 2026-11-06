import subprocess

import pytest

from tracker import state
from tracker.monitors.commits.collector import collect
from tracker.monitors.commits.git import Repository
from tracker.monitors.commits.model import settings
from tracker.monitors.commits.scan import advance


def git(path, *args):
    return subprocess.check_output(['git', '-c', f'safe.directory={path}', '-C', str(path), *args],
                                   stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def package(config, snapshot, tmp_path, monkeypatch):
    repo_path = tmp_path / 'upstream'
    repo_path.mkdir()
    git(repo_path, 'init', '-qb', 'main')
    git(repo_path, 'config', 'user.name', 'Fixture')
    git(repo_path, 'config', 'user.email', 'fixture@example.org')
    (repo_path / 'file').write_text('one\n')
    git(repo_path, 'add', '.')
    git(repo_path, 'commit', '-qm', 'base')
    base = git(repo_path, 'rev-parse', 'HEAD')
    (repo_path / 'file').write_text('two\n')
    git(repo_path, 'commit', '-qam', 'riscv: fix')
    fix = git(repo_path, 'rev-parse', 'HEAD')
    snapshot['specs'] = {'binutils': dict(head=base, fetched_at=state.utcnow(), error=None,
        source_origin={'url': 'https://example.org/spec', 'branch': 'main'}, native_query=dict(spec_sha256='a' * 64, context={'resolver': 6}),
        metadata=dict(version='3.9.0', sources=[{'number': 0, 'url': 'https://example.org/src.tar.xz'}]))}
    revision = state.current_source(snapshot, 'binutils')['revision']
    config['commits'] = dict(repositories={'fixture': dict(url='https://example.org/repo.git',
        web_url='https://example.org/repo', ref='refs/heads/main', window_start=base)}, batch_size=1)
    config['packages']['binutils'] = {'monitors': {'commits': dict(repository='fixture',
        rules=[{'id': 'riscv', 'keywords': ['riscv']}], mappings={'3.9.0': dict(oid=fix,
            source_revision=revision, source_url='https://example.org/src.tar.xz',
            review_url='https://example.org/review')})}}
    monkeypatch.setattr('tracker.monitors.commits.collector.cfg.require_unchanged', lambda *args: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    return config, db, Repository(repo_path), base, fix


def test_fixed_manifest_resumes_same_tip(package):
    config, _, repo, base, fix = package
    policy = settings(config).repositories['fixture']
    scan, records = advance(repo, policy, {}, {}, state.utcnow(), 0, fetch=False)
    assert not scan['complete'] and scan['cursor'] == 0
    (repo.path / 'other').write_text('next')
    git(repo.path, 'add', '.')
    git(repo.path, 'commit', '-qm', 'riscv: next round')
    resumed, records = advance(repo, policy, scan, records, state.utcnow(), 1, fetch=False)
    assert resumed['tip'] == fix and resumed['ids'] == [fix]
    assert resumed['complete'] and resumed['last_complete_tip'] == fix
    next_scan, records = advance(repo, policy, resumed, records, state.utcnow(), 1, fetch=False)
    assert next_scan['ids'][0] == fix and len(next_scan['ids']) == 2
    assert next_scan['complete']
    assert next_scan['cursor'] == 2


def test_real_collector_storage_and_unknown_upgrade(package):
    config, db, repo, _, fix = package
    result = collect(config, db, repositories={'fixture': repo})
    assert state.read(db) == result
    batch = result['commits_batches']['binutils']['active']
    assert batch['source_identity']['name'] == 'binutils'
    assert batch['selected'] == batch['total'] == 1
    current = batch['entries'][0]['sides']['current']
    assert current['status'] == 'original_reachable'
    assert current['target']['oid'] == fix
    assert current['checked_at'] is not None
    assert batch['entries'][0]['sides']['upgrade']['reason'] == 'no_candidate_release'


def test_same_evidence_keeps_source_time(package):
    config, db, repo, _, _ = package
    first = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:00:00+00:00')
    saved = first['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    second = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:01:00+00:00')
    assert second['commits_batches']['binutils']['active']['entries'][0]['sides']['current']['checked_at'] == saved['checked_at']


def test_phase_rejects_in_flight_source_change(package, monkeypatch):
    config, db, repo, _, _ = package
    original = repo.read_commit
    changed = False

    def change(value):
        nonlocal changed
        if not changed:
            changed = True
            latest = state.read(db)
            latest['specs']['binutils']['head'] = 'f' * 40
            state.commit(db, latest)
        return original(value)

    monkeypatch.setattr(repo, 'read_commit', change)
    result = collect(config, db, repositories={'fixture': repo})
    assert 'active' not in result['commits_batches']['binutils']


def test_saved_results_reach_typed_api_and_document(package):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app
    from tracker.readmodel import commits as view

    config, db, repo, _, fix = package
    result = collect(config, db, repositories={'fixture': repo})
    assert view.page(result, 'binutils')['items'][0]['oid'] == fix
    with ProjectedClient(create_app(db)) as client:
        response = client.get('/api/v2/packages/binutils?include=commits')
        assert response.status_code == 200, response.text
        assert response.json()['monitors']['commits']['data']['selected'] == 1
        response = client.get('/api/ui/packages/binutils')
        assert response.status_code == 200, response.text
        assert any(section['id'] == 'commits' for section in response.json()['sections'])
        response = client.get('/api/v2/packages/binutils/commits')
        assert response.status_code == 200, response.text
        assert response.json()['items'][0]['oid'] == fix


def test_pages_bind_filters_and_exact_batch(package):
    from tracker.readmodel.commits import page

    config, db, repo, _, fix = package
    result = collect(config, db, repositories={'fixture': repo})
    batch = result['commits_batches']['binutils']['active']
    second = {**batch['entries'][0], 'oid': 'f' * 40}
    batch['entries'].append(second)
    first = page(result, 'binutils', per_page=1)
    assert first['next_cursor']
    assert page(result, 'binutils', per_page=1, cursor=first['next_cursor'])['items'][0]['oid'] == 'f' * 40
    with pytest.raises(ValueError, match='filters changed'):
        page(result, 'binutils', cursor=first['next_cursor'], rule='other')
    batch['id'] = 'changed'
    with pytest.raises(ValueError, match='filters changed'):
        page(result, 'binutils', cursor=first['next_cursor'])
    for malformed in ('!', '%%%not-json', 'a' * 2048):
        with pytest.raises(ValueError):
            page(result, 'binutils', cursor=malformed)


def test_new_candidate_and_failure_retain_only_same_target(package, monkeypatch):
    from tracker.monitors.commits import collector
    from tracker.monitors.commits.git import GitError

    config, db, repo, _, fix = package
    first = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:00:00+00:00')
    saved = first['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    (repo.path / 'file').write_text('third\n')
    git(repo.path, 'commit', '-qam', 'riscv: later')
    new = git(repo.path, 'rev-parse', 'HEAD')
    original = collector.analyze

    def failed(*args, **kwargs):
        if args[1] == fix:
            return dict(status='unknown', reason='timeout', matches=[], reverts=[], coverage={'complete': False})
        return original(*args, **kwargs)

    monkeypatch.setattr(collector, 'analyze', failed)
    result = collect(config, db, repositories={'fixture': repo}, now='2026-11-08T10:00:00+00:00')
    entries = {item['oid']: item for item in result['commits_batches']['binutils']['active']['entries']}
    assert entries[fix]['sides']['current']['retained']
    assert entries[fix]['sides']['current']['checked_at'] == saved['checked_at']
    new_side = entries[new]['sides']['current']
    assert (new_side['reason'] == 'pending' or new_side['checked_at'] == '2026-11-08T10:00:00+00:00')
    assert not new_side.get('retained')
    config['packages']['binutils']['monitors']['commits']['mappings']['3.9.0']['oid'] = 'e' * 40
    monkeypatch.setattr(repo, 'fetch', lambda *a, **k: (_ for _ in ()).throw(GitError('timeout')))
    changed = collect(config, db, repositories={'fixture': repo}, now='2026-11-08T10:01:00+00:00')
    sides = [item['sides']['current'] for item in changed['commits_batches']['binutils']['active']['entries']]
    assert not any(side.get('retained') for side in sides)
    assert all(side['checked_at'] is None for side in sides)


def test_permanent_failure_does_not_starve_unseen_candidate(package, monkeypatch):
    from tracker.monitors.commits import collector

    config, db, repo, _, _ = package
    (repo.path / 'other').write_text('another')
    git(repo.path, 'add', '.')
    git(repo.path, 'commit', '-qm', 'riscv: other')
    calls = []

    def unknown(repository, candidate, target, **kwargs):
        calls.append(candidate)
        return dict(status='unknown', reason='commit_limit', matches=[], reverts=[], coverage={'complete': False})

    monkeypatch.setattr(collector, 'analyze', unknown)
    for number in range(4):
        collect(config, db, repositories={'fixture': repo}, now=f'2026-11-06T10:0{number}:00+00:00')
    assert len(set(calls)) == 2


def test_history_failure_matches_summary_staleness(package):
    from tracker.readmodel.commits import page

    config, db, repo, _, _ = package
    result = collect(config, db, repositories={'fixture': repo})
    result['components']['commits:fixture']['error'] = 'timeout'
    assert all(item['stale'] for item in page(result, 'binutils')['items'])


def test_initial_fetch_failure_is_visible_as_error_not_unconfigured(package, monkeypatch):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app
    from tracker.monitors.commits.git import GitError

    config, db, repo, _, _ = package
    monkeypatch.setattr(repo, 'resolve', lambda ref: (_ for _ in ()).throw(GitError('timeout')))
    result = collect(config, db, repositories={'fixture': repo})
    assert 'active' not in result['commits_batches']['binutils']
    with ProjectedClient(create_app(db)) as client:
        response = client.get('/api/v2/packages/binutils?include=commits')
        assert response.status_code == 200, response.text
        assert response.json()['monitors']['commits']['check']['status'] == 'error'


def test_upstream_identity_change_is_rejected_during_publication(package, monkeypatch):
    config, db, repo, _, _ = package
    original = repo.read_commit
    changed = False

    def change(value):
        nonlocal changed
        if not changed:
            changed = True
            latest = state.read(db)
            latest['tracks']['binutils']['source'] = {'source': 'github', 'github': 'other/repository'}
            state.commit(db, latest)
        return original(value)

    monkeypatch.setattr(repo, 'read_commit', change)
    result = collect(config, db, repositories={'fixture': repo})
    assert 'active' not in result['commits_batches']['binutils']


def test_source_binding_change_and_age_mark_history_stale(package):
    from tracker.readmodel.commits import page

    config, db, repo, _, _ = package
    result = collect(config, db, repositories={'fixture': repo})
    result['specs']['binutils']['metadata']['sources'][0]['url'] = 'https://example.org/different.tar.xz'
    assert all(item['stale'] for item in page(result, 'binutils')['items'])
    result['commits_batches']['binutils']['active']['checked_at'] = '2000-01-01T00:00:00+00:00'
    assert all(item['stale'] for item in page(result, 'binutils')['items'])


def test_removed_policy_clears_owned_state_without_touching_others(package):
    config, db, repo, _, _ = package
    first = collect(config, db, repositories={'fixture': repo})
    original_sources = first['sources']
    config['packages']['binutils']['monitors']['commits'] = False
    result = collect(config, db, repositories={'fixture': repo})
    assert not any(result[key] for key in ('commits_records', 'commits_scans', 'commits_evidence', 'commits_batches'))
    assert result['sources'] == original_sources
    assert not any(key.startswith('commits:') for key in result['components'])


def test_tag_drift_retains_original_binding_without_refresh_time(package, monkeypatch):
    config, db, repo, _, fix = package
    mapping = config['packages']['binutils']['monitors']['commits']['mappings']['3.9.0']
    mapping['ref'] = 'refs/tags/v3.9.0'
    monkeypatch.setattr(repo, 'fetch', lambda *a, **k: fix)
    first = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:00:00+00:00')
    saved = first['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    monkeypatch.setattr(repo, 'fetch', lambda *a, **k: 'f' * 40)
    result = collect(config, db, repositories={'fixture': repo}, now='2026-11-08T10:00:00+00:00')
    observed = result['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    assert observed['target']['oid'] == fix
    assert observed['retained'] and observed['error'] == 'tag_drift'
    assert observed['checked_at'] == saved['checked_at']


def test_candidate_response_limit_preserves_total_and_bounded_rule_progress(package):
    config, db, repo, _, _ = package
    config['commits']['max_findings'] = 1
    (repo.path / 'other').write_text('next candidate')
    git(repo.path, 'add', '.')
    git(repo.path, 'commit', '-qm', 'riscv: second')
    for number in range(3):
        result = collect(config, db, repositories={'fixture': repo}, now=f'2026-11-06T10:0{number}:00+00:00')
    batch = result['commits_batches']['binutils']['active']
    progress = result['commits_batches']['binutils']['rules']
    assert batch['selected'] == 1 and batch['total'] == 2
    assert batch['coverage']['truncated'] and batch['status'] == 'partial'
    assert len(progress['matches']) == 1 and progress['total'] == 2


def test_target_sides_can_succeed_and_fail_independently(package, monkeypatch):
    from tracker.monitors.commits import collector
    config, db, repo, _, fix = package
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    revision = config['packages']['binutils']['monitors']['commits']['mappings']['3.9.0']['source_revision']
    config['packages']['binutils']['monitors']['commits']['mappings']['3.10.0'] = dict(
        oid='e' * 40, source_revision=revision, source_url='https://example.org/new-source.tar.xz',
        review_url='https://example.org/new-review')
    config['commits']['batch_size'] = 10

    def unknown_target(*args, **kwargs):
        if args[0] == 'e' * 40:
            from tracker.monitors.commits.git import GitError
            raise GitError('missing_object')
        return resolve(*args, **kwargs)

    resolve = repo.resolve
    monkeypatch.setattr(repo, 'resolve', unknown_target)
    monkeypatch.setattr(repo, 'fetch', lambda *a, **k: (_ for _ in ()).throw(collector.GitError('timeout')))
    result = collect(config, db, repositories={'fixture': repo})
    batch = result['commits_batches']['binutils']['active']
    assert batch['entries'][0]['sides']['current']['status'] == 'original_reachable'
    assert batch['entries'][0]['sides']['upgrade']['reason'] == 'timeout'
    assert batch['status'] == 'partial'


def test_rule_failure_is_local_to_one_package(package, monkeypatch):
    from copy import deepcopy

    from tracker.monitors.commits import collector
    from tracker.monitors.commits.rules import RuleError

    config, db, repo, _, _ = package
    latest = state.read(db)
    latest['specs']['foo3'] = deepcopy(latest['specs']['binutils'])
    latest['specs']['foo3']['head'] = 'd' * 40
    state.commit(db, latest)
    policy = deepcopy(config['packages']['binutils']['monitors']['commits'])
    policy['rules'][0]['id'] = 'bad-rule'
    config['packages']['foo3']['monitors'] = {'commits': policy}
    original = collector.match_rules

    def matching(commit, rules, **kwargs):
        if rules[0].id == 'bad-rule':
            raise RuleError('regex_timeout')
        return original(commit, rules, **kwargs)

    monkeypatch.setattr(collector, 'match_rules', matching)
    result = collect(config, db, repositories={'fixture': repo})
    assert result['commits_batches']['binutils']['active']['selected'] == 1
    assert 'active' not in result['commits_batches']['foo3']
    assert result['components']['commits:package:foo3']['error'] == 'regex_timeout'
    assert result['components']['commits:fixture']['error'] is None


def test_identical_replay_does_not_write_or_advance_generation(package):
    config, db, repo, _, _ = package
    first = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:00:00+00:00')
    second = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:00:00+00:00')
    assert second == first


def test_retained_commit_filter_is_the_same_evidence_as_detail(package):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app

    config, db, repo, _, _ = package
    result = collect(config, db, repositories={'fixture': repo})
    result['commits_batches']['binutils']['active']['entries'][0]['sides']['current']['retained'] = True
    state.commit(db, result)
    with ProjectedClient(create_app(db)) as client:
        listing = client.get('/api/v2/packages?monitor=commits&select=retained:commits=yes').json()
        assert listing['total'] == listing['retained_count'] == 1
        assert listing['items'][0]['name'] == 'binutils'


def test_changed_collection_budget_invalidates_an_in_flight_batch(package, monkeypatch):
    from tracker.monitors.commits import collector

    config, db, repo, _, _ = package
    previous = collector.input_key(config, state.read(db), 'binutils',
        collector.package_policies(config)['binutils'], collector.settings(config).repositories['fixture'])
    config['commits']['max_findings'] = 2
    current = collector.input_key(config, state.read(db), 'binutils',
        collector.package_policies(config)['binutils'], collector.settings(config).repositories['fixture'])
    assert previous != current
