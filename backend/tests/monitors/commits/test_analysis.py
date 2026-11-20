import base64
import hashlib
import os

import pytest

from tests.helpers.git import commit, git, init
from tracker.monitors.commits.analysis import analyze
from tracker.monitors.commits.git import Repository


def histories(tmp_path):
    init(tmp_path)
    (tmp_path / 'file').write_text('before\n')
    base = commit(tmp_path, 'base')
    (tmp_path / 'file').write_text('after\n')
    git(tmp_path, 'commit', '-qam', 'riscv: fix')
    fix = git(tmp_path, 'rev-parse', 'HEAD')
    return Repository(tmp_path), base, fix


def test_original_and_revert_remain_historical(tmp_path):
    repo, _, fix = histories(tmp_path)
    git(tmp_path, 'revert', '--no-edit', fix)
    reverted = git(tmp_path, 'rev-parse', 'HEAD')
    result = analyze(repo, fix, reverted)
    assert result['status'] == 'original_reachable'
    assert result['reverts'] == [reverted]


def test_exact_cherry_pick_is_strict_evidence(tmp_path):
    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'backport', base)
    (tmp_path / 'other').write_text('unrelated\n')
    commit(tmp_path, 'unrelated')
    git(tmp_path, 'cherry-pick', '-x', fix)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    assert target != fix
    result = analyze(repo, fix, target)
    assert result['status'] == 'strict_patch_evidence'
    assert result['matches'] == [target]


def test_no_evidence_and_budget_are_not_conflated(tmp_path):
    repo, base, fix = histories(tmp_path)
    assert analyze(repo, fix, base)['status'] == 'no_evidence_in_range'
    git(tmp_path, 'checkout', '-qb', 'elsewhere', base)
    (tmp_path / 'other').write_text('x')
    commit(tmp_path, 'other')
    result = analyze(repo, fix, git(tmp_path, 'rev-parse', 'HEAD'), history_limit=1)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'commit_limit'


def test_patch_id_whitespace_match_is_not_confirmed(tmp_path):
    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'whitespace', base)
    (tmp_path / 'file').write_text('after  \n')
    git(tmp_path, 'commit', '-qam', 'riscv backport')
    result = analyze(repo, fix, git(tmp_path, 'rev-parse', 'HEAD'))
    assert result['status'] == 'backport_needs_review'


def test_unknown_objects_are_not_negative(tmp_path):
    repo, base, _ = histories(tmp_path)
    assert analyze(repo, 'f' * 40, base)['status'] == 'unknown'


def test_error_after_enumeration_is_incomplete(tmp_path, monkeypatch):
    from tracker.monitors.commits.git import GitError

    repo, base, fix = histories(tmp_path)
    original = repo.run

    def fail_blob(*args):
        if args[:2] == ('cat-file', 'blob'):
            raise GitError('output_limit')
        return original(*args)

    monkeypatch.setattr(repo, 'run', fail_blob)
    result = analyze(repo, fix, base)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'output_limit'
    assert not result['coverage']['complete']


def test_revert_of_revert_is_only_a_historical_clue(tmp_path):
    repo, _, fix = histories(tmp_path)
    git(tmp_path, 'revert', '--no-edit', fix)
    revert = git(tmp_path, 'rev-parse', 'HEAD')
    git(tmp_path, 'revert', '--no-edit', revert)
    restored = git(tmp_path, 'rev-parse', 'HEAD')
    result = analyze(repo, fix, restored)
    assert result['status'] == 'original_reachable'
    assert result['reverts'] == [revert, restored]


def test_binary_changes_cannot_be_strictly_confirmed(tmp_path):
    repo, base, _ = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'binary', base)
    (tmp_path / 'binary').write_bytes(b'binary\x00data')
    candidate = commit(tmp_path, 'riscv binary')
    result = analyze(repo, candidate, base)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'unsupported_commit_shape'


def test_shallow_negative_is_unknown_and_declared_full_range_is_allowed(tmp_path):
    repo, base, fix = histories(tmp_path)
    (repo.path / '.git' / 'shallow').write_text(base + '\n')
    result = analyze(repo, fix, base)
    assert result['status'] == 'unknown' and result['reason'] == 'shallow_history'
    ranged = analyze(repo, fix, base, history_start=base)
    assert ranged['status'] == 'no_evidence_in_range'
    assert ranged['coverage']['complete']


