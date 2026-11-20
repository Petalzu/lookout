"""Commit collections are phase-owned rows; format upgrades stay explicit and atomic."""
import hashlib
import json
import runpy
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from tests.helpers.storage import legacy, rows
from tracker import state, storage

COLLECTIONS = ('commits_records', 'commits_scans', 'commits_evidence', 'commits_batches')
REPOSITORY = 'openruyi/lookout'
OID = 'a' * 40
STAMP = '2030-01-02T03:04:05+00:00'


@pytest.fixture
def commits_snapshot(snapshot):
    snapshot.update(
        commits_records={REPOSITORY: {OID: {'oid': OID, 'subject': 'first'},
                                     'b' * 40: {'oid': 'b' * 40, 'subject': 'second'}}},
        commits_scans={REPOSITORY: {'main:manifest': {'head': OID, 'complete': True},
                                   'stable:manifest': {'head': OID, 'complete': False}}},
        commits_evidence={'binutils': {'source:' + OID: {'oid': OID, 'path': 'SPECS/a.spec'},
                                      'upstream:' + OID: {'oid': OID, 'path': 'src/a.c'}}},
        commits_batches={'binutils': {'main:0': {'next': OID, 'complete': False},
                                     'main:1': {'next': None, 'complete': True}}},
    )
    snapshot['components']['commits:' + REPOSITORY] = state.success({}, {}, STAMP)
    return snapshot


def old_database(db, snapshot, version, stamp=STAMP):
    if version == 0:
        legacy(db, snapshot, stamp)
        return
    header, records = storage.pack(snapshot)
    header = json.loads(header)
    header['storage'] = version
    with closing(sqlite3.connect(db)) as conn, conn:
        storage._create(conn)
        conn.execute('INSERT INTO snapshot VALUES (1, ?)', (json.dumps(header),))
        conn.execute('INSERT INTO snapshot_clock VALUES (1, ?, ?)', ('old', stamp))
        conn.executemany('INSERT INTO snapshot_records VALUES (?, ?, ?, ?, ?, ?)', [
            (*key, payload, hashlib.sha256(payload.encode()).hexdigest(), 'old-row-' + str(index))
            for index, (key, payload) in enumerate(records.items())
        ])
        conn.execute(f'PRAGMA user_version={version}')


def fail_after_version_update(monkeypatch):
    connect = sqlite3.connect
    mutations = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql == 'PRAGMA user_version=4':
                mutations.append(sql)
                raise sqlite3.OperationalError('injected failure after migration writes')
            return result

    def open_connection(*args, **kwargs):
        return connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(storage.sqlite3, 'connect', open_connection)
    return mutations


def test_storage_format_is_four_without_changing_public_snapshot_schema(tmp_path):
    assert storage.FORMAT == 4
    snapshot = state.empty()
    assert snapshot['schema'] == 1
    assert not set(COLLECTIONS) & snapshot.keys()
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    assert state.read(db) == snapshot
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone() == (4,)
        assert conn.execute('PRAGMA journal_mode').fetchone() == ('delete',)


@pytest.mark.parametrize('section', COLLECTIONS)
@pytest.mark.parametrize('values', [{}, {'empty-subject': {}}])
def test_empty_commit_collections_round_trip_as_depth_two(tmp_path, section, values):
    assert storage.DEPTH[section] == 2
    snapshot = {**state.empty(), section: values}
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    assert state.read(db) == snapshot
    assert rows(db) == {}
    with closing(sqlite3.connect(db)) as conn:
        header = json.loads(conn.execute('SELECT payload FROM snapshot').fetchone()[0])
    assert section not in header['snapshot']
    assert header['sections'][section] == list(values)


