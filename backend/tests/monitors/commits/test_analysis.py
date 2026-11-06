import subprocess

from tracker.monitors.commits.analysis import analyze
from tracker.monitors.commits.git import Repository


def git(path, *args):
    return subprocess.check_output(['git', '-c', f'safe.directory={path}', '-C', str(path), *args],
                                   stderr=subprocess.DEVNULL).decode().strip()


def histories(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    git(tmp_path, 'config', 'user.email', 'fixture@example.org')
    (tmp_path / 'file').write_text('before\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    base = git(tmp_path, 'rev-parse', 'HEAD')
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
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'unrelated')
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
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'other')
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
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'riscv binary')
    candidate = git(tmp_path, 'rev-parse', 'HEAD')
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
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'riscv: add empty file')
    candidate = git(tmp_path, 'rev-parse', 'HEAD')
    git(tmp_path, 'checkout', '-qb', 'backport-empty', base)
    (tmp_path / 'unrelated').write_text('different parent')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'context')
    git(tmp_path, 'cherry-pick', candidate)
    target = git(tmp_path, 'rev-parse', 'HEAD')
    assert analyze(repo, candidate, target)['status'] == 'strict_patch_evidence'


def test_squash_keeps_reference_without_claiming_equivalence(tmp_path):
    repo, base, fix = histories(tmp_path)
    git(tmp_path, 'checkout', '-qb', 'squash', base)
    (tmp_path / 'file').write_text('after\n')
    (tmp_path / 'other').write_text('extra change')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'squash with reference ' + fix)
    result = analyze(repo, fix, git(tmp_path, 'rev-parse', 'HEAD'))
    assert result['status'] == 'backport_needs_review'