def test_empty_and_missing_file_are_not_the_same(tmp_path):
    repo, base, _ = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'empty-file', base)
    (tmp_path / 'empty').write_bytes(b'')
    candidate = commit(tmp_path, 'riscv: add empty file')
    git(tmp_path, 'checkout', '-qb', 'backport-empty', base)
    (tmp_path / 'unrelated').write_text('different parent')
    commit(tmp_path, 'context')
    git(tmp_path, 'cherry-pick', candidate)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    assert analyze(repo, candidate, target)['status'] == 'strict_patch_evidence'


def test_squash_keeps_reference_without_claiming_equivalence(tmp_path):
    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'squash', base)
    (tmp_path / 'file').write_text('after\n')
    (tmp_path / 'other').write_text('extra change')
    commit(tmp_path, 'squash with reference ' + fix)
    result = analyze(repo, fix, git(tmp_path, 'rev-parse', 'HEAD'))
    assert result['status'] == 'backport_needs_review'


@pytest.mark.parametrize('kind', ['original', 'strict', 'reference', 'patch_only'])
def test_verified_association_revert_chains_are_target_history_clues(tmp_path, kind):
    repo, base, fix = histories(tmp_path)
    if kind != 'original':
        git(tmp_path, 'checkout', '-qb', 'backport', base)
        if kind == 'strict':
            git(tmp_path, 'cherry-pick', '-x', fix)
        else:
            (tmp_path / 'file').write_text('after  \n' if kind == 'patch_only' else 'after with adapted context\n')
            message = 'riscv backport' + (' of ' + fix if kind == 'reference' else '')
            git(tmp_path, 'commit', '-qam', message)
    linked = git(tmp_path, 'rev-parse', 'HEAD')
    git(tmp_path, 'revert', '--no-edit', linked)
    reverted = git(tmp_path, 'rev-parse', 'HEAD')
    git(tmp_path, 'revert', '--no-edit', reverted)
    restored = git(tmp_path, 'rev-parse', 'HEAD')
    result = analyze(repo, fix, restored, history_start=base)
    expected = {'original': 'original_reachable', 'strict': 'strict_patch_evidence',
                'reference': 'backport_needs_review', 'patch_only': 'backport_needs_review'}
    assert result['status'] == expected[kind]
    assert result['reverts'] == [reverted, restored]
    if kind in ('reference', 'patch_only'):
        assert result['reason'] == 'association_only'
        assert linked in result['matches'] and result['strict_manifest'] is None


def test_unrelated_reverts_and_other_branches_are_not_reported(tmp_path):
    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'revert', '--no-edit', fix)
    off_target = git(tmp_path, 'rev-parse', 'HEAD')
    git(tmp_path, 'checkout', '-qb', 'target', base)
    (tmp_path / 'other').write_text('unrelated\n')
    unrelated = commit(tmp_path, 'unrelated')
    git(tmp_path, 'revert', '--no-edit', unrelated)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    result = analyze(repo, fix, target, history_start=base)
    assert result['status'] == 'no_evidence_in_range'
    assert not result['reverts'] and off_target not in result['matches']
    bounded = analyze(repo, fix, off_target, history_start=off_target)
    assert bounded['status'] == 'original_reachable' and bounded['reverts'] == []


def test_strict_manifest_preserves_raw_paths_modes_and_absent_vs_empty(tmp_path):
    from tracker.monitors.commits.model import StrictManifest

    repo, base, _ = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'raw-paths', base)
    raw_path = b'raw-\xff\tfile'
    file_path = os.fsencode(tmp_path) + b'/' + raw_path
    with open(file_path, 'wb') as stream:
        stream.write(b'')
    (tmp_path / 'file').unlink()
    (tmp_path / 'executable').write_text('text\n')
    (tmp_path / 'executable').chmod(0o755)
    candidate = commit(tmp_path, 'riscv: file states')
    git(tmp_path, 'checkout', '-qb', 'copied', base)
    (tmp_path / 'unrelated').write_text('other parent\n')
    parent = commit(tmp_path, 'context')
    git(tmp_path, 'cherry-pick', candidate)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    result = analyze(repo, candidate, target, history_start=base)
    assert result['status'] == 'strict_patch_evidence'
    manifest = StrictManifest.model_validate(result['strict_manifest'])
    assert manifest.version == 1
    assert (manifest.candidate.oid, manifest.candidate.parent_oid) == (candidate, base)
    assert [(change.oid, change.parent_oid) for change in manifest.matches] == [(target, parent)]
    assert manifest.candidate.files == manifest.matches[0].files
    files = {base64.b64decode(item.path_b64): item for item in manifest.candidate.files}
    empty = files[raw_path]
    assert empty.before.model_dump() == dict(mode='000000', blob_oid=None, sha256=None)
    assert empty.after.mode == '100644' and empty.after.blob_oid
    assert empty.after.sha256 == hashlib.sha256(b'').hexdigest()
    deleted = files[b'file']
    assert deleted.before.sha256 == hashlib.sha256(b'before\n').hexdigest()
    assert deleted.after.model_dump() == dict(mode='000000', blob_oid=None, sha256=None)
    assert files[b'executable'].after.mode == '100755'
    assert b'text\n' not in manifest.model_dump_json().encode()


