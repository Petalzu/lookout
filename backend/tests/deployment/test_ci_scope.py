"""Unknown inputs must not bypass the image release gate."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('ci_scope', ROOT / 'scripts/ci-scope.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize('paths, expected', [
    (['README.md', 'docs/deployment.md'], False),
    (['README.md', 'Containerfile'], True),
    (['docs/assets.md'], True),
    (['frontend/README.md'], True),
    (['.github/workflows/ci.yml'], True),
    ([], True),
    (None, True),
])
def test_scope_fails_closed(paths, expected):
    assert ci.needs_full(paths) is expected
    assert ci.needs_full(paths, release=True) is True


def test_git_diff_covers_all_commits_and_renames(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (tmp_path / 'code.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    git('mv', 'code.py', 'README.md')
    git('commit', '-qm', 'rename')
    (tmp_path / 'CONTRIBUTING.md').write_text('Contribute.\n')
    git('add', '.')
    git('commit', '-qm', 'docs')
    paths = ci.changed_paths({'before': base}, root=tmp_path)
    assert set(paths) == {'code.py', 'README.md', 'CONTRIBUTING.md'}
    assert ci.needs_full(paths)
    assert ci.changed_paths({'pull_request': {'base': {'sha': base}}}, root=tmp_path) == paths


@pytest.mark.parametrize('event', [{}, {'before': '0' * 40}, {'before': '--help'}, {'before': 'f' * 40}])
def test_missing_or_invalid_base_requires_full_checks(tmp_path, event):
    assert ci.changed_paths(event, root=tmp_path) is None


def test_prebuilt_test_image_is_used_without_rebuilding(tmp_path):
    engine = tmp_path / 'docker'
    engine.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ENGINE_ARGS"\n')
    engine.chmod(0o755)
    import os
    output = tmp_path / 'args'
    subprocess.run(
        ['sh', str(ROOT / 'deploy/check-image.sh'), 'runtime:test', 'checks:test'],
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
             'CONTAINER_ENGINE': 'docker', 'ENGINE_ARGS': str(output)},
        check=True,
    )
    args = output.read_text().splitlines()
    assert args[0] == 'run'
    assert 'checks:test' in args
    assert 'runtime:test' not in args
    assert '--network' in args and 'none' in args
