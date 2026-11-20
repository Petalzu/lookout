"""Container operations shared by installation and release upgrades.

Docker's container and Quadlet's unit remain the runtime configuration owners.
This module validates those inputs; it does not invent another service format.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import pwd
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from credentials import github_token

PYTHON = '/opt/venv/bin/python'
LABEL = 'org.openruyi.instance'
LIMITS = ['--memory', '8g', '--cpus', '4', '--pids-limit', '512']
PROTECTION = ['--read-only', '--cap-drop=all', '--security-opt=no-new-privileges',
              '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777']


def bind_address(value):
    address = ipaddress.ip_address(value)
    if address.is_multicast:
        raise ValueError('listener must be a unicast or wildcard IP address')
    return str(address)


def published_port(address, port):
    address = bind_address(address)
    if not 1 <= int(port) <= 65535:
        raise ValueError('invalid published port')
    return f'{"[" + address + "]" if ":" in address else address}:{port}:8080'


def user_environment(environment=None):
    result = dict(os.environ if environment is None else environment)
    uid = os.geteuid()
    runtime = Path('/run/user') / str(uid)
    if uid and runtime.is_dir():
        result.update(HOME=pwd.getpwuid(uid).pw_dir, XDG_RUNTIME_DIR=str(runtime),
                      DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(runtime / 'bus'))
    return result


def run(argv, *, timeout=120, input=None, env=None, retry_transport=False):
    deadline = time.monotonic() + timeout
    for attempt in range(3 if retry_transport else 1):
        budget = max(0.001, deadline - time.monotonic()) if retry_transport else timeout
        result = subprocess.run(argv, input=input, text=not isinstance(input, bytes),
                                capture_output=True, timeout=budget, env=user_environment(env),
                                cwd=pwd.getpwuid(os.geteuid()).pw_dir if os.geteuid() and (Path("/run/user") / str(os.geteuid())).is_dir() else None)
        if not result.returncode:
            output = result.stdout
            return (output.decode() if isinstance(output, bytes) else output).strip()
        stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
        transient = re.search(r'\beof\b|connection reset|connection timed out|TLS handshake timeout|i/o timeout',
                              stderr or '', re.IGNORECASE)
        delay = 2 ** attempt
        if not retry_transport or not transient or attempt == 2 or time.monotonic() + delay >= deadline:
            # A provider proxy or a configuration error may contain credentials.
            raise RuntimeError(f'{argv[0]} {argv[1]} failed (exit {result.returncode})')
        time.sleep(delay)
    raise AssertionError('registry retry budget exhausted')


def image_reference(reference):
    local = re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', reference)
    registry = re.fullmatch(r'[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9_./-]+(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-f]{64})', reference)
    host = reference.split('/', 1)[0]
    if registry and host != 'localhost' and '.' not in host and ':' not in host:
        registry = None
    if not local and not registry:
        raise ValueError('use a fully qualified registry image with tag/digest, or a local sha256 image ID')
    return bool(registry)


def validate_catalog_baseline(reference):
    """Reject an ambiguous original-image identity before starting an upgrade."""
    if image_reference(reference) and '@sha256:' not in reference:
        raise ValueError('catalog baseline requires the immutable original installation image, not a moving tag')



def unchanged_image(engine, reference, current):
    """Compare registry metadata before pulling layers; failures leave the service alone."""
    if not image_reference(reference):
        return 'sha256:' + reference.removeprefix('sha256:') == current
    info = json.loads(run([engine, 'image', 'inspect', current]))[0]
    manifest = json.loads(run([engine, 'manifest', 'inspect', reference], timeout=60, retry_transport=True))
    # Podman projects single-image manifests as a list with manifests=null.
    # Skopeo preserves the configuration digest without downloading layers.
    if engine == 'podman' and manifest.get('manifests', []) is None:
        manifest = json.loads(run(['skopeo', 'inspect', '--raw', 'docker://' + reference],
                                  timeout=60, retry_transport=True))
    if isinstance(manifest.get('manifests'), list):
        candidates = [entry for entry in manifest['manifests']
                      if entry.get('platform', {}).get('os') == info['Os']
                      and entry.get('platform', {}).get('architecture') == info['Architecture']
                      and (not info.get('Variant') or entry['platform'].get('variant') == info['Variant'])]
        if len(candidates) != 1:
            raise ValueError('registry manifest must identify exactly one matching runtime platform')
        digest = candidates[0]['digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('registry platform manifest digest is invalid')
        host, name = reference.split('/', 1)
        repository = host + '/' + name.split('@', 1)[0].rsplit(':', 1)[0]
        manifest = json.loads(run([engine, 'manifest', 'inspect', repository + '@' + digest], timeout=60, retry_transport=True))
    digest = manifest.get('config', {}).get('digest')
    if manifest.get('schemaVersion') != 2 or not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('registry image configuration digest is invalid')
    return digest == current


def resolve_image(engine, reference):
    """Pull a registry reference once; subsequent operations use its local immutable ID."""
    registry = image_reference(reference)
    if registry:
        # The host scheduler retries failed upgrades; avoid nested pull retries.
        options = ['--retry=0'] if engine == 'podman' else []
        run([engine, 'pull', *options, reference], timeout=600)
    info = json.loads(run([engine, 'image', 'inspect', reference]))[0]
    labels = info['Config'].get('Labels') or {}
    version = labels.get('org.opencontainers.image.version', '')
    revision = labels.get('org.opencontainers.image.revision', '')
    if (info['Os'] != 'linux' or info['Config'].get('User') != '10001:10001'
            or not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or not re.fullmatch(r'[0-9a-f]{40}', revision)):
        raise ValueError('image must declare its application version, source revision and non-root identity')
    image = 'sha256:' + info['Id'].removeprefix('sha256:')
    code = ('import json,tomllib; from tracker import storage; '
            'p=tomllib.load(open("/app/backend/pyproject.toml","rb")); '
            'print(json.dumps([p["project"]["version"], storage.FORMAT]))')
    actual = json.loads(run([engine, 'run', '--rm', '--network', 'none', *PROTECTION,
                            '--entrypoint', PYTHON, image, '-c', code]))
    if actual[0] != version or type(actual[1]) is not int or actual[1] < 1:
        raise ValueError('image application/storage version does not match its metadata')
    return dict(version=version, revision=revision, image=image, storage=actual[1],
                platform=info['Os'] + '/' + info['Architecture'],
                reference=reference, digests=info.get('RepoDigests') or [])


def export_image_tree(engine, image, source, destination):
    """Copy a trusted pinned image tree without running it or mounting host state."""
    if destination.exists() or destination.is_symlink():
        raise ValueError('image export destination must be new')
    helper = 'lookout-export-' + uuid.uuid4().hex[:12]
    run([engine, 'create', '--name', helper, '--network', 'none', *PROTECTION,
         '--entrypoint', '/bin/true', image])
    try:
        run([engine, 'cp', f'{helper}:{source}', str(destination)])
        if (destination.is_symlink() or not destination.is_dir()
                or any(path.is_symlink() or not (path.is_file() or path.is_dir())
                       for path in destination.rglob('*'))):
            raise ValueError('release tools/catalogs must contain only regular files and directories')
    finally:
        run([engine, 'rm', helper])


def instance_lock(service):
    if service.engine == 'podman':
        return service.settings['data'] / '.upgrade.lock'
    daemon = json.loads(run(['docker', 'info', '--format', '{{json .ID}}']))
    identity = hashlib.sha256((daemon + ':' + service.instance).encode()).hexdigest()
    root = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'lookout/locks'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / (identity + '.lock')


@contextmanager
def upgrade_lock(path):
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def publish_file(staged, destination, *, replace=False):
    """Sync and publish a staged file; only reviewed replacements may overwrite.

    Staging must be on the destination filesystem. A failed directory sync is
    reported even if publication succeeded; callers must not assume no file exists.
    """
    with staged.open('rb') as stream:
        os.fsync(stream.fileno())
    if replace:
        os.replace(staged, destination)
    else:
        os.link(staged, destination)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_exclusive(path, data):
    """Publish a fully synced file without overwriting a concurrently created one."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        publish_file(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def healthy(engine, name, image, *, timeout=90):
    deadline = time.monotonic() + timeout
    probe = ('import urllib.request; '
             '[urllib.request.urlopen("http://127.0.0.1:8080/"+p,timeout=3).read() '
             'for p in ("livez","readyz")]')
    while time.monotonic() < deadline:
        try:
            info = json.loads(run([engine, 'inspect', name], timeout=5))[0]
            if info['Image'].removeprefix('sha256:') != image.removeprefix('sha256:'):
                raise ValueError('running image does not match the selected release')
            if info.get('State', {}).get('Status') in ('exited', 'dead', 'restarting'):
                raise ValueError('application exited before becoming ready')
            run([engine, 'exec', name, PYTHON, '-c', probe], timeout=10)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(1)
    raise RuntimeError('release did not become live and ready within the startup budget')


