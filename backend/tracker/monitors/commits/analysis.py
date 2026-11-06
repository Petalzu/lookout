"""Conservative historical inclusion, never a functional applicability decision."""
import hashlib
import re
from typing import TypedDict

from tracker.monitors.commits.git import GitError


class Coverage(TypedDict):
    target: str
    limit: int
    complete: bool
    checked: int
    start: str | None
    ancestry_complete: bool


class Analysis(TypedDict):
    status: str
    reason: str | None
    matches: list[str]
    reverts: list[str]
    coverage: Coverage


def _files(repo, value):
    commit = repo.read_commit(value)
    if len(commit['parents']) != 1:
        return None
    parent = commit['parents'][0]
    changed = repo.run('diff-tree', '-r', '--no-commit-id', '--no-renames', '--no-abbrev', '--raw', '-z',
                       parent, value, '--').split(b'\x00')
    result = []
    for index in range(0, len(changed) - 1, 2):
        metadata, path = changed[index:index + 2]
        if not metadata:
            continue
        fields = metadata.removeprefix(b':').split()
        if len(fields) != 5 or not path:
            raise GitError('invalid_diff')
        before_mode, after_mode, before, after, _ = fields
        if any(mode not in (b'000000', b'100644', b'100755') for mode in (before_mode, after_mode)):
            return None
        hashes = []
        for blob in (before, after):
            if set(blob) == {48}:
                hashes.append(None)
                continue
            raw = repo.run('cat-file', 'blob', blob.decode())
            if b'\x00' in raw:
                return None
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                return None
            hashes.append(hashlib.sha256(raw).hexdigest())
        result.append((path, before_mode, after_mode, *hashes))
    return sorted(result) if result else None


def _references(body, original):
    return any(original.startswith(token) for token in re.findall(r'(?<![a-f0-9])[a-f0-9]{7,64}(?![a-f0-9])', body))


def analyze(repo, candidate, target, *, history_limit=2000, history_start=None):
    result: Analysis = {'status': 'unknown', 'reason': None, 'matches': [], 'reverts': [],
                        'coverage': {'target': target, 'limit': history_limit, 'complete': False, 'checked': 0, 'start': history_start, 'ancestry_complete': False}}
    try:
        candidate = repo.resolve(candidate)
        target = repo.resolve(target)
        reachable = repo.is_ancestor(candidate, target)
        result['coverage']['ancestry_complete'] = bool(
            reachable or not repo.is_shallow() or repo.is_ancestor(target, candidate))
        if reachable:
            result.update(status='original_reachable', matches=[candidate])
        elif repo.is_shallow() and history_start is None:
            result['reason'] = 'shallow_history'
            return result
        try:
            if history_start and not repo.is_ancestor(history_start, target):
                result['reason'] = 'analysis_range_unreachable'
                return result
            history = repo.commits(target, exclude=history_start, limit=history_limit)
        except GitError as error:
            result['reason'] = error.code
            return result
        history_complete = repo.range_complete(history)
        result['coverage'].update(checked=0, complete=False)
        strict = _files(repo, candidate) if not reachable else None
        patch = repo.patch_id(candidate) if strict else None
        associations, matched = [], []
        reverts = {}
        for value in history:
            commit = repo.read_commit(value)
            body = commit['subject'] + '\n' + commit['body']
            if not reachable and value != candidate:
                referenced = _references(body, candidate)
                if referenced:
                    tokens = re.findall(r'(?<![a-f0-9])[a-f0-9]{7,64}(?![a-f0-9])', body)
                    references = [token for token in tokens if candidate.startswith(token)]
                    if any(repo.resolve(token) == candidate for token in references):
                        associations.append(value)
                if patch and repo.patch_id(value) == patch:
                    if _files(repo, value) == strict:
                        matched.append(value)
                    else:
                        associations.append(value)
            for match in re.findall(r'This reverts commit ([a-f0-9]{40}|[a-f0-9]{64})\.', commit['body']):
                reverts[value] = match
            result['coverage']['checked'] += 1
        result['coverage']['complete'] = history_complete
        affected = {candidate, *matched}
        for value, reverted in reverts.items():
            if reverted in affected:
                result['reverts'].append(value)
                affected.add(value)
        if reachable:
            return result
        if matched:
            result.update(status='strict_patch_evidence', matches=matched)
        elif associations:
            result.update(status='backport_needs_review', matches=sorted(set(associations)), reason='association_only')
        elif not strict:
            result.update(status='unknown', reason='unsupported_commit_shape')
        elif not result['coverage']['complete'] or not result['coverage']['ancestry_complete']:
            result['coverage']['complete'] = False
            result['reason'] = 'shallow_history'
        else:
            result.update(status='no_evidence_in_range')
        return result
    except (GitError, ValueError) as error:
        result['coverage']['complete'] = False
        result['reason'] = error.code if isinstance(error, GitError) else 'invalid_identity'
        return result
