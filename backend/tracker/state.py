"""Phase-owned observation updates and atomic SQLite snapshot views."""
import fcntl
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tracker import storage


def utcnow():
    return datetime.now(UTC).isoformat(timespec='seconds')

def empty() -> storage.Snapshot:
    return dict(schema=1, generation=0, mode='live', sources={}, tracks={}, builds={}, specs={},
                inventory={}, index={}, components={}, last_attempt=None)

def read(db) -> storage.Snapshot:
    return read_cached(db)[0]


def recover(db):
    """Startup only: let SQLite roll back an interrupted transaction.

    Readers remain mode=ro. A journal's existence does not prove it is hot;
    SQLite owns that decision and its locking/recovery protocol. Never unlink
    journals, create a missing database, or replace committed observations.
    """
    path = Path(db)
    if not Path(str(path) + '-journal').exists():
        return False
    with writer_lock(path, timeout=10):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)) as conn:
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity check failed after journal recovery')
        read(path)
    return True


def read_cached(db, previous: tuple[storage.Snapshot, storage.Revision | None] | None = None) -> tuple[storage.Snapshot, storage.Revision | None]:
    """Read one SQLite transaction, reusing payload only for its exact storage revision.

    Successful, unchanged build polls have a small clock row. They never relabel
    failed/missing records: commit_build_heartbeat requires a complete clean vector.
    ``previous`` is a prior (snapshot, revision) result from this database;
    reused nested observations are shared and must not be mutated by the caller.
    """
    if not Path(db).is_file():
        return empty(), None
    snapshot, revision, stamp = storage.read(db, previous)
    if stamp:
        snapshot = {**snapshot,
            'builds': {name: {tid: {**fact, 'fetched_at': stamp, 'attempted_at': stamp}
                             for tid, fact in targets.items()} for name, targets in snapshot['builds'].items()},
            'components': {**snapshot['components'], 'builds': {
                **snapshot['components']['builds'], 'fetched_at': stamp, 'attempted_at': stamp}}}
    return snapshot, revision


def commit_build_heartbeat(db, latest, patches, component):
    """Return True only when a complete successful poll changed no status facts.

    Called under writer_lock. A partial/failing response, scope change or changed
    fact takes the ordinary row-update path, including its exact old timestamps.
    """
    stamps = {'fetched_at', 'attempted_at'}
    stamp = component.get('fetched_at')
    if not stamp or component.get('error') or component.get('attempted_at') != stamp:
        return False
    prior = latest['components'].get('builds', {})
    if {k: v for k, v in component.items() if k not in stamps} != {k: v for k, v in prior.items() if k not in stamps}:
        return False
    expected = set(latest['inventory'])
    targets = {target['id'] for target in latest['targets']}
    if set(patches) != expected or set(latest['builds']) != expected:
        return False
    for name, builds in patches.items():
        if set(builds) != targets or set(latest['builds'][name]) != targets:
            return False
        for tid, fact in builds.items():
            old = latest['builds'][name][tid]
            if (set(fact) - BUILD_FIELDS['builds'] or fact.get('error')
                    or any(fact.get(key) != stamp for key in stamps)
                    or {k: v for k, v in fact.items() if k not in stamps} !=
                       {k: v for k, v in old.items() if k in BUILD_FIELDS['builds'] - stamps}):
                return False
    with closing(sqlite3.connect(db, timeout=10)) as conn, conn:
        updated = conn.execute('UPDATE snapshot_clock SET build_checked_at=? WHERE id=1', (stamp,))
        if updated.rowcount != 1:
            raise ValueError('snapshot clock row is missing')
        return True


def commit(db, snapshot, *, previous=None):
    """Publish changed observations and their clock in one database transaction.

    Concurrent writers must hold writer_lock across read/merge/commit; a transaction
    alone cannot prevent overwriting a newer snapshot with old input.
    Each payload write gets a new storage revision, even if generation is unchanged.
    ``previous`` is an unmodified read_cached result; its storage revision and
    heartbeat are checked before skipping serialization of equal observations.
    """
    return storage.commit(db, snapshot, previous=previous)