def no_data_users(service):
    for ident in run([service.engine, 'ps', '-q']).split():
        info = json.loads(run([service.engine, 'inspect', ident]))[0]
        # A listed container may have exited since the engine's list snapshot.
        if info.get('State', {}).get('Status') == 'exited':
            continue
        for mount in info.get('Mounts', []):
            if service.engine == 'docker':
                overlap = mount.get('Name') == service.settings['data']
            else:
                source = Path(mount.get('Source', '/nonexistent')).resolve()
                data = service.settings['data']
                overlap = source == data or source in data.parents or data in source.parents
            if overlap:
                raise RuntimeError('another running container mounts this data directory')


def image_command(service, image, script, *args, backup_dir=None):
    command = [service.engine, 'run', '--rm', '--network', 'none', *PROTECTION]
    if service.engine == 'podman':
        command += ['--userns=keep-id:uid=10001,gid=10001']
    for role in ('config', 'data'):
        source = service.settings[role]
        if service.engine == 'docker':
            value = f'type=volume,src={source},dst=/{role},volume-nocopy'
            command += ['--mount', value + (',readonly' if role == 'config' else '')]
        else:
            command += ['-v', f'{source}:/{role}:' + ('ro,Z' if role == 'config' else 'Z')]
    if backup_dir:
        if service.engine == 'docker':
            command += ['--mount', f'type=volume,src={service.backup_volume},dst=/backup,volume-nocopy']
        else:
            command += ['-v', f'{backup_dir}:/backup:Z']
    if credential := service.settings.get('github_env_file'):
        github_token(credential)
        command += ['--env-file', str(credential)]
    for value in service.settings.get('environment', []):
        command += ['-e', value]
    return command + ['--entrypoint', PYTHON, image, script, *args]