@pytest.mark.parametrize('budget', ['file_count', 'path_bytes', 'manifest_bytes', 'multiple_matches'])
def test_strict_manifest_budget_never_confirms_without_complete_proof(tmp_path, monkeypatch, budget):
    from tracker.monitors.commits import analysis, model

    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'backport', base)
    (tmp_path / 'other').write_text('context\n')
    commit(tmp_path, 'context')
    git(tmp_path, 'cherry-pick', fix)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    if budget == 'file_count':
        monkeypatch.setattr(model, 'STRICT_FILE_LIMIT', 1)
    elif budget == 'path_bytes':
        monkeypatch.setattr(analysis, 'STRICT_PATH_BYTES', 1)
    elif budget == 'manifest_bytes':
        monkeypatch.setattr(model, 'STRICT_MANIFEST_BYTES', 1)
    else:
        git(tmp_path, 'revert', '--no-edit', target)
        reverted = git(tmp_path, 'rev-parse', 'HEAD')
        git(tmp_path, 'revert', '--no-edit', reverted)
        target = git(tmp_path, 'rev-parse', 'HEAD')
        monkeypatch.setattr(model, 'STRICT_FILE_LIMIT', 2)
    result = analyze(repo, fix, target, history_start=base)
    assert result['status'] == 'unknown' and result['reason'] == 'strict_manifest_limit'
    assert result['matches'] == [] and result['strict_manifest'] is None
    assert not result['coverage']['complete']


@pytest.mark.parametrize('kind', ['path', 'mode', 'before_content'])
def test_patch_shortlist_cannot_bypass_strict_file_requirements(tmp_path, monkeypatch, kind):
    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'adapted', base)
    if kind == 'path':
        git(tmp_path, 'mv', 'file', 'renamed')
    elif kind == 'mode':
        (tmp_path / 'file').chmod(0o755)
    else:
        (tmp_path / 'file').write_text('different before\n')
    git(tmp_path, 'commit', '-qam', 'adapted parent')
    changed = tmp_path / ('renamed' if kind == 'path' else 'file')
    changed.write_text('after\n')
    git(tmp_path, 'commit', '-qam', 'backport reference ' + fix)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    monkeypatch.setattr(repo, 'patch_id', lambda value: 'same-shortlist')
    result = analyze(repo, fix, target, history_start=base)
    assert result['status'] == 'backport_needs_review'
    assert result['reason'] == 'association_only' and result['strict_manifest'] is None


def test_only_unambiguous_candidate_reference_links_reverts(tmp_path, monkeypatch):
    from tracker.monitors.commits.git import GitError

    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'ambiguous', base)
    (tmp_path / 'file').write_text('adapted context\n')
    git(tmp_path, 'commit', '-qam', 'backport reference ' + fix[:7])
    backport = git(tmp_path, 'rev-parse', 'HEAD')
    git(tmp_path, 'revert', '--no-edit', backport)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    resolve = repo.resolve

    def ambiguous(ref):
        if ref == fix[:7]:
            raise GitError('ambiguous_object')
        return resolve(ref)

    monkeypatch.setattr(repo, 'resolve', ambiguous)
    result = analyze(repo, fix, target, history_start=base)
    assert result['status'] == 'unknown' and result['reason'] == 'ambiguous_object'
    assert result['matches'] == result['reverts'] == []
