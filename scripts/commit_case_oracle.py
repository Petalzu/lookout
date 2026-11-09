"""Raw-Git oracle and provenance helpers; deliberately independent of tracker analysis."""
import base64
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

ORACLE_VERSION = 'raw-git-full-bytes-v1'
FULL_OID = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(root):
    root = Path(root).resolve()
    command = ['git', '-c', f'safe.directory={root}', '-C', str(root)]
    head = subprocess.check_output([*command, 'rev-parse', 'HEAD']).decode().strip()
    changed = subprocess.check_output([*command, 'status', '--porcelain=v1', '-z', '--untracked-files=all'])
    files = []
    records = iter(changed.split(b'\0'))
    for record in records:
        if not record:
            continue
        name = os.fsdecode(record[3:])
        path = root / name
        item = {'status': record[:2].decode(), 'path': name,
                'sha256': sha256(path.read_bytes()) if path.is_file() else None}
        if record[:1] in (b'R', b'C'):
            item['original_path'] = os.fsdecode(next(records))
        files.append(item)
    return {'head': head, 'dirty_files': files,
            'dirty_digest': sha256(json.dumps(files, sort_keys=True).encode())}


def environment():
    cpu = None
    if Path('/proc/cpuinfo').exists():
        cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                    if line.startswith('model name')), None)
    return {'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(),
            'cpu_model': cpu, 'affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
            'git': subprocess.check_output(['git', '--version']).decode().strip(),
            'command': shlex.join([sys.executable, *sys.argv]),
            'environment': {key: os.environ.get(key) for key in
                            ('HOME', 'TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME', 'UV_CACHE_DIR', 'PYTHONDONTWRITEBYTECODE')}}


