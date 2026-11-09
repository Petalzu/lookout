import subprocess

import pytest

from tracker import state
from tracker.monitors.commits.collector import collect
from tracker.monitors.commits.git import Repository
from tracker.monitors.commits.model import settings
from tracker.monitors.commits.scan import advance

STAMP = '2026-11-06T10:00:00+00:00'
NEXT = '2026-11-08T10:00:00+00:00'


@pytest.fixture(autouse=True)
def fixed_git_clock(monkeypatch):
    monkeypatch.setenv('GIT_AUTHOR_DATE', STAMP)
    monkeypatch.setenv('GIT_COMMITTER_DATE', STAMP)


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


@pytest.mark.parametrize(('relation', 'reason'), [
    ('current', 'no_candidate_release'),
    ('outdated', 'target_unresolved'),
])
def test_real_collector_storage_and_upgrade_eligibility(package, monkeypatch, relation, reason):
    config, db, repo, _, fix = package
    monkeypatch.setattr(state, 'compare', lambda *args: relation)
    result = collect(config, db, repositories={'fixture': repo})
    assert state.read(db) == result
    batch = result['commits_batches']['binutils']['active']
    assert batch['source_identity']['name'] == 'binutils'
    assert batch['selected'] == batch['total'] == 1
    current = batch['entries'][0]['sides']['current']
    assert current['status'] == 'original_reachable'
    assert current['target']['oid'] == fix
    assert current['checked_at'] is not None
    assert batch['entries'][0]['sides']['upgrade']['reason'] == reason
    assert batch['target_version'] == ('3.10.0' if relation == 'outdated' else None)


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
        summary = response.json()['monitors']['commits']['check']
        assert summary['status'] == 'error' and summary['error'] == 'timeout'
        history = client.get('/api/v2/packages/binutils/commits')
        assert history.status_code == 200, history.text
        assert history.json()['check'] == summary
        assert history.json()['repository'] == 'fixture'
        assert summary['configured'] and summary['checked_at'] is None
        assert history.json()['items'] == []
        document = client.get('/api/ui/packages/binutils/commits')
        assert document.status_code == 200, document.text
        assert 'timeout' in document.text and 'Last attempt' in document.text
        assert 'Configured' in document.text


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


@pytest.fixture
def strict_package(package):
    config, db, repo, base, fix = package
    git(repo.path, 'checkout', '-qb', 'backport', base)
    (repo.path / 'other').write_text('unrelated context\n')
    git(repo.path, 'add', '.')
    git(repo.path, 'commit', '-qm', 'context')
    git(repo.path, 'cherry-pick', fix)
    target = git(repo.path, 'rev-parse', 'HEAD')
    git(repo.path, 'checkout', 'main')
    config['packages']['binutils']['monitors']['commits']['mappings']['3.9.0']['oid'] = target
    return config, db, repo, base, fix


def test_strict_manifest_round_trips_storage_api_and_document(strict_package):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app

    config, db, repo, _, fix = strict_package
    result = collect(config, db, repositories={'fixture': repo})
    batch = result['commits_batches']['binutils']['active']
    side = batch['entries'][0]['sides']['current']
    manifest = side['strict_manifest']
    assert batch['method_version'] == 2
    assert side['status'] == 'strict_patch_evidence' and manifest['candidate']['oid'] == fix
    assert manifest['matches'][0]['oid'] == side['target']['oid']
    saved = state.read(db)
    assert saved['commits_evidence']['binutils'][side['key']]['strict_manifest'] == manifest
    assert saved['commits_batches']['binutils']['active']['entries'][0]['sides']['current']['strict_manifest'] == manifest
    with ProjectedClient(create_app(db)) as client:
        summary = client.get('/api/v2/packages/binutils?include=commits').json()['monitors']['commits']
        history = client.get('/api/v2/packages/binutils/commits').json()
        assert summary['data']['entries'][0]['sides']['current']['strict_manifest'] == manifest
        assert history['items'][0]['sides']['current']['strict_manifest'] == manifest
        for url in ('/api/ui/packages/binutils', '/api/ui/packages/binutils/commits'):
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert 'Saved manifest v1' in response.text
            assert manifest['candidate']['parent_oid'] in response.text
            assert manifest['candidate']['files'][0]['after']['sha256'] in response.text