class Docker:
    engine = 'docker'

    def __init__(self, name):
        self.info = json.loads(run(['docker', 'inspect', name]))[0]
        self.name = self.info['Name'].lstrip('/')
        config, host = self.info['Config'], self.info['HostConfig']
        self.instance = (config.get('Labels') or {}).get(LABEL)
        if (not self.instance or config['User'] != '10001:10001'
                or not host['ReadonlyRootfs'] or host.get('Privileged')
                or set(host.get('CapDrop') or []) != {'ALL'}
                or not any(v.startswith('no-new-privileges') for v in host.get('SecurityOpt') or [])
                or host.get('NetworkMode') not in ('default', 'bridge', 'none')
                or host.get('CapAdd') or host.get('Devices') or host.get('Binds')):
            raise ValueError('container does not match the installed Docker runtime contract')
        mounts = {m['Destination']: m for m in self.info['Mounts'] if m['Type'] != 'tmpfs'}
        if set(mounts) != {'/config', '/data'}:
            raise ValueError('Docker deployment needs exactly its config and data volumes')
        for path, mount in mounts.items():
            if mount['Type'] != 'volume' or mount['RW'] != (path == '/data'):
                raise ValueError('invalid Docker persistent mount')
            labels = json.loads(run(['docker', 'volume', 'inspect', mount['Name']]))[0].get('Labels') or {}
            if labels.get(LABEL) != self.instance:
                raise ValueError('volume identity does not match the installed instance')
        self.backup_volume = config['Labels'].get('org.openruyi.backups')
        if not self.backup_volume:
            raise ValueError('installed backup volume is missing')
        labels = json.loads(run(['docker', 'volume', 'inspect', self.backup_volume]))[0].get('Labels') or {}
        if labels.get(LABEL) != self.instance:
            raise ValueError('backup volume identity does not match the installed instance')
        image_config = json.loads(run(['docker', 'image', 'inspect', self.info['Image']]))[0]['Config']
        if any(config.get(key) != image_config.get(key) for key in ('Entrypoint', 'Cmd')):
            raise ValueError('custom application entrypoint requires operator review')
        image_env = image_config.get('Env') or []
        explicit_env = json.loads(config['Labels'].get('org.openruyi.environment', '[]'))
        self.settings = dict(image=self.info['Image'], name=self.name,
            config=mounts['/config']['Name'], data=mounts['/data']['Name'],
            environment=[e for e in config.get('Env') or []
                         if e not in image_env or e.split('=', 1)[0] in explicit_env])
        if any(e.split('=', 1)[0] in {'TRACKER_DB', 'TRACKER_CONFIG', 'PORT', 'API_PORT', 'HOST'}
               and e not in {'HOST=0.0.0.0', 'PORT=8080', 'API_PORT=18731'} for e in self.settings['environment']):
            raise ValueError('custom database or listener requires operator review')
        credential = config['Labels'].get('org.openruyi.github-env-file')
        if credential:
            github_token(credential)
            self.settings['github_env_file'] = credential
            self.settings['environment'] = [v for v in self.settings['environment'] if not v.startswith('LOOKOUT_GITHUB_TOKEN=')]
        self.settings['catalog_image'] = config['Labels'].get('org.openruyi.catalog-image')
        self.previous = None
        if not host.get('Memory') or not host.get('NanoCpus') or not host.get('PidsLimit'):
            raise ValueError('installed container is missing resource limits')
        for port, bindings in (host.get('PortBindings') or {}).items():
            if port != '8080/tcp':
                raise ValueError('publish only the web port 8080')
            for binding in bindings:
                published_port(binding['HostIp'], binding['HostPort'])

    def stop(self):
        if run(['docker', 'ps', '-aq', '--filter', 'name=^/' + self.name + '$']):
            run(['docker', 'stop', '--time', '20', self.name], timeout=40)

    def stage(self, image, *, port=None):
        self.previous = self.name + '-previous-' + uuid.uuid4().hex[:10]
        run(['docker', 'rename', self.name, self.previous])
        h = self.info['HostConfig']
        command = ['docker', 'create', '--name', self.name, *PROTECTION,
                   '--memory', str(h['Memory']), '--cpus', str(h['NanoCpus'] / 1e9),
                   '--pids-limit', str(h['PidsLimit']), '--network', h['NetworkMode'],
                   '--restart', h['RestartPolicy']['Name'], '--stop-timeout', '20',
                   '--log-driver', h['LogConfig']['Type']]
        for key, value in h['LogConfig'].get('Config', {}).items():
            command += ['--log-opt', f'{key}={value}']
        for container_port, bindings in (h.get('PortBindings') or {}).items():
            for binding in bindings:
                if container_port != '8080/tcp':
                    raise ValueError('publish only the web port 8080')
                command += ['-p', published_port(binding['HostIp'], port or binding['HostPort'])]
        for key, value in self.info['Config']['Labels'].items():
            if key == 'org.openruyi.catalog-image' and not self.settings.get('catalog_image'):
                continue
            if key.startswith('org.openruyi.'):
                command += ['--label', f'{key}={value}']
        for role in ('config', 'data'):
            command += ['--mount', f'type=volume,src={self.settings[role]},dst=/{role},volume-nocopy'
                        + (',readonly' if role == 'config' else '')]
        if credential := self.settings.get('github_env_file'):
            github_token(credential)
            command += ['--env-file', str(credential)]
        for value in self.settings['environment']:
            command += ['-e', value]
        run(command + [image])

    def start(self):
        run(['docker', 'start', self.name])

    def rollback(self):
        if self.previous:
            existing = run(['docker', 'ps', '-aq', '--filter', 'name=^/' + self.name + '$'])
            if existing:
                run(['docker', 'rm', self.name])
            run(['docker', 'rename', self.previous, self.name])
        self.start()

    def save(self, directory):
        saved = {**self.info, 'Config': {**self.info['Config'],
                 'Env': [v for v in self.info['Config'].get('Env', [])
                         if not v.startswith('LOOKOUT_GITHUB_TOKEN=')]}}
        write_exclusive(directory / 'previous.json', json.dumps(saved, indent=2).encode())

    def export_backup(self, image, filename, destination):
        helper = 'openruyi-backup-' + uuid.uuid4().hex[:12]
        run(['docker', 'create', '--name', helper, '--network', 'none', *PROTECTION,
             '--mount', f'type=volume,src={self.backup_volume},dst=/backup,readonly,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c', 'pass'])
        try:
            with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
                staged = Path(temporary) / 'backup.sqlite3'
                run(['docker', 'cp', f'{helper}:/backup/{filename}', str(staged)])
                staged.chmod(0o600)
                publish_file(staged, destination)
        finally:
            run(['docker', 'rm', helper])

    def finish(self):
        if self.previous:
            run(['docker', 'rm', self.previous])


