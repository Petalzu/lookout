"""SQLite row storage; public snapshots are assembled views, not database blobs."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

type Snapshot = dict[str, Any]


FORMAT = 4
# Collection shape, not provider identity. Older row formats reject new collections.
LEGACY_DEPTH = dict(sources=1, tracks=1, specs=1, inventory=1, index=1, components=1,
                    builds=2, monitors=2, github_items=2, github_links=2)
DEPTH = dict(LEGACY_DEPTH, commits_records=2, commits_scans=2, commits_evidence=2, commits_batches=2)


@dataclass(frozen=True)
class Revision:
    token: str
    rows: dict = field(default_factory=dict, compare=False, repr=False)
    build_stamp: str | None = field(default=None, compare=False)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def validate(snapshot) -> Snapshot:
    if (not isinstance(snapshot, dict) or type(snapshot.get('schema')) is not int
            or snapshot['schema'] != 1 or type(snapshot.get('generation')) is not int
            or snapshot['generation'] < 0):
        raise ValueError('snapshot payload is invalid')
    return snapshot


def pack(snapshot, previous=None):
    validate(snapshot)
    metadata = {key: value for key, value in snapshot.items() if key not in DEPTH}
    sections, records = {}, {}
    for section, depth in DEPTH.items():
        if section not in snapshot:
            continue
        values = snapshot[section]
        if not isinstance(values, dict):
            raise ValueError('snapshot collection is not a mapping: ' + section)
        sections[section] = []
        prior = (previous or {}).get(section, {})
        for subject, value in values.items():
            if depth == 1:
                records[section, subject, ''] = None if subject in prior and value == prior[subject] else encode(value)
            else:
                if not isinstance(value, dict):
                    raise ValueError('snapshot subject is not a mapping: ' + section)
                if not value:
                    sections[section].append(subject)
                for slot, observation in value.items():
                    old = prior.get(subject, {})
                    records[section, subject, slot] = (None if slot in old and observation == old[slot]
                                                      else encode(observation))
    header = encode(dict(storage=FORMAT, snapshot=metadata, sections=sections))
    return header, records


def _unpack_header(header, version):
    if not isinstance(header, dict) or header.get('storage') != version:
        raise ValueError('snapshot payload is invalid')
    snapshot = validate(header.get('snapshot'))
    sections = header.get('sections')
    collections = DEPTH if version == FORMAT else LEGACY_DEPTH
    if not isinstance(sections, dict) or set(sections) - collections.keys():
        raise ValueError('snapshot collections are invalid')
    return snapshot, sections


def _create(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS snapshot_clock (id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL, build_checked_at TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS snapshot_records (
        section TEXT NOT NULL, subject TEXT NOT NULL, slot TEXT NOT NULL,
        payload TEXT NOT NULL, digest TEXT NOT NULL, revision TEXT NOT NULL,
        PRIMARY KEY (section, subject, slot)) WITHOUT ROWID''')


def _preserve_clock(conn, records, snapshot):
    """Avoid materializing an unchanged OBS heartbeat during another phase's write."""
    clock = conn.execute('SELECT build_checked_at FROM snapshot_clock WHERE id=1').fetchone()
    if not clock or not clock[0]:
        return None
    stamp = clock[0]
    stamps = {'fetched_at', 'attempted_at'}
    builds = {('builds', name, target): fact for name, targets in snapshot.get('builds', {}).items()
              for target, fact in targets.items()}
    component = snapshot.get('components', {}).get('builds')
    if component is not None:
        builds['components', 'builds', ''] = component
    if (('components', 'builds', '') not in builds
            or any(fact.get('error') or any(fact.get(k) != stamp for k in stamps) for fact in builds.values())):
        return None
    if all(records[key] is None for key in builds):
        return stamp
    for section, subject, slot, payload in conn.execute(
            "SELECT section, subject, slot, payload FROM snapshot_records "
            "WHERE section='builds' OR (section='components' AND subject='builds')"):
        key = section, subject, slot
        if key in builds and records[key] is not None:
            old = json.loads(payload)
            if {k: v for k, v in old.items() if k not in stamps} == {
                    k: v for k, v in builds[key].items() if k not in stamps}:
                records[key] = None
    return stamp