def test_strict_manifest_failure_retention_and_target_identity(strict_package, monkeypatch):
    from tracker.monitors.commits import collector

    config, db, repo, _, fix = strict_package
    first = collect(config, db, repositories={'fixture': repo}, now=STAMP)
    saved = first['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    monkeypatch.setattr(collector, 'analyze', lambda *args, **kwargs: dict(status='unknown', reason='timeout',
        matches=[], reverts=[], strict_manifest=None, coverage={'complete': False}))
    failed = collect(config, db, repositories={'fixture': repo}, now=NEXT)
    retained = failed['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    assert retained['retained'] and retained['error'] == 'timeout'
    assert retained['checked_at'] == saved['checked_at'] == STAMP
    assert retained['strict_manifest'] == saved['strict_manifest']
    config['packages']['binutils']['monitors']['commits']['mappings']['3.9.0']['oid'] = fix
    changed = collect(config, db, repositories={'fixture': repo}, now='2026-11-08T10:01:00+00:00')
    current = changed['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    assert not current.get('retained') and current['checked_at'] is None
    assert current['strict_manifest'] is None


def test_old_method_manifest_is_readable_but_not_reused(package, monkeypatch):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app
    from tracker.monitors.commits import collector, model
    from tracker.readmodel.commits import page

    config, db, repo, _, _ = package
    monkeypatch.setattr(collector, 'METHOD_VERSION', 1)
    monkeypatch.setattr(model, 'METHOD_VERSION', 1)
    old = collect(config, db, repositories={'fixture': repo}, now=STAMP)
    side = old['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    side.pop('strict_manifest', None)
    state.commit(db, old)
    assert page(old, 'binutils')['check']['status'] == 'input_changed'
    with ProjectedClient(create_app(db)) as client:
        response = client.get('/api/v2/packages/binutils/commits')
        assert response.status_code == 200, response.text
        assert response.json()['items'][0]['sides']['current']['strict_manifest'] is None
    monkeypatch.setattr(collector, 'METHOD_VERSION', 2)
    monkeypatch.setattr(model, 'METHOD_VERSION', 2)
    monkeypatch.setattr(collector, 'analyze', lambda *args, **kwargs: dict(status='unknown', reason='timeout',
        matches=[], reverts=[], strict_manifest=None, coverage={'complete': False}))
    new = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:01:00+00:00')
    current = new['commits_batches']['binutils']['active']['entries'][0]['sides']['current']
    assert not current.get('retained') and current['checked_at'] is None
    assert current['key'] != side['key']


@pytest.mark.parametrize('condition', ['not_configured', 'pending', 'repo_error', 'package_error'])
def test_history_without_batch_shares_summary_check_and_attempt(package, condition):
    from datetime import datetime
    from types import SimpleNamespace

    from tracker.monitors.version.compare import evaluate
    from tracker.presentation.commits import history_section
    from tracker.readmodel import commits

    _, db, _, _, _ = package
    snapshot = state.read(db)
    now = datetime.fromisoformat(STAMP)
    if condition != 'not_configured':
        snapshot['commits_batches'] = {'binutils': {'policy': {'repository': 'fixture'}}}
    if condition.endswith('_error'):
        key = 'commits:fixture' if condition == 'repo_error' else 'commits:package:binutils'
        snapshot['components'][key] = dict(error='timeout', attempted_at=STAMP)
    version = evaluate(snapshot, 'binutils', now)
    summary = commits.project(SimpleNamespace(snapshot=snapshot, name='binutils', version=version, now=now))
    page = commits.page(snapshot, 'binutils', now=now)
    assert page['check'] == summary['check']
    assert page['check']['status'] == ('error' if condition.endswith('_error') else condition)
    assert page['check']['configured'] == (condition != 'not_configured')
    assert page['check']['checked_at'] is None
    assert page['check']['attempted_at'] == (STAMP if condition.endswith('_error') else None)
    assert not page['items'] and page['batch_id'] is None
    rendered = history_section('binutils', page).model_dump_json()
    if condition.endswith('_error'):
        assert 'timeout' in rendered and 'Last attempt' in rendered
    with pytest.raises(ValueError, match='refresh the first page'):
        commits.page(snapshot, 'binutils', cursor='old-cursor', now=now)


@pytest.mark.parametrize('condition', ['ok', 'error', 'expired', 'input_changed'])
def test_history_with_batch_shares_summary_check_times_and_staleness(package, condition, monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from tracker.monitors.version import compare as versions
    from tracker.presentation.commits import history_section
    from tracker.readmodel import commits

    config, db, repo, _, _ = package
    observed = datetime.fromisoformat(STAMP)

    class CollectionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed.astimezone(tz) if tz else observed.replace(tzinfo=None)

    monkeypatch.setattr(versions, 'datetime', CollectionClock)
    seeded = state.read(db)
    seeded['specs']['binutils'].update(fetched_at=STAMP, attempted_at=STAMP)
    seeded['tracks']['binutils'].update(version='3.9.0', fetched_at=STAMP, attempted_at=STAMP)
    state.commit(db, seeded)
    # Expire commit evidence while source/upstream facts and release eligibility stay fixed.
    config['commits']['stale_after_seconds'] = 7200
    snapshot = collect(config, db, repositories={'fixture': repo}, now=STAMP)
    assert snapshot['commits_batches']['binutils']['active']['target_version'] is None
    if condition == 'error':
        snapshot['components']['commits:fixture'].update(error='timeout', attempted_at=NEXT)
    elif condition == 'input_changed':
        snapshot['specs']['binutils']['metadata']['sources'][0]['url'] = 'https://example.org/changed.tar.xz'
    now = observed + timedelta(seconds=7201) if condition == 'expired' else observed
    version = versions.evaluate(snapshot, 'binutils', now)
    assert not version.upgrading and not version.upstream_stale
    summary = commits.project(SimpleNamespace(snapshot=snapshot, name='binutils', version=version, now=now))
    page = commits.page(snapshot, 'binutils', now=now)
    assert page['check'] == summary['check']
    assert page['check']['status'] == condition
    assert page['check']['checked_at'] == STAMP
    assert page['check']['attempted_at'] == (NEXT if condition == 'error' else STAMP)
    assert page['check']['configured']
    assert page['check']['stale'] == (condition != 'ok')
    assert all(item['stale'] == (condition != 'ok') for item in page['items'])
    rendered = history_section('binutils', page).model_dump_json()
    assert condition.replace('_', ' ').title() in rendered
    if condition == 'error':
        assert 'timeout' in rendered


def test_retained_batch_history_api_keeps_observed_time_and_latest_failure(package, monkeypatch):
    from tests.conftest import ProjectedClient
    from tracker.api import create_app
    from tracker.monitors.commits.git import GitError

    config, db, repo, _, _ = package
    first = collect(config, db, repositories={'fixture': repo}, now=STAMP)
    monkeypatch.setattr(repo, 'resolve', lambda ref: (_ for _ in ()).throw(GitError('timeout')))
    failed = collect(config, db, repositories={'fixture': repo}, now=NEXT)
    assert failed['commits_batches']['binutils']['active'] == first['commits_batches']['binutils']['active']
    with ProjectedClient(create_app(db)) as client:
        summary = client.get('/api/v2/packages/binutils?include=commits').json()['monitors']['commits']
        history = client.get('/api/v2/packages/binutils/commits').json()
        assert history['check'] == summary['check']
        assert history['check']['status'] == 'error' and history['check']['error'] == 'timeout'
        assert history['check']['checked_at'] == STAMP and history['check']['attempted_at'] == NEXT
        assert history['items'] and all(item['stale'] for item in history['items'])
        response = client.get('/api/ui/packages/binutils/commits')
        assert response.status_code == 200, response.text
        assert 'timeout' in response.text and 'Last attempt' in response.text


def test_batch_byte_limit_never_splits_or_drops_a_published_strict_manifest(package):
    config, db, repo, base, _ = package
    git(repo.path, 'reset', '--hard', base)
    for index in range(30):
        (repo.path / f'file-{index}').write_text('complete raw content\n')
    git(repo.path, 'add', '.')
    git(repo.path, 'commit', '-qm', 'riscv: many files')
    candidate = git(repo.path, 'rev-parse', 'HEAD')
    git(repo.path, 'checkout', '-qb', 'backport', base)
    (repo.path / 'context').write_text('parent context\n')
    git(repo.path, 'add', '.')
    git(repo.path, 'commit', '-qm', 'context')
    git(repo.path, 'cherry-pick', candidate)
    target = git(repo.path, 'rev-parse', 'HEAD')
    git(repo.path, 'checkout', 'main')
    config['commits']['max_batch_bytes'] = 16384
    config['packages']['binutils']['monitors']['commits']['mappings']['3.9.0']['oid'] = target
    limited = collect(config, db, repositories={'fixture': repo}, now=STAMP)
    batch = limited['commits_batches']['binutils']['active']
    assert batch['status'] == 'partial' and batch['coverage']['truncated']
    assert batch['selected'] == 0 and batch['total'] == 1
    proof = next(iter(limited['commits_evidence']['binutils'].values()))['strict_manifest']
    assert len(proof['candidate']['files']) == 30 and len(proof['matches'][0]['files']) == 30
    config['commits']['max_batch_bytes'] = 65536
    published = collect(config, db, repositories={'fixture': repo}, now='2026-11-06T10:01:00+00:00')
    batch = published['commits_batches']['binutils']['active']
    assert batch['selected'] == batch['total'] == 1
    assert batch['entries'][0]['sides']['current']['strict_manifest'] == proof