def test_commit_rows_use_repository_or_package_and_individual_slot(tmp_path, commits_snapshot):
    db = tmp_path / 'db'
    state.commit(db, commits_snapshot)
    expected = {(section, subject, slot) for section in COLLECTIONS
                for subject, slots in commits_snapshot[section].items() for slot in slots}
    assert {key for key in rows(db) if key[0] in COLLECTIONS} == expected
    assert state.read(db) == commits_snapshot
    with closing(sqlite3.connect(db)) as conn:
        header = json.loads(conn.execute('SELECT payload FROM snapshot').fetchone()[0])
    assert not set(COLLECTIONS) & header['snapshot'].keys()


@pytest.mark.parametrize('section', COLLECTIONS)
@pytest.mark.parametrize('values', [[], {'subject': []}])
def test_commit_collections_reject_non_mapping_shape(section, values):
    with pytest.raises(ValueError, match='not a mapping'):
        storage.pack({**state.empty(), section: values})


@pytest.mark.parametrize('section', COLLECTIONS)
def test_incremental_commit_encodes_and_decodes_only_changed_row(
        tmp_path, commits_snapshot, monkeypatch, section):
    db = tmp_path / 'db'
    state.commit(db, commits_snapshot)
    before_rows = rows(db)
    previous = state.read_cached(db)
    changed = deepcopy(previous[0])
    subject = next(iter(changed[section]))
    slot, unchanged = changed[section][subject]
    changed[section][subject][slot]['extra'] = 'updated'
    changed['generation'] += 1
    encoded, encode = [], storage.encode

    def record(value):
        encoded.append(value)
        return encode(value)

    monkeypatch.setattr(storage, 'encode', record)
    result = state.commit(db, changed, previous=previous)
    assert result['changed_records'] == 1 and len(encoded) == 2
    assert [key for key, value in rows(db).items() if before_rows[key] != value] == [(section, subject, slot)]
    decoded, loads = [], storage.json.loads

    def decode(value):
        decoded.append(value)
        return loads(value)

    monkeypatch.setattr(storage.json, 'loads', decode)
    observed, revision = state.read_cached(db, previous)
    assert observed == changed and len(decoded) == 2
    assert revision != previous[1]
    assert observed[section][subject][unchanged] is previous[0][section][subject][unchanged]
    assert observed[section][subject][slot] is not previous[0][section][subject][slot]
    assert previous[0] == commits_snapshot
    same, same_revision = state.read_cached(db, (observed, revision))
    assert same is observed and same_revision == revision


@pytest.mark.parametrize('section', COLLECTIONS)
def test_cached_commit_deletions_preserve_empty_and_absent_collections(tmp_path, commits_snapshot, section):
    db = tmp_path / 'db'
    state.commit(db, commits_snapshot)
    previous = state.read_cached(db)
    changed = deepcopy(previous[0])
    subject = next(iter(changed[section]))
    changed[section][subject].clear()
    result = state.commit(db, changed, previous=previous)
    assert result['deleted_records'] == 2 and result['changed_records'] == 0
    following = state.read_cached(db, previous)
    assert following[0] == changed
    changed = deepcopy(following[0])
    changed.pop(section)
    state.commit(db, changed, previous=following)
    assert state.read_cached(db, following)[0] == changed
    assert previous[0] == commits_snapshot


