"""Fixed-tip manifests; a cursor advances only with its saved object records."""
from typing import Any

from tracker.monitors.commits.git import GitError
from tracker.monitors.commits.model import json_size


def advance(repo, policy, previous, records, at, budget, *, fetch=True):
    identity = policy.fingerprint
    active: dict[str, Any] = dict(previous) if previous.get('policy') == identity else {}
    if not active or active.get('complete'):
        tip = repo.fetch(policy.url, policy.ref, depth=policy.fetch_depth, object_format=policy.object_format) if fetch else repo.resolve(policy.ref)
        start = repo.resolve(policy.window_start)
        if not repo.is_ancestor(start, tip):
            raise ValueError('discovery window start is not reachable')
        if (active.get('tip') and repo.is_ancestor(active['tip'], tip)
                and active.get('start') == start):
            completed = len(active['ids'])
            ids = active['ids'] + repo.commits(tip, exclude=active['tip'], limit=policy.scan_limit)
            if len(ids) > policy.scan_limit:
                raise GitError('commit_limit')
        else:
            completed = 0
            ids = repo.commits(tip, exclude=start, limit=policy.scan_limit)
        active = dict(policy=identity, tip=tip, start=start, ids=ids, cursor=completed, complete=False,
                      observed_at=at, history_complete=repo.range_complete(ids), last_complete_tip=active.get('last_complete_tip'))
        repo.run('update-ref', 'refs/lookout/active', tip)
    end = min(active['cursor'] + budget, len(active['ids']))
    missing = [oid for oid in active['ids'][active['cursor']:end] if oid not in records]
    if missing:
        # Only a write needs a new map and a byte budget. A failed round must not mutate its input.
        records = dict(records)
        record_bytes = sum(map(json_size, records.values()))
        for oid in missing:
            record = repo.read_commit(oid)
            size = json_size(record)
            if size > 1048576:
                raise GitError('commit_record_limit')
            record_bytes += size
            if record_bytes > policy.record_limit_bytes:
                raise GitError('repository_record_limit')
            records[oid] = record
    active.update(cursor=end, attempted_at=at, error=None, complete=end == len(active['ids']))
    if active['complete']:
        active['last_complete_tip'] = active['tip']
    return active, records