class RawGit:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.commands = []
        self._files = {}
        self._patches = {}

    def run(self, *args, input_data=None, allowed=(0,)):
        command = ['git', '-c', f'safe.directory={self.path}', '-c', 'core.hooksPath=/dev/null',
                   '-c', 'credential.helper=', '-c', 'gc.auto=0', '-c', 'core.attributesFile=/dev/null',
                   '-c', 'diff.algorithm=myers', '-c', 'diff.indentHeuristic=false',
                   '-C', str(self.path), *args]
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0',
                   GIT_ATTR_NOSYSTEM='1', LC_ALL='C.UTF-8')
        result = subprocess.run(command, input=input_data, capture_output=True, timeout=90, env=env, check=False)
        self.commands.append({'argv': command, 'returncode': result.returncode,
                              'stdin_sha256': sha256(input_data) if input_data is not None else None,
                              'stdout_sha256': sha256(result.stdout), 'stdout_bytes': len(result.stdout),
                              'stderr_sha256': sha256(result.stderr)})
        if result.returncode not in allowed:
            raise RuntimeError(f'raw Git failed ({result.returncode}): {shlex.join(args)}: {result.stderr[:160]!r}')
        return result.stdout, result.returncode

    def text(self, *args):
        return self.run(*args)[0].decode('utf-8').strip()

    def ancestor(self, first, last):
        return self.run('merge-base', '--is-ancestor', first, last, allowed=(0, 1))[1] == 0

    def file_evidence(self, oid):
        if oid in self._files:
            return self._files[oid]
        raw = self.run('cat-file', 'commit', oid)[0]
        header, _, _ = raw.partition(b'\n\n')
        parents = [line[7:].decode() for line in header.splitlines() if line.startswith(b'parent ')]
        if len(parents) != 1:
            self._files[oid] = None
            return None
        parent = parents[0]
        parts = self.run('diff-tree', '-r', '--raw', '-z', '--no-commit-id', '--no-renames',
                         '--no-abbrev', parent, oid, '--')[0].split(b'\0')
        evidence, raw_files = [], []
        for pos in range(0, len(parts) - 1, 2):
            metadata, path = parts[pos:pos + 2]
            before_mode, after_mode, before_oid, after_oid, _ = metadata[1:].split()
            if any(mode not in (b'000000', b'100644', b'100755') for mode in (before_mode, after_mode)):
                self._files[oid] = None
                return None
            blobs = []
            row = {'path_b64': base64.b64encode(path).decode(), 'before_mode': before_mode.decode(),
                   'after_mode': after_mode.decode()}
            for label, blob_oid in [('before', before_oid), ('after', after_oid)]:
                data = None if set(blob_oid) == {48} else self.run('cat-file', 'blob', blob_oid.decode())[0]
                if data is not None:
                    try:
                        data.decode('utf-8')
                    except UnicodeDecodeError:
                        self._files[oid] = None
                        return None
                    if b'\0' in data:
                        self._files[oid] = None
                        return None
                row[label] = {'oid': None if data is None else blob_oid.decode(),
                              'sha256': None if data is None else sha256(data),
                              'bytes': None if data is None else len(data)}
                blobs.append(data)
            evidence.append(row)
            raw_files.append((path, before_mode, after_mode, *blobs))
        result = {'commit': oid, 'parent': parent, 'files': sorted(evidence, key=lambda row: base64.b64decode(row['path_b64'])),
                  'raw_files': sorted(raw_files)} if evidence else None
        self._files[oid] = result
        return result

    def patch_id(self, oid):
        if oid not in self._patches:
            header = self.run('cat-file', 'commit', oid)[0].partition(b'\n\n')[0]
            parents = [line[7:].decode() for line in header.splitlines() if line.startswith(b'parent ')]
            if len(parents) != 1:
                self._patches[oid] = None
                return None
            patch = self.run('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                             '--binary', parents[0], oid, '--')[0]
            result = self.run('patch-id', '--stable', input_data=patch)[0].split()
            self._patches[oid] = result[0].decode() if result else None
        return self._patches[oid]

    def expected(self, case, limit):
        command_start = len(self.commands)
        candidate, target, start = (self.text('rev-parse', '--verify', case[key] + '^{commit}')
                                    for key in ('candidate', 'target', 'start'))
        identity = {'candidate': candidate, 'target': target, 'start': start}
        if identity != {key: case[key] for key in identity}:
            raise ValueError('noncanonical identity')
        if not self.ancestor(start, target):
            raise ValueError('oracle requires a valid bounded window')
        history = self.text('rev-list', '--reverse', '--topo-order', f'--max-count={limit + 1}',
                            target, '^' + start, '--').splitlines()
        if len(history) > limit:
            raise ValueError('oracle window exceeds declared history_limit')
        shallow = self.text('rev-parse', '--is-shallow-repository') == 'true'
        boundaries = set()
        if shallow:
            location = Path(self.text('rev-parse', '--git-path', 'shallow'))
            location = location if location.is_absolute() else self.path / location
            boundaries = set(location.read_text().splitlines())
        reachable = self.ancestor(candidate, target)
        ancestry = reachable or not shallow or self.ancestor(target, candidate)
        original = self.file_evidence(candidate)
        patch = self.patch_id(candidate) if original and not reachable else None
        strict, associations, revert_pairs, proofs = [], set(), {}, []
        for oid in history:
            raw = self.run('cat-file', 'commit', oid)[0]
            message = raw.partition(b'\n\n')[2].decode('utf-8', 'replace')
            if not reachable:
                tokens = re.findall(r'(?<![a-f0-9])[a-f0-9]{7,64}(?![a-f0-9])', message)
                for token in tokens:
                    if candidate.startswith(token) and self.text('rev-parse', '--verify', token + '^{commit}') == candidate:
                        associations.add(oid)
                if patch and self.patch_id(oid) == patch:
                    other = self.file_evidence(oid)
                    if other and original['raw_files'] == other['raw_files']:
                        strict.append(oid)
                        proofs.append({'candidate': {k: v for k, v in original.items() if k != 'raw_files'},
                                       'matched': {k: v for k, v in other.items() if k != 'raw_files'},
                                       'full_bytes_equal': True, 'stable_patch_id': patch})
                    else:
                        associations.add(oid)
            for reverted in re.findall(r'This reverts commit ([a-f0-9]{40}|[a-f0-9]{64})\.', message):
                revert_pairs[oid] = reverted
        affected = {candidate, *strict, *associations}
        reverts = []
        for oid, reverted in revert_pairs.items():
            if reverted in affected:
                affected.add(oid)
                reverts.append(oid)
        complete = not boundaries.intersection(history)
        status, reason, matches = 'no_evidence_in_range', None, []
        if reachable:
            status, matches = 'original_reachable', [candidate]
        elif strict:
            status, matches = 'strict_patch_evidence', strict
        elif associations:
            status, reason, matches = 'backport_needs_review', 'association_only', sorted(associations)
        elif not original:
            status, reason = 'unknown', 'unsupported_commit_shape'
        elif not complete or not ancestry:
            status, reason, complete = 'unknown', 'shallow_history', False
        return {'identity': identity, 'status': status, 'reason': reason, 'matches': matches, 'reverts': reverts,
                'coverage': {'target': target, 'start': start, 'limit': limit, 'checked': len(history),
                             'complete': bool(complete), 'ancestry_complete': bool(ancestry)},
                'proofs': proofs,
                'candidate_change': {k: v for k, v in original.items() if k != 'raw_files'} if original else None,
                'association_changes': [{k: v for k, v in (self.file_evidence(oid) or {'commit': oid}).items()
                                         if k != 'raw_files'} for oid in sorted(associations - set(strict))],
                'source': {'method': ORACLE_VERSION, 'label_kind': 'machine_raw_git_not_human_reviewed',
                           'history': history, 'commands': self.commands[command_start:]}}