def _write(conn, header, records, snapshot):
    revision = uuid.uuid4().hex
    stamp = _preserve_clock(conn, records, snapshot)
    old = {tuple(row[:3]): row[3] for row in conn.execute(
        'SELECT section, subject, slot, digest FROM snapshot_records')}
    changed = []
    for key, payload in records.items():
        if payload is None:
            if key not in old:
                raise ValueError('unchanged record missing from database')
            continue
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if old.get(key) != digest:
            changed.append((*key, payload, digest, revision))
    removed = old.keys() - records.keys()
    conn.executemany('DELETE FROM snapshot_records WHERE section=? AND subject=? AND slot=?', removed)
    conn.executemany('''INSERT INTO snapshot_records VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(section, subject, slot) DO UPDATE SET
        payload=excluded.payload, digest=excluded.digest, revision=excluded.revision''', changed)
    conn.execute('INSERT INTO snapshot VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (header,))
    conn.execute('INSERT OR REPLACE INTO snapshot_clock VALUES (1, ?, ?)', (revision, stamp))
    conn.execute(f'PRAGMA user_version={FORMAT}')
    return dict(changed_records=len(changed), deleted_records=len(removed),
                serialized_bytes=len(header.encode()) + sum(len(row[3].encode()) for row in changed))


def commit(db, snapshot, *, previous=None):
    header, records = pack(snapshot, previous[0] if previous else None)
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db, timeout=10)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot'").fetchone()
            if version != FORMAT and (version != 0 or exists):
                raise ValueError('database migration required; use deploy/migrate-state.py before starting collectors')
            _create(conn)
            if previous is not None:
                clock = conn.execute('SELECT revision, build_checked_at FROM snapshot_clock WHERE id=1').fetchone()
                revision = previous[1]
                expected = (revision.token, revision.build_stamp) if revision else None
                if clock != expected:
                    raise ValueError('snapshot changed since read; refusing stale incremental write')
            result = _write(conn, header, records, snapshot)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def read(db, previous: tuple[Snapshot, Revision | None] | None = None) -> tuple[Snapshot, Revision, str | None]:
    """Capture rows in one read transaction, then decode after releasing its lock."""
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
        conn.execute('BEGIN')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 2, 3, FORMAT):
            raise ValueError('unsupported database storage version')
        clock = conn.execute('SELECT revision, build_checked_at FROM snapshot_clock WHERE id=1').fetchone()
        if clock is None:
            raise ValueError('snapshot clock row is missing')
        token, stamp = clock
        prior = previous[1] if previous and isinstance(previous[1], Revision) else None
        clock_rewound = bool(prior and prior.build_stamp and (not stamp or stamp < prior.build_stamp))
        if previous is not None and prior and prior.token == token and not clock_rewound:
            return previous[0], Revision(token, prior.rows, stamp), stamp
        row = conn.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        if row is None:
            raise ValueError('snapshot row is missing')
        revisions, payloads = {}, {}
        if version in (2, 3, FORMAT):
            if prior:
                revisions = {tuple(r[:3]): r[3] for r in conn.execute(
                    'SELECT section, subject, slot, revision FROM snapshot_records')}
                changed = [key for key, value in revisions.items() if prior.rows.get(key) != value
                           or clock_rewound and (key[0] == 'builds' or key[:2] == ('components', 'builds'))]
                for offset in range(0, len(changed), 200):
                    keys = changed[offset:offset + 200]
                    placeholders = ','.join('(?,?,?)' for _ in keys)
                    for r in conn.execute('SELECT section, subject, slot, payload FROM snapshot_records '
                                          f'WHERE (section,subject,slot) IN ({placeholders})',
                                          [part for key in keys for part in key]):
                        payloads[tuple(r[:3])] = r[3]
            else:
                for r in conn.execute('SELECT section, subject, slot, revision, payload FROM snapshot_records'):
                    revisions[tuple(r[:3])] = r[3]
                    payloads[tuple(r[:3])] = r[4]
    header = json.loads(row[0])
    if version == 0:
        validate(header)
        return header, Revision(token, build_stamp=stamp), stamp
    snapshot, sections = _unpack_header(header, version)
    for section, empty in sections.items():
        snapshot[section] = {name: {} for name in empty}
    for key in revisions:
        section, subject, slot = key
        if section not in sections:
            raise ValueError('observation has no collection')
        if key in payloads:
            value = json.loads(payloads[key])
        else:
            if previous is None:
                raise ValueError('unchanged observation requires a prior snapshot')
            value = previous[0][section][subject]
            if DEPTH[section] == 2:
                value = value[slot]
        if DEPTH[section] == 1:
            snapshot[section][subject] = value
        else:
            snapshot[section].setdefault(subject, {})[slot] = value
    return snapshot, Revision(token, revisions, stamp), stamp


def migrate(db):
    """Explicit, transactional conversion from formats 0, 2 and 3 to format 4.

    The caller must stop collectors and create a verified backup first. Readers
    can inspect the old format; ordinary writes never migrate it implicitly.
    """
    path = Path(db)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version == FORMAT:
                conn.rollback()
                return False
            if version in (2, 3):
                row = conn.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                clock = conn.execute('SELECT revision FROM snapshot_clock WHERE id=1').fetchone()
                if row is None or clock is None:
                    raise ValueError('snapshot or clock row is missing')
                header = json.loads(row[0])
                _, sections = _unpack_header(header, version)
                if any(section not in sections for section, in conn.execute(
                        'SELECT DISTINCT section FROM snapshot_records')):
                    raise ValueError('observation has no collection')
                header['storage'] = FORMAT
                conn.execute('UPDATE snapshot SET payload=? WHERE id=1', (encode(header),))
                conn.execute('UPDATE snapshot_clock SET revision=? WHERE id=1', (uuid.uuid4().hex,))
                conn.execute(f'PRAGMA user_version={FORMAT}')
                conn.commit()
                return True
            if version != 0:
                raise ValueError('unsupported database storage version')
            row = conn.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            clock = conn.execute('SELECT revision, build_checked_at FROM snapshot_clock WHERE id=1').fetchone()
            if row is None or clock is None:
                raise ValueError('snapshot or clock row is missing')
            snapshot = json.loads(row[0])
            header, records = pack(snapshot)
            _create(conn)
            _write(conn, header, records, snapshot)
            # Heartbeats are observations too; migration must not erase their time.
            conn.execute('UPDATE snapshot_clock SET build_checked_at=? WHERE id=1', (clock[1],))
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