def quadlet(text):
    """Read only the installed template's deployment boundary, not a second INI DSL."""
    section, fields = None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('['):
            section = line
        elif section == '[Container]' and '=' in line and not line.startswith(('#', ';')):
            key, value = line.split('=', 1)
            fields.setdefault(key, []).append(value)
    def one(key):
        values = fields.get(key, [])
        if len(values) != 1:
            raise ValueError('Quadlet requires one ' + key)
        return values[0]
    fixed = {'UserNS': 'keep-id:uid=10001,gid=10001', 'User': '10001', 'Group': '10001',
             'ReadOnly': 'true', 'NoNewPrivileges': 'true', 'DropCapability': 'all'}
    if any(one(key) != value for key, value in fixed.items()):
        raise ValueError('Quadlet does not match the rootless runtime contract')
    volumes = {}
    for value in fields.get('Volume', []):
        parts = value.split(':')
        if len(parts) != 3 or parts[1] not in ('/config', '/data') or parts[1] in volumes:
            raise ValueError('upgrade supports one config mount and one data mount')
        path = Path(parts[0])
        options = set(parts[2].split(','))
        expected = {'ro', 'Z'} if parts[1] == '/config' else {'Z'}
        if not path.is_absolute() or not path.is_dir() or options != expected:
            raise ValueError('invalid persistent mount')
        volumes[parts[1]] = path.resolve()
    if set(volumes) != {'/config', '/data'}:
        raise ValueError('config/data mount is missing')
    name = one('ContainerName')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('invalid container name')
    # Preflight must use the same default database and ports as the service.
    environment = fields.get('Environment', [])
    for value in environment:
        key, sep, _ = value.partition('=')
        if not sep or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key):
            raise ValueError('use one unquoted KEY=value per Environment line')
        if key in {'TRACKER_DB', 'TRACKER_CONFIG', 'API_PORT'}:
            raise ValueError('custom data/config/internal API requires operator review')
        if key in {'HOST', 'PORT'} and value not in {'HOST=0.0.0.0', 'PORT=8080'}:
            raise ValueError('use the default internal listener; configure PublishPort instead')
    credential_files = fields.get('EnvironmentFile', [])
    if len(credential_files) > 1:
        raise ValueError('use one GitHub credential file')
    credential = credential_files[0] if credential_files else None
    if credential:
        if not Path(credential).is_absolute() or any(c.isspace() or c in '\"\'%:' for c in credential):
            raise ValueError('credential path must be absolute without whitespace or unit specifiers')
        github_token(credential)
    if any(fields.get(key) for key in ('PodmanArgs', 'Exec', 'Entrypoint',
                                      'AddCapability', 'Device', 'SecurityLabelDisable')):
        raise ValueError('custom runtime overrides require an operator-reviewed upgrade')
    if any(v == 'host' for v in fields.get('Network', [])):
        raise ValueError('use a private container network')
    for value in fields.get('PublishPort', []):
        match = re.fullmatch(r'(\[[0-9a-fA-F:]+\]|[0-9.]+):(\d+):8080', value)
        if not match:
            raise ValueError('publish a numeric IP and port for web port 8080')
        published_port(match[1].strip('[]'), match[2])
    catalog_images = [value.split('=', 1)[1] for value in fields.get('Label', [])
                      if value.startswith('org.openruyi.catalog-image=')]
    if len(catalog_images) > 1:
        raise ValueError('catalog initialization identity must be unique')
    catalog_image = catalog_images[0] if catalog_images else None
    return dict(image=one('Image'), name=name, config=volumes['/config'], data=volumes['/data'],
                environment=fields.get('Environment', []), catalog_image=catalog_image, github_env_file=credential,
                ports=fields.get('PublishPort', []))