@contextmanager
def writer_lock(db, timeout=0):
    lock = Path(str(db) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def success(previous, fields, now):
    return {**previous, **fields, 'fetched_at': now, 'attempted_at': now, 'error': None}

def failure(previous, error, now):
    return {**previous, 'attempted_at': now, 'error': error}

def stale(observation, now, ttl):
    stamp = observation.get('checked_at') or observation.get('fetched_at')
    if not stamp:
        return True
    try:
        age = (now - datetime.fromisoformat(stamp)).total_seconds()
        return age < -300 or age > ttl
    except (ValueError, TypeError):
        return True

def next_stale_change(observation, now, ttl):
    """Next UTC instant where stale() can change without a new observation.

    A timestamp more than 300 seconds ahead becomes valid at stamp - 300.
    Expiry uses > ttl, so the first stale datetime is one microsecond after it.
    Invalid/missing or already-expired timestamps never become fresh forwards.
    """
    stamp = observation.get('checked_at') or observation.get('fetched_at')
    if not stamp:
        return None
    try:
        observed = datetime.fromisoformat(stamp)
        age = (now - observed).total_seconds()
        if age < -300:
            return observed - timedelta(seconds=300)
        if age <= ttl:
            return observed + timedelta(seconds=ttl, microseconds=1)
    except (ValueError, TypeError, OverflowError):
        # Invalid timestamps have no usable expiry boundary.
        pass
    return None


def package_names(snapshot):
    """The confirmed SPEC catalogue owns package identity; OBS owns build objects.

    Before a complete Git catalogue (or when Git is disabled), use the OBS source
    inventory. A failed refresh retains the last confirmed SPEC catalogue, including
    packages whose metadata cannot be parsed and packages not yet registered in OBS.
    """
    catalogued = snapshot.get('components', {}).get('spec_git', {}).get('catalog_head')
    return snapshot.get('specs' if catalogued else 'sources', {}).keys()


def current_source(snapshot, name):
    """Select the same source observation for display, comparison and monitors.

    A recorded SPEC owns the source version even on failure; do not silently
    substitute OBS's different source. OBS remains the fallback before the first
    SPEC observation, and independently owns build/artifact evidence.
    """
    obs_ttl = snapshot.get('obs_stale_after_seconds', snapshot.get('stale_after_seconds', 86400))
    spec = snapshot.get('specs', {}).get(name)
    if spec is not None:
        metadata = spec.get('metadata') or {}
        provenance = {'head': spec.get('head'), 'native_query': spec.get('native_query'),
                      'origin': spec.get('source_origin')}
        revision = ('spec:' + hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
                    if spec.get('head') or spec.get('native_query', {}).get('spec_sha256') else None)
        value = metadata.get('version')
        ttl = max(obs_ttl, snapshot['spec_interval_seconds'] * 2) if 'spec_interval_seconds' in snapshot else obs_ttl
        return {**spec, 'version': value if usable_version(value) else None,
                'revision': revision, 'origin': 'spec', 'stale_after_seconds': ttl,
                'error': spec.get('error')}
    source = snapshot.get('sources', {}).get(name, {})
    value = source.get('version')
    return {**source, 'version': value if usable_version(value) else None,
            'revision': source.get('srcmd5') or source.get('native_query', {}).get('spec_sha256'),
            'origin': 'obs', 'stale_after_seconds': obs_ttl}

def compare(current, latest, comparable=True):
    """Only RPM VERSION; never compare the Epoch or package Release to an upstream tag."""
    if not comparable or not all(usable_version(v) for v in (current, latest)):
        return 'unknown'
    try:
        import rpm
    except ImportError:
        return 'unknown'
    result = rpm.labelCompare(('0', current, '0'), ('0', latest, '0'))
    return 'outdated' if result < 0 else 'ahead' if result > 0 else 'current'

def usable_version(value):
    # OBS's source parser may emit literal MACRO placeholders, not an expanded RPM VERSION.
    return bool(isinstance(value, str) and value not in ('unknown', 'None') and 'MACRO' not in value
                and re.fullmatch(r'[A-Za-z0-9._+~^]+', value))


# Small executable ownership contract. Shared config projections are explicit;
# observations owned by another phase can never be overwritten by a slow job.
PHASE_FIELDS = {
    'obs': frozenset(('last_attempt', 'mode', 'targets', 'bindings', 'native_ids', 'obs',
                      'stale_after_seconds', 'obs_stale_after_seconds', 'build_history_interval_seconds',
                      'inventory', 'index', 'sources', 'builds', 'presentation')),
    'upstreams': frozenset(('tracks', 'native_ids', 'nv_digest', 'bindings')),
    'specs': frozenset(('specs', 'spec_interval_seconds')),
    'monitors': frozenset(('monitors', 'monitor_catalog', 'monitor_stale_after_seconds', 'dependency_packages',
                           'dependency_environments')),
    'builds': frozenset(('builds',)),
    'github': frozenset(('github_items', 'github_links', 'github_repositories')),
    'commits': frozenset(('commits_records', 'commits_scans', 'commits_evidence', 'commits_batches')),
}

# Status and history share a displayed build, but have independent writers and
# cadences. Neither writer may replay the other one's old observation.
BUILD_FIELDS = {
    'builds': frozenset(('raw_status', 'details', 'matches_source',
                         'fetched_at', 'attempted_at', 'error')),
    'obs': frozenset(('last_success', 'history_attempted_at', 'history_checked_at',
                     'history_error', 'history_unresolved',
                     'version_attempt_identity', 'version_attempted_at')),
}


def owns_component(phase, key):
    if phase in ('github', 'commits'):
        return key.startswith(phase + ':')
    if phase == 'obs':
        return key in ('targets', 'inventory', 'source_index') or key.startswith('build_history:')
    return key == {'upstreams': 'nvchecker', 'specs': 'spec_git', 'builds': 'builds'}.get(phase)


def merge(latest, phase, fields, components=None):
    """Merge only owned fields into the latest snapshot; never accept generation."""
    if phase not in PHASE_FIELDS or set(fields) - PHASE_FIELDS[phase]:
        raise ValueError('snapshot phase field ownership violation')
    components = components or {}
    for key in components:
        if phase == 'monitors':
            raise ValueError('monitor observations own no collection component')
        if not owns_component(phase, key):
            raise ValueError('snapshot component ownership violation')
    result = deepcopy(latest)
    result.update(deepcopy({k: v for k, v in fields.items() if k != 'builds'}),
                  generation=latest['generation'] + 1)
    if phase == 'obs':
        prior = {t['id']: (t['repository'], t['architecture']) for t in latest.get('targets', [])}
        current = {t['id']: (t['repository'], t['architecture']) for t in result.get('targets', [])}
        result['builds'] = {
            name: {tid: fact for tid, fact in builds.items()
                   if tid in current and prior.get(tid) == current[tid]}
            for name, builds in result['builds'].items() if name in result['inventory']
        }
    for name, targets in fields.get('builds', {}).items():
        for tid, facts in targets.items():
            if set(facts) - BUILD_FIELDS[phase]:
                raise ValueError('build observation field ownership violation')
            result['builds'].setdefault(name, {}).setdefault(tid, {}).update(deepcopy(facts))
    result['components'].update(deepcopy(components))
    return result
