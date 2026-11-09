"""Commit projections consume one saved snapshot, never Git or provider IO."""
import base64
import binascii
import json
from datetime import UTC, datetime
from urllib.parse import quote

from tracker import state
from tracker.monitors.commits.model import METHOD_VERSION, digest, source_binding
from tracker.monitors.version.compare import evaluate


def _batch(snapshot, name):
    return snapshot.get('commits_batches', {}).get(name, {}).get('active')


def _check(snapshot, name, batch, version, now):
    policy = snapshot.get('commits_batches', {}).get(name, {}).get('policy')
    repository = batch['repository'] if batch else (policy or {}).get('repository')
    components = snapshot.get('components', {})
    checks = [components.get('commits:' + (repository or ''), {}),
              components.get('commits:package:' + name, {})]
    error = next((check['error'] for check in checks if check.get('error')), None)
    attempted = max((check['attempted_at'] for check in checks if check.get('attempted_at')), default=None)
    if not batch:
        return dict(configured=bool(policy), status='error' if error else 'pending' if policy else 'not_configured',
                    stale=bool(error), checked_at=None, attempted_at=attempted, error=error, failures=[])
    changed = (batch.get('method_version') != METHOD_VERSION
               or batch.get('source_identity') != version.subject
               or batch.get('source_binding') != source_binding(version.source)
               or batch.get('upstream_binding') != digest(version.upstream.get('source'))
               or version.source_unavailable or version.identity_conflict
               or batch.get('target_version') != (version.target_version if version.upgrading else None))
    expired = state.stale({'fetched_at': batch.get('checked_at')}, now, batch.get('stale_after_seconds', 86400))
    gaps = {'no_candidate_release', 'target_unresolved', 'pending', 'unsupported_commit_shape',
            'association_only', 'shallow_history'}
    failures = sorted({side + ': ' + str(value.get('error') or value.get('reason'))
                       for item in batch['entries'] for side, value in item['sides'].items()
                       if (value.get('error') or value.get('reason')) not in gaps | {None}})
    attempts = [value for value in (attempted, batch.get('attempted_at')) if value]
    return dict(configured=True, failures=failures,
                status='input_changed' if changed else 'error' if error else 'expired' if expired else batch['status'],
                stale=bool(changed or expired or error), checked_at=batch.get('checked_at'),
                attempted_at=max(attempts, default=None), error=error,
                note='Counts cover the published candidate subset; historical evidence does not establish current behavior.')


def _entries(snapshot, batch, selected, stale):
    records = snapshot.get('commits_records', {}).get(batch['repository'], {})
    result = []
    for entry in selected:
        record = records.get(entry['oid'], {})
        result.append({**entry, 'subject': record.get('subject', entry['oid'])[:8192],
                       'committed_at': record.get('committed_at'),
                       'url': batch['web_url'] + '/commit/' + entry['oid'],
                       'stale': bool(stale or any(side.get('retained') for side in entry['sides'].values()))})
    return result


def project(context):
    batch = _batch(context.snapshot, context.name)
    check = _check(context.snapshot, context.name, batch, context.version, context.now)
    if not batch:
        policy = context.snapshot.get('commits_batches', {}).get(context.name, {}).get('policy')
        return dict(check=check, dimensions={}, data=dict(kind='commits', entries=[], selected=0, total=0,
                    batch_id=None, repository=(policy or {}).get('repository'), coverage={}, labels=[], history_url=None))
    entries = _entries(context.snapshot, batch, batch['entries'], check['stale'])
    statuses = sorted({side['status'] for item in entries for side in item['sides'].values()
                       if side.get('reason') != 'no_candidate_release'})
    rules = sorted({reason['rule'] for item in entries for reason in item['reasons']})
    return dict(check=check, dimensions={'commits:status': statuses, 'commits:repository': [batch['repository']],
                'commits:rule': rules, 'findings:commits': ['yes'] if entries else []}, data=dict(kind='commits', entries=entries, selected=batch['selected'],
                total=batch['total'], batch_id=batch['id'], repository=batch['repository'],
                coverage=batch['coverage'], labels=[],
                history_url='/packages/' + quote(context.name, safe='') + '/commits'))


def page(snapshot, name, *, cursor=None, per_page=20, status=None, rule=None, now=None):
    if not 1 <= per_page <= 50:
        raise ValueError('per_page must be in 1..50')
    batch = _batch(snapshot, name)
    now = now or datetime.now(UTC)
    check = _check(snapshot, name, batch, evaluate(snapshot, name, now), now)
    policy = snapshot.get('commits_batches', {}).get(name, {}).get('policy')
    if not batch:
        if cursor:
            raise ValueError('Commit batch or filters changed; refresh the first page.')
        return dict(items=[], total=0, next_cursor=None, batch_id=None, coverage={}, check=check,
                    repository=(policy or {}).get('repository'))
    signature = digest({'batch': batch['id'], 'status': status, 'rule': rule})
    offset = 0
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError()
            token = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
            if set(token) != {'signature', 'offset'} or token['signature'] != signature:
                raise ValueError()
            offset = token['offset']
            if type(offset) is not int or offset < 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
            raise ValueError('Commit batch or filters changed; refresh the first page.') from None
    items = [entry for entry in sorted(batch['entries'], key=lambda item: item['oid'])
             if (status is None or any(side['status'] == status and side.get('reason') != 'no_candidate_release'
                                         for side in entry['sides'].values()))
             and (rule is None or any(reason['rule'] == rule for reason in entry['reasons']))]
    result = _entries(snapshot, batch, items[offset:offset + per_page], check['stale'])
    next_cursor = None
    if offset + per_page < len(items):
        next_cursor = base64.urlsafe_b64encode(json.dumps({'signature': signature,
            'offset': offset + per_page}, separators=(',', ':')).encode()).decode().rstrip('=')
    return dict(items=result, total=len(items), next_cursor=next_cursor, batch_id=batch['id'],
                coverage=batch['coverage'], check=check, repository=batch['repository'])
