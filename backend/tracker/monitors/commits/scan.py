"""Fixed-tip manifests; a cursor advances only with its saved object records."""
import json
from typing import Any

from tracker.monitors.commits.git import GitError
from tracker.monitors.commits.model import digest


def advance(repo, policy, previous, records, at, budget, *, fetch=True):
    records = dict(records)
    identity = digest(policy.model_dump())
    active: dict[str, Any] = dict(previous) if previous.get('policy') == identity else {}
    if not active or active.get('complete'):
        tip = repo.fetch(policy.url, policy.ref, depth=policy.fetch_depth, object_format=policy.object_format) if fetch else repo.resolve(policy.ref)
        start = repo.resolve(policy.window_start)
        if not repo.is_ancestor(start, tip):
            raise ValueError('discovery window start is not reachable')
        if (active.get('tip') and repo.is_ancestor(active['tip'], tip)
                and active.get('start') == start):
            completed = len(active['ids'])
            ids = list(active['ids']) + repo.commits(tip, exclude=active['tip'], limit=policy.scan_limit)
            if len(ids) > policy.scan_limit:
                raise GitError('commit_limit')
        else:
            completed = 0
            ids = repo.commits(tip, exclude=start, limit=policy.scan_limit)
        active = dict(policy=identity, tip=tip, start=start, ids=ids, cursor=completed, complete=False,
                      observed_at=at, history_complete=repo.range_complete(ids), last_complete_tip=active.get('last_complete_tip'))
        repo.run('update-ref', 'refs/lookout/active', tip)
    record_bytes = sum(len(json.dumps(record, ensure_ascii=True).encode()) for record in records.values())
    end = min(active['cursor'] + budget, len(active['ids']))
    for value in active['ids'][active['cursor']:end]:
        if value not in records:
            record = repo.read_commit(value)
            size = len(json.dumps(record, ensure_ascii=True).encode())
            if size > 1048576:
                raise GitError('commit_record_limit')
            if record_bytes + size > policy.record_limit_bytes:
                raise GitError('repository_record_limit')
            record_bytes += size
            records[value] = record
    active.update(cursor=end, attempted_at=at, error=None, complete=end == len(active['ids']))
    if active['complete']:
        active['last_complete_tip'] = active['tip']
    return active, records
