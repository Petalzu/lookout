"""Bounded Git object IO; no worktree, hooks or repository code execution."""
import configparser
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from tracker.monitors.commits.model import https_url, oid


class GitError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Repository:
    def __init__(self, path, *, timeout=60, output_limit=8388608, disk_limit=1073741824):
        supplied = Path(path).absolute()
        if any(parent.is_symlink() for parent in (supplied, *supplied.parents)):
            raise GitError('cache_symlink')
        self.path = supplied.resolve()
        self.timeout, self.output_limit, self.disk_limit = timeout, output_limit, disk_limit
        self.deadline: float | None = None
        self._commit_cache = {}
        self._patch_cache = {}
        self._checked_disk_limit = None
        self._fetched_refs = {}

    def begin_round(self):
        self.deadline = time.monotonic() + self.timeout
        self._fetched_refs.clear()

    def _size(self):
        total = 0
        for parent, directories, names in os.walk(self.path, followlinks=False):
            if any((Path(parent) / name).is_symlink() for name in directories):
                raise GitError('cache_symlink')
            for name in names:
                path = Path(parent) / name
                if path.is_symlink():
                    raise GitError('cache_symlink')
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    continue
        return total

    def _execute(self, args, input_data=None):
        if not self.path.is_dir() or self.path.is_symlink():
            raise GitError('cache_missing')
        config = ['-c', f'safe.directory={self.path}', '-c', 'core.hooksPath=/dev/null',
                  '-c', 'credential.helper=', '-c', 'protocol.allow=never',
                  '-c', 'protocol.https.allow=always', '-c', 'http.followRedirects=false',
                  '-c', 'http.sslVerify=true', '-c', 'fetch.recurseSubmodules=false',
                  '-c', 'gc.auto=0', '-c', 'core.fsmonitor=false',
                  '-c', 'log.showSignature=false', '-c', 'core.attributesFile=/dev/null',
                  '-c', 'diff.algorithm=myers', '-c', 'diff.indentHeuristic=false',
                  '-c', 'diff.ignoreSubmodules=none', '-c', 'diff.noprefix=false']
        environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
                       if k in os.environ}
        environment.update(LC_ALL='C.UTF-8', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                           GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1',
                           GIT_OPTIONAL_LOCKS='0', GIT_ATTR_NOSYSTEM='1')
        command = ['git', *config, '-C', str(self.path), *args]
        started = time.monotonic()
        timeout = min(self.timeout, self.deadline - started) if self.deadline is not None else self.timeout
        if timeout <= 0:
            raise GitError('batch_timeout')
        writes_objects = args[0] in ('fetch', 'init')
        if writes_objects or self._checked_disk_limit != self.disk_limit:
            if self._size() > self.disk_limit:
                raise GitError('disk_limit')
            self._checked_disk_limit = self.disk_limit
        source = None
        if input_data is not None:
            if len(input_data) > self.output_limit:
                raise GitError('input_limit')
            source = tempfile.TemporaryFile(dir=self.path.parent)
            source.write(input_data)
            source.seek(0)
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=source if source is not None else subprocess.DEVNULL,
                                       env=environment, start_new_session=True)
        except OSError as error:
            if source is not None:
                source.close()
            raise GitError('git_unavailable') from error
        assert process.stdout is not None and process.stderr is not None
        output, diagnostics = bytearray(), bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostics)
                next_disk_check = started
                while selector.get_map():
                    now = time.monotonic()
                    if now - started >= timeout:
                        raise GitError('timeout')
                    if writes_objects and now >= next_disk_check:
                        if self._size() > self.disk_limit:
                            raise GitError('disk_limit')
                        next_disk_check = now + 0.25
                    for key, _ in selector.select(min(0.1, timeout)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(chunk)
                        if len(output) > self.output_limit or len(diagnostics) > 65536:
                            raise GitError('output_limit')
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise GitError('timeout')
                try:
                    code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as error:
                    raise GitError('timeout') from error
            if writes_objects and self._size() > self.disk_limit:
                raise GitError('disk_limit')
            return code, bytes(output)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if source is not None:
                source.close()
            process.stdout.close()
            process.stderr.close()

    def run(self, *args):
        if not args or args[0] not in {'init', 'fetch', 'rev-parse', 'merge-base', 'rev-list', 'show',
                                       'diff-tree', 'diff', 'cat-file', 'update-ref', 'patch-id'}:
            raise GitError('unsupported_operation')
        code, output = self._execute(args)
        if code:
            raise GitError('git_failed')
        return output

    def resolve(self, ref):
        if not isinstance(ref, str) or ref.startswith('-') or any(c in ref for c in '\r\n\x00'):
            raise GitError('invalid_ref')
        try:
            return oid(self.run('rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip())
        except ValueError as error:
            raise GitError('invalid_object') from error

    def is_ancestor(self, candidate, target):
        oid(candidate)
        oid(target)
        code, _ = self._execute(('merge-base', '--is-ancestor', candidate, target))
        if code not in (0, 1):
            raise GitError('ancestry_failed')
        return code == 0

    def is_shallow(self):
        return self.run('rev-parse', '--is-shallow-repository').strip() == b'true'

    def range_complete(self, commits):
        if not self.is_shallow():
            return True
        name = self.run('rev-parse', '--git-path', 'shallow').decode().strip()
        shallow = Path(name)
        shallow = shallow if shallow.is_absolute() else self.path / shallow
        resolved = shallow.resolve()
        if not resolved.is_relative_to(self.path) or shallow.is_symlink():
            raise GitError('shallow_path')
        try:
            boundaries = set(resolved.read_text().splitlines())
        except OSError as error:
            raise GitError('shallow_metadata') from error
        return not boundaries.intersection(commits)

    def commits(self, tip, *, exclude=None, limit=2000):
        oid(tip)
        args = ['rev-list', '--reverse', '--topo-order', f'--max-count={limit + 1}', tip]
        if exclude:
            args += ['^' + oid(exclude)]
        result = self.run(*args, '--').decode().splitlines()
        if len(result) > limit:
            raise GitError('commit_limit')
        return [oid(value) for value in result]

    def read_commit(self, value):
        value = oid(value)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise GitError('batch_timeout')
        if value in self._commit_cache:
            return self._commit_cache[value]
        raw = self.run('cat-file', 'commit', value)
        if len(raw) > 1048576:
            raise GitError('commit_record_limit')
        header, separator, message = raw.partition(b'\n\n')
        if not separator:
            raise GitError('invalid_commit')
        headers = header.splitlines()
        parents = [oid(line[7:].decode('ascii')) for line in headers if line.startswith(b'parent ')]
        committer = next((line for line in headers if line.startswith(b'committer ')), b'')
        timestamp = re.search(rb' (-?[0-9]+) [+-][0-9]{4}$', committer)
        if timestamp is None:
            raise GitError('invalid_commit')
        content = message.decode('utf-8', errors='replace').rstrip('\n')
        subject, _, body = content.partition('\n')
        paths = set()
        for parent in parents or [None]:
            revisions = [parent, value] if parent else ['--root', value]
            changed = self.run('diff-tree', '-r', '--no-commit-id', '--no-renames', '--name-only',
                               '-z', *revisions, '--')
            paths.update(p.decode('utf-8', errors='backslashreplace') for p in changed.split(b'\x00') if p)
        try:
            committed_at = datetime.fromtimestamp(int(timestamp[1]), UTC).isoformat(timespec='seconds')
        except (ValueError, OverflowError, OSError) as error:
            raise GitError('invalid_commit_time') from error
        result = dict(oid=value, parents=parents, subject=subject, body=body.lstrip('\n'),
                      committed_at=committed_at, paths=sorted(paths))
        if len(self._commit_cache) >= 256:
            self._commit_cache.pop(next(iter(self._commit_cache)))
        self._commit_cache[value] = result
        return result

    def patch_id(self, value):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise GitError('batch_timeout')
        if value in self._patch_cache:
            return self._patch_cache[value]
        parents = self.read_commit(oid(value))['parents']
        if len(parents) != 1:
            return None
        patch = self.run('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                         '--binary', parents[0], value, '--')
        code, output = self._execute(('patch-id', '--stable'), patch)
        if code:
            raise GitError('patch_id_failed')
        result = output.split()[0].decode() if output.split() else None
        if len(self._patch_cache) >= 4096:
            self._patch_cache.pop(next(iter(self._patch_cache)))
        self._patch_cache[value] = result
        return result

    def fetch(self, url, ref, *, depth=2048, object_format='sha1'):
        https_url(url)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise GitError('batch_timeout')
        if (url, ref, depth) in self._fetched_refs:
            return self._fetched_refs[url, ref, depth]
        if object_format not in ('sha1', 'sha256'):
            raise GitError('invalid_object_format')
        if type(depth) is not int or not 1 <= depth <= 100000:
            raise GitError('invalid_depth')
        valid = re.fullmatch(r'refs/(heads|tags)/[A-Za-z0-9_./-]+|[a-f0-9]{40}|[a-f0-9]{64}', ref)
        if not valid or '..' in ref:
            raise GitError('invalid_ref')
        self.path.mkdir(parents=True, exist_ok=True)
        if not (self.path / 'HEAD').exists():
            self.run('init', '--bare', '--quiet', '--template=', '--object-format=' + object_format)
        config = configparser.RawConfigParser()
        try:
            config.read_string((self.path / 'config').read_text())
        except (OSError, UnicodeError, configparser.Error) as error:
            raise GitError('invalid_cache_config') from error
        allowed = {'core': {'repositoryformatversion', 'filemode', 'bare'}, 'extensions': {'objectformat'}}
        if any(section not in allowed or set(config[section]) - allowed[section] for section in config.sections()):
            raise GitError('unsafe_cache_config')
        if config.get('extensions', 'objectformat', fallback='sha1') != object_format:
            raise GitError('object_format_changed')
        if config.get('core', 'bare', fallback='false').lower() != 'true':
            raise GitError('cache_not_bare')
        if (self.path / 'objects' / 'info' / 'alternates').exists():
            raise GitError('cache_alternates')
        self.run('fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
                 f'--depth={depth}', url, f'+{ref}:refs/lookout/fetched')
        value = self.resolve('refs/lookout/fetched')
        self._fetched_refs[url, ref, depth] = value
        return value