def test_commits_write_preserves_build_heartbeat_and_clock_rewind(tmp_path, commits_snapshot):
    db = tmp_path / 'db'
    state.commit(db, commits_snapshot)
    before = rows(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('UPDATE snapshot_clock SET build_checked_at=?', (STAMP,))
    previous = state.read_cached(db)
    changed = deepcopy(previous[0])
    changed['commits_records'][REPOSITORY][OID]['subject'] = 'updated'
    result = state.commit(db, changed, previous=previous)
    assert result['changed_records'] == 1
    following = state.read_cached(db, previous)
    assert following[0] == changed
    assert all(rows(db)[key] == value for key, value in before.items() if key[0] == 'builds')
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute('SELECT build_checked_at FROM snapshot_clock').fetchone() == (STAMP,)
        conn.execute('UPDATE snapshot_clock SET build_checked_at=NULL')
    rewound = state.read_cached(db, following)
    assert rewound[0]['builds'] == commits_snapshot['builds']
    assert rewound[0]['commits_records'][REPOSITORY][OID] is following[0]['commits_records'][REPOSITORY][OID]
    assert following[0]['builds']['binutils']['rva23']['fetched_at'] == STAMP


def test_commits_phase_owns_only_its_four_fields_and_component_prefix(commits_snapshot):
    assert state.PHASE_FIELDS['commits'] == frozenset(COLLECTIONS)
    fields = {section: commits_snapshot[section] for section in COLLECTIONS}
    components = {'commits:' + REPOSITORY: {'error': 'retry'}}
    latest = deepcopy(commits_snapshot)
    merged = state.merge(latest, 'commits', fields, components)
    assert merged['generation'] == latest['generation'] + 1
    assert merged['components'] == {**latest['components'], **components}
    assert all(merged[section] == fields[section] for section in COLLECTIONS)
    assert all(merged[section] == latest[section] for section in ('sources', 'builds', 'tracks', 'specs'))
    merged['commits_records'][REPOSITORY][OID]['subject'] = 'changed'
    assert latest == commits_snapshot and fields['commits_records'][REPOSITORY][OID]['subject'] == 'first'


@pytest.mark.parametrize('phase', ['obs', 'upstreams', 'specs', 'monitors', 'builds', 'github'])
@pytest.mark.parametrize('section', COLLECTIONS)
def test_other_phases_cannot_write_commit_fields(phase, section):
    with pytest.raises(ValueError, match='field ownership'):
        state.merge(state.empty(), phase, {section: {}})
    assert not state.owns_component(phase, 'commits:' + REPOSITORY)


@pytest.mark.parametrize('section', ['sources', 'tracks', 'specs', 'monitors', 'builds', 'github_items',
                                     'generation', 'schema', 'components'])
def test_commits_phase_cannot_write_other_fields(section):
    with pytest.raises(ValueError, match='field ownership'):
        state.merge(state.empty(), 'commits', {section: {}})


@pytest.mark.parametrize('key', ['commits:' + REPOSITORY, 'commits:' + REPOSITORY + ':main'])
def test_commits_component_prefix_is_owned(key):
    assert state.owns_component('commits', key)
    assert state.merge(state.empty(), 'commits', {}, {key: {'error': None}})['components'][key] == {'error': None}


@pytest.mark.parametrize('key', ['commits', 'commits-other', 'github:' + REPOSITORY, 'builds', 'spec_git'])
def test_commits_phase_cannot_write_other_components(key):
    assert not state.owns_component('commits', key)
    with pytest.raises(ValueError, match='component ownership'):
        state.merge(state.empty(), 'commits', {}, {key: {}})


@pytest.mark.parametrize('version', [0, 2, 3])
def test_explicit_upgrade_preserves_observations_clock_and_rows(tmp_path, snapshot, version):
    snapshot.update(monitors={'binutils': {'upstream': {'error': 'offline'}}},
                    github_items={REPOSITORY: {'7': {'title': 'old item'}}},
                    github_links={'binutils': {'7': {'repository': REPOSITORY}}})
    db = tmp_path / 'db'
    old_database(db, snapshot, version)
    previous = state.read_cached(db)
    before_rows = rows(db) if version else None
    original = db.read_bytes()
    assert state.read_cached(db, previous)[0] == previous[0]
    for incremental in (None, previous):
        with pytest.raises(ValueError, match='migration required'):
            state.commit(db, previous[0], previous=incremental)
        assert db.read_bytes() == original
    assert storage.migrate(db) is True
    observed = state.read_cached(db, previous)
    assert observed[0] == previous[0] and observed[1] != previous[1]
    assert not set(COLLECTIONS) & observed[0].keys()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone() == (4,)
        assert conn.execute('SELECT build_checked_at FROM snapshot_clock').fetchone() == (STAMP,)
        assert conn.execute('PRAGMA journal_mode').fetchone() == ('delete',)
    if before_rows is not None:
        assert rows(db) == before_rows
    pristine = db.read_bytes()
    assert storage.migrate(db) is False and db.read_bytes() == pristine
    changed = state.merge(observed[0], 'commits', {section: {} for section in COLLECTIONS})
    state.commit(db, changed, previous=observed)
    assert state.read(db) == changed


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('section', [*COLLECTIONS, 'future_collection'])
def test_old_row_formats_reject_unknown_collections_without_mutation(tmp_path, snapshot, version, section):
    db = tmp_path / 'db'
    old_database(db, snapshot, version)
    with closing(sqlite3.connect(db)) as conn, conn:
        header = json.loads(conn.execute('SELECT payload FROM snapshot').fetchone()[0])
        header['sections'][section] = []
        conn.execute('UPDATE snapshot SET payload=?', (json.dumps(header),))
    original = db.read_bytes()
    for operation in (state.read, storage.migrate):
        with pytest.raises(ValueError, match='collections'):
            operation(db)
        assert db.read_bytes() == original


@pytest.mark.parametrize('version', [0, 2, 3])
def test_upgrade_failure_really_rolls_back_header_clock_rows_and_ddl(tmp_path, snapshot, monkeypatch, version):
    db = tmp_path / 'db'
    old_database(db, snapshot, version)
    observed = state.read(db)
    original = db.read_bytes()
    with monkeypatch.context() as patch:
        mutations = fail_after_version_update(patch)
        with pytest.raises(sqlite3.OperationalError, match='injected failure'):
            storage.migrate(db)
    assert mutations == ['PRAGMA user_version=4']
    assert db.read_bytes() == original and state.read(db) == observed
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone() == (version,)
        assert conn.execute('SELECT revision, build_checked_at FROM snapshot_clock').fetchone() == ('old', STAMP)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        if version == 0:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='snapshot_records'").fetchall() == []
    assert not Path(str(db) + '-journal').exists()
    assert storage.migrate(db) is True and state.read(db) == observed


@pytest.mark.parametrize('version', [0, 2, 3])
def test_migration_cli_recognizes_old_formats_and_preserves_backup(tmp_path, snapshot, capsys, version):
    db, backup = tmp_path / 'db', tmp_path / 'backup'
    old_database(db, snapshot, version)
    observed = state.read(db)
    main = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'deploy/migrate-state.py'))['main']
    args = ['--db', str(db), '--backup', str(backup)]
    assert main(args) == 0
    assert capsys.readouterr().out == 'migrated\n'
    assert state.read(db) == state.read(backup) == observed
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone() == (4,)
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone() == (version,)
    pristine, backup_bytes = db.read_bytes(), backup.read_bytes()
    assert main(args) == 0
    assert capsys.readouterr().out == 'unchanged\n'
    assert db.read_bytes() == pristine and backup.read_bytes() == backup_bytes


@pytest.mark.parametrize('version', [0, 2, 3])
def test_migration_cli_failure_keeps_original_and_verified_backup(tmp_path, snapshot, monkeypatch, capsys, version):
    db, backup = tmp_path / 'db', tmp_path / 'backup'
    old_database(db, snapshot, version)
    observed, original = state.read(db), db.read_bytes()
    main = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'deploy/migrate-state.py'))['main']
    with monkeypatch.context() as patch:
        mutations = fail_after_version_update(patch)
        assert main(['--db', str(db), '--backup', str(backup)]) == 2
    assert 'injected failure' in capsys.readouterr().err
    assert mutations == ['PRAGMA user_version=4']
    assert db.read_bytes() == original and state.read(db) == state.read(backup) == observed
    assert list(tmp_path.glob('.snapshot-*')) == []
