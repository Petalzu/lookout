"""Small deterministic Git fixture operations; assertions stay in each test."""
import subprocess


def git(path, *args):
    return subprocess.check_output(['git', '-c', f'safe.directory={path}', '-C', str(path), *args],
                                   stderr=subprocess.DEVNULL).decode().strip()


def init(path, *, object_format='sha1'):
    path.mkdir(exist_ok=True)
    git(path, 'init', '-qb', 'main', '--object-format=' + object_format)
    git(path, 'config', 'user.name', 'Fixture')
    git(path, 'config', 'user.email', 'fixture@example.org')


def commit(path, subject):
    git(path, 'add', '.')
    git(path, 'commit', '-qm', subject)
    return git(path, 'rev-parse', 'HEAD')
