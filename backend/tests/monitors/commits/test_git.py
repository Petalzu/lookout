
import pytest

from tests.helpers.git import commit, git, init
from tracker.monitors.commits.git import GitError, Repository


@pytest.fixture
def history(tmp_path):
    init(tmp_path)
    (tmp_path / 'a').write_text('one\n')
    git(tmp_path, 'add', 'a')
    git(tmp_path, 'commit', '-qm', 'riscv: first')
    first = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / 'a').write_text('two\n')
    git(tmp_path, 'commit', '-qam', 'second')
    return Repository(tmp_path), first, git(tmp_path, 'rev-parse', 'HEAD')


def test_objects_and_ancestry_are_exact(history):
    repo, first, second = history
    assert repo.resolve('HEAD') == second
    assert repo.is_ancestor(first, second)
    assert not repo.is_ancestor(second, first)
    assert repo.commits(second) == [first, second]
    assert repo.commits(second, exclude=first) == [second]
    assert repo.read_commit(first)['paths'] == ['a']
    assert repo.read_commit(first)['subject'] == 'riscv: first'
    assert not repo.is_shallow()


def test_error_is_not_negative_ancestry(history):
    repo, first, _ = history
    with pytest.raises(GitError):
        repo.is_ancestor('f' * 40, first)
    with pytest.raises(GitError, match='invalid_ref'):
        repo.resolve('--help')
    with pytest.raises(GitError, match='commit_limit'):
        repo.commits(repo.resolve('HEAD'), limit=1)


def test_output_and_disk_are_bounded(history):
    repo, first, _ = history
    repo.output_limit = 8
    with pytest.raises(GitError, match='output_limit'):
        repo.read_commit(first)
    repo.disk_limit = 1
    with pytest.raises(GitError, match='disk_limit'):
        repo.resolve('HEAD')


def test_fetch_rejects_local_and_credentialled_sources(history):
    repo, _, _ = history
    for url in ('file:///tmp/x', 'https://user:password@example.org/x'):
        with pytest.raises(ValueError):
            repo.fetch(url, 'refs/heads/main')


def test_utf8_control_character_and_hash_named_paths_are_preserved(history):
    repo, _, _ = history
    names = ['a' * 40, 'tab\tname', 'line\nname', '目标']
    for name in names:
        (repo.path / name).write_text('path fixture')
    commit(repo.path, 'paths')
    assert set(repo.read_commit(repo.resolve('HEAD'))['paths']) == set(names)


def test_sha256_object_format_is_not_truncated(tmp_path):
    git(tmp_path, 'init', '-q', '--object-format=sha256')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    git(tmp_path, 'config', 'user.email', 'fixture@example.org')
    (tmp_path / 'file').write_text('sha256')
    commit(tmp_path, 'sha256')
    repo = Repository(tmp_path)
    value = repo.resolve('HEAD')
    assert len(value) == 64
    assert repo.read_commit(value)['oid'] == value


def test_parent_header_is_not_rewritten_by_shallow_boundaries(history):
    repo, first, second = history
    before = repo.read_commit(second)
    (repo.path / '.git' / 'shallow').write_text(second + '\n')
    other = Repository(repo.path)
    after = other.read_commit(second)
    assert after['parents'] == [first]
    assert after['paths'] == before['paths'] == ['a']
    assert other.patch_id(second) == repo.patch_id(second)


def test_deadline_and_error_output_are_bounded_without_leaking_diagnostics(history, monkeypatch, tmp_path):
    import os
    import sys
    import time

    repo, _, _ = history
    tools = tmp_path / 'tools'
    tools.mkdir()
    fake = tools / 'git'
    fake.write_text('#!' + sys.executable + "\nimport time\ntime.sleep(30)\n")
    fake.chmod(0o700)
    monkeypatch.setenv('PATH', str(tools) + os.pathsep + os.environ['PATH'])
    repo.timeout = 0.05
    started = time.monotonic()
    with pytest.raises(GitError, match='timeout'):
        repo.resolve('HEAD')
    assert time.monotonic() - started < 5
    fake.write_text('#!' + sys.executable + "\nimport sys\nsys.stderr.write('credential-secret-' * 10000)\n")
    repo.timeout = 5
    with pytest.raises(GitError, match='output_limit') as failure:
        repo.resolve('HEAD')
    assert 'credential-secret' not in str(failure.value)


def test_fetch_refuses_inherited_repo_transport_configuration(tmp_path):
    git(tmp_path, 'init', '-q', '--bare')
    config = tmp_path / 'config'
    config.write_text(config.read_text() + '\n[http]\n\tproxy = http://not-approved.invalid\n')
    repo = Repository(tmp_path)
    with pytest.raises(GitError, match='unsafe_cache_config'):
        repo.fetch('https://example.org/repo.git', 'refs/heads/main')


def test_cache_symlinks_and_global_object_alternates_are_rejected(history, tmp_path):
    repo, _, _ = history
    alias = tmp_path / 'alias'
    alias.symlink_to(repo.path, target_is_directory=True)
    with pytest.raises(GitError, match='cache_symlink'):
        Repository(alias)
    bare = tmp_path / 'bare'
    bare.mkdir()
    git(bare, 'init', '-q', '--bare')
    (bare / 'objects/info/alternates').write_text(str(repo.path / '.git/objects') + '\n')
    with pytest.raises(GitError, match='cache_alternates'):
        Repository(bare).fetch('https://example.org/repo.git', 'refs/heads/main')