def replace_image(text, image):
    section, lines, changed = None, [], 0
    for line in text.splitlines(keepends=True):
        if line.strip().startswith('['):
            section = line.strip()
        if section == '[Container]' and line.startswith('Image='):
            line, changed = f'Image={image}\n', changed + 1
        lines.append(line)
    if changed != 1:
        raise ValueError('expected one Image= in Container section')
    return ''.join(lines)


def write_unit(path, expected, replacement):
    if path.is_symlink() or path.read_text() != expected:
        raise ValueError('unit changed since review; refusing overwrite')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(replacement)
        publish_file(temporary, path, replace=True)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def manager(method, *arguments):
    """Require the user manager to acknowledge each requested operation."""
    run(['busctl', '--user', '--timeout=120', 'call', 'org.freedesktop.systemd1',
         '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager', method, *arguments])


def service_state(unit):
    return run(['systemctl', '--user', 'show', '--property=ActiveState', '--value', unit], timeout=5)


def service_action(unit, method):
    manager(method, 'ss', unit, 'replace')
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = service_state(unit)
        if method == 'StopUnit' and state in ('inactive', 'failed'):
            return
        if method == 'StartUnit':
            if state == 'active':
                return
            if state == 'failed':
                raise RuntimeError('service failed to start: ' + unit)
        time.sleep(0.25)
    raise RuntimeError('service transition timed out: ' + unit)


class Quadlet:
    engine = 'podman'

    def __init__(self, unit):
        if os.geteuid() == 0:
            raise ValueError('run as the rootless service owner, not root')
        if unit.suffix != '.container' or unit.is_symlink():
            raise ValueError('use the installed Quadlet .container file')
        os.environ.update(user_environment())
        self.unit, self.original = unit, unit.read_text()
        self.settings = quadlet(self.original)
        self.name = self.settings['name']
        self.service = unit.stem + '.service'
        self.replacement = None

    def check(self):
        if run(['podman', 'info', '--format', '{{.Host.Security.Rootless}}']) != 'true':
            raise ValueError('rootless Podman is required')
        if service_state(self.service) != 'active':
            raise ValueError('Quadlet service must be active before upgrade')
        if self.unit.read_text() != self.original:
            raise ValueError('unit changed since review; refusing upgrade')

    def stop(self):
        if service_state(self.service) not in ('inactive', 'failed'):
            service_action(self.service, 'StopUnit')

    def stage(self, image):
        replacement = replace_image(self.original, image)
        new_config = self.settings['config']
        section, lines = None, []
        for line in replacement.splitlines(keepends=True):
            if line.strip().startswith('['):
                section = line.strip()
            if section == '[Container]' and line.strip().startswith('Volume='):
                value = line.strip().removeprefix('Volume=')
                if value.split(':')[1] == '/config':
                    line = line.replace(value, f'{new_config}:/config:ro,Z', 1)
            lines.append(line)
        replacement = ''.join(lines)
        if quadlet(replacement)['config'] != new_config:
            raise ValueError('prepared config mount was not selected')
        write_unit(self.unit, self.original, replacement)
        self.replacement = replacement
        manager('Reload')

    def start(self):
        service_action(self.service, 'StartUnit')

    def rollback(self):
        expected = self.replacement or self.original
        write_unit(self.unit, expected, replace_image(self.original, self.settings['image']))
        manager('Reload')
        self.start()

    def save(self, directory):
        write_exclusive(directory / 'previous.container', self.original.encode())

    def export_backup(self, image, filename, destination):
        if not destination.is_file():
            raise RuntimeError('backup was not published')

    def finish(self):
        pass
