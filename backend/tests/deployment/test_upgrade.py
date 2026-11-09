"""Release orchestration contracts; real image startup is covered by smoke-image."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ('deploy/' + name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


upgrade = module('release-upgrade')
import deployment as operations
NEW, OLD = 'sha256:' + '2' * 64, 'sha256:' + '1' * 64


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(operations.os, 'geteuid', lambda: 10001)
    config, data, backups, bundle = [tmp_path / name for name in ('config', 'data', 'backups', 'release')]
    for path in (config, data, backups, bundle):
        path.mkdir()
    (data / 'state').mkdir()
    (data / 'state/tracker.sqlite3').write_bytes(b'operator data')
    text = (ROOT / 'deploy/quadlet/openruyi-lookout.container.in').read_text()
    for key, value in (('IMAGE', OLD), ('CONFIG_DIR', str(config)), ('DATA_DIR', str(data))):
        text = text.replace('@' + key + '@', value)
    unit = tmp_path / 'fixture.container'
    unit.write_text(text)
    manifest = dict(version='0.1.0', revision='a' * 40, image=NEW, storage=2,
                    platform='linux/arm64')
    return NEW, unit, backups, manifest


def simulate(monkeypatch, manifest, *, fail=None, incompatible=False):
    calls, ready = [], []
    active = ['active']
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'busctl':
            if 'StartUnit' in argv: active[0] = 'active'
            if 'StopUnit' in argv: active[0] = 'inactive'
        if argv[:3] == ['systemctl', '--user', 'show']:
            return active[0]
        if argv[:2] == ['podman', 'info']:
            return 'true'
        if argv[:2] == ['podman', 'inspect']:
            return json.dumps([{'Image': OLD, 'State': {'Status': 'running'}}])
        if argv[:3] == ['podman', 'image', 'inspect']:
            return json.dumps([{'Id': argv[-1], 'Os': 'linux', 'Architecture': 'arm64', 'Config': {'User': '10001:10001',
                'Labels': {'org.opencontainers.image.revision': manifest['revision'],
                           'org.opencontainers.image.version': manifest['version']}}}])
        if argv[:3] == ['podman', 'manifest', 'inspect']:
            return json.dumps({'schemaVersion': 2, 'config': {'digest': manifest['image']}})
        if argv[:2] == ['podman', 'run'] or argv[:2] == ['podman', 'exec']:
            if 'paths=[c[k]' in argv[-1]:
                return json.dumps({'local': True, 'inputs': {'fixture': 'unchanged'}})
            if 'storage.FORMAT' in argv[-1]:
                return json.dumps([manifest['version'], manifest['storage']])
            if fail and fail in argv:
                raise RuntimeError('fixture failure')
            if incompatible and OLD in argv and 'tracker.runtime_checks' in argv:
                raise RuntimeError('old reader rejects migrated database')
            if '/app/deploy/backup-snapshot.py' in argv:
                mount = next(value for value in argv if value.endswith(':/backup:Z'))
                (Path(mount.removesuffix(':/backup:Z')) / 'before.sqlite3').write_bytes(b'fixture backup')
        return ''
    def healthy(engine, name, image):
        ready.append(image)
        if fail == 'ready' and image == NEW:
            raise RuntimeError('new service unavailable')
    monkeypatch.setattr(upgrade, 'run', run)
    monkeypatch.setattr(operations, 'run', run)
    monkeypatch.setattr(__import__('catalog_upgrade'), 'run', run)
    monkeypatch.setattr(upgrade, 'healthy', healthy)
    return calls, ready


def test_plan_performs_no_engine_or_service_actions(deployment, monkeypatch):
    bundle, unit, backups, _ = deployment
    monkeypatch.setattr(upgrade, 'run', lambda *args, **kwargs: pytest.fail('plan must not execute'))
    before = unit.read_bytes()
    result = upgrade.upgrade(bundle, unit, backups)
    assert result['status'] == 'plan' and result['reference'] == NEW
    assert unit.read_bytes() == before and not list(backups.iterdir())


def test_upgrade_keeps_data_and_orders_stop_backup_migrate_start(deployment, monkeypatch):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    original = unit.read_text()
    result = upgrade.upgrade(bundle, unit, backups, apply=True)
    assert result['status'] == 'ready' and ready == [NEW]
    assert unit.read_text() == original.replace('Image=' + OLD, 'Image=' + NEW)
    assert (Path(result['data']) / 'state/tracker.sqlite3').read_bytes() == b'operator data'
    stop = next(i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StopUnit' in call)
    backup = next(i for i, call in enumerate(calls) if '/app/deploy/backup-snapshot.py' in call)
    migrate = next(i for i, call in enumerate(calls) if '/app/deploy/migrate-state.py' in call)
    start = next(i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StartUnit' in call)
    reload = next(i for i, call in enumerate(calls) if call[0] == 'busctl' and call[-1] == 'Reload')
    assert stop < backup < migrate < reload < start
    assert all(':Z' not in arg for call in calls[:stop] for arg in call)
    assert next(backups.glob('upgrade-*/previous.container')).read_text() == original


def test_reload_rpc_failure_is_not_reported_as_success(monkeypatch):
    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, '', 'manager unavailable')
    monkeypatch.setattr(operations.subprocess, 'run', failed)
    with pytest.raises(RuntimeError, match='busctl.*exit 1'):
        operations.manager('Reload')


@pytest.mark.parametrize('failure', ['/app/deploy/backup-snapshot.py', '/app/deploy/migrate-state.py', 'ready'])
def test_failed_upgrade_rolls_back_only_after_compatible_reader(deployment, monkeypatch, failure):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail=failure)
    original = unit.read_text()
    with pytest.raises(RuntimeError):
        upgrade.upgrade(bundle, unit, backups, apply=True)
    assert unit.read_text() == original and ready[-1] == OLD
    old_check = next(i for i, call in enumerate(calls) if OLD in call and 'tracker.runtime_checks' in call)
    start = max(i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StartUnit' in call)
    assert old_check < start


def test_incompatible_rollback_leaves_data_intact_and_service_stopped(deployment, monkeypatch):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail='ready', incompatible=True)
    with pytest.raises(RuntimeError, match='rollback/preflight failed'):
        upgrade.upgrade(bundle, unit, backups, apply=True)
    stops = [i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StopUnit' in call]
    assert not any(call[0] == 'busctl' and 'StartUnit' in call for call in calls[stops[-1]:])
    assert ready == [NEW]
    assert (operations.quadlet(unit.read_text())['data'] / 'state/tracker.sqlite3').read_bytes() == b'operator data'


def test_invalid_image_reference_is_rejected_before_service_stop(deployment, monkeypatch):
    _, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    with pytest.raises(ValueError, match='fully qualified'):
        upgrade.upgrade('--injected', unit, backups, apply=True)
    assert not any('StopUnit' in call for call in calls)
    assert not list(backups.iterdir())


@pytest.mark.parametrize('change', ['Environment=TRACKER_DB=/other/db', 'PodmanArgs=--privileged',
                                    'Volume=/tmp:/config:ro,Z', 'EnvironmentFile=/secrets'])
def test_custom_runtime_boundaries_fail_closed(deployment, change):
    _, unit, _, _ = deployment
    text = unit.read_text().replace('[Container]', '[Container]\n' + change)
    with pytest.raises(ValueError):
        operations.quadlet(text)


def test_unit_compare_and_swap_preserves_concurrent_operator_edit(tmp_path):
    unit = tmp_path / 'unit'
    unit.write_text('operator update')
    with pytest.raises(ValueError, match='changed since review'):
        operations.write_unit(unit, 'old unit', 'new unit')
    assert unit.read_text() == 'operator update'


def test_health_poll_has_bounded_subprocess_timeouts(monkeypatch):
    times = iter([0, 0, 100])
    monkeypatch.setattr(operations.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(operations.time, 'sleep', lambda *_: None)
    def run(argv, timeout):
        assert timeout <= 10
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(upgrade, 'run', run)
    monkeypatch.setattr(operations, 'run', run)
    with pytest.raises(RuntimeError, match='startup budget'):
        operations.healthy('podman', 'fixture', NEW)


@pytest.mark.parametrize('port', [1, 18730, 28730, 65535])
def test_quadlet_accepts_operator_loopback_port_and_keeps_it_on_upgrade(deployment, port):
    _, unit, _, _ = deployment
    text = unit.read_text().replace('127.0.0.1:18730', f'127.0.0.1:{port}')
    assert operations.quadlet(text)['image'] == OLD
    assert operations.replace_image(text, NEW) == text.replace(OLD, NEW)


@pytest.mark.parametrize('port', ['0', '65536', 'foo', '-1'])
def test_quadlet_rejects_invalid_ports(deployment, port):
    _, unit, _, _ = deployment
    with pytest.raises(ValueError, match='port'):
        operations.quadlet(unit.read_text().replace(':18730:', ':' + port + ':'))


def test_quadlet_accepts_explicit_listener_but_rejects_host_network(deployment):
    _, unit, _, _ = deployment
    assert operations.quadlet(unit.read_text().replace('127.0.0.1', '0.0.0.0'))['ports'] == ['0.0.0.0:18730:8080']
    with pytest.raises(ValueError):
        operations.quadlet(unit.read_text().replace('[Container]', '[Container]\nNetwork=host'))


def test_installer_rejects_conflicting_environment_before_engine_call(tmp_path, monkeypatch):
    install = module('install')
    monkeypatch.setattr(install, 'run', lambda *a, **kw: pytest.fail('no engine writes'))
    for value in ['PORT=9000', 'TRACKER_DB=/tmp/db', 'HOST=0.0.0.0', 'missing-separator']:
        with pytest.raises(ValueError, match='environment'):
            install.install(NEW, 'fixture', environment=[value])


@pytest.mark.parametrize('engine', ['docker', 'podman'])
def test_default_config_initialization_uses_operator_environment(tmp_path, monkeypatch, engine):
    install = module('install')
    monkeypatch.setattr(install.os, 'geteuid', lambda: 10001)
    monkeypatch.setattr(install.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(install.Path, 'is_file', lambda _: True)
    monkeypatch.setattr(install, 'resolve_image', lambda *_: {'image': NEW})
    calls = []

    class Prepared(Exception):
        pass

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ['podman', 'info']:
            return 'true'
        if argv[:2] == [engine, 'run']:
            raise Prepared
        return ''

    monkeypatch.setattr(install, 'run', run)
    with pytest.raises(Prepared):
        install.install(NEW, 'fixture', engine=engine,
                        directory=tmp_path / 'instance' if engine == 'podman' else None,
                        network='none', environment=['TRACKER_SPEC_REPO='])
    helper = calls[-1]
    assert helper[:2] == [engine, 'run']
    assert helper[helper.index('-e') + 1] == 'TRACKER_SPEC_REPO='


def test_config_archive_preserves_bytes_and_rejects_symlinks(tmp_path):
    import io
    import tarfile
    install = module('install')
    (tmp_path / 'tracker.toml').write_bytes(b'private configuration')
    (tmp_path / 'tracker.toml').chmod(0o600)
    with tarfile.open(fileobj=io.BytesIO(install.config_archive(tmp_path))) as archive:
        assert archive.extractfile('tracker.toml').read() == b'private configuration'
    (tmp_path / 'link').symlink_to('tracker.toml')
    with pytest.raises(ValueError, match='regular'):
        install.config_archive(tmp_path)


def test_atomic_metadata_never_overwrites_existing_file(tmp_path):
    path = tmp_path / 'release.json'
    operations.write_exclusive(path, b'original')
    with pytest.raises(FileExistsError):
        operations.write_exclusive(path, b'new')
    assert path.read_bytes() == b'original'
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_config_read_reports_permissions_instead_of_claiming_file_type(tmp_path):
    from tracker.config import read_input
    directory = tmp_path / 'private'
    directory.mkdir()
    path = directory / 'tracker.toml'
    path.write_text('x=1')
    directory.chmod(0)
    try:
        with pytest.raises(PermissionError):
            read_input(path)
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize('method,states', [
    ('StartUnit', ['inactive', 'activating', 'active']),
    ('StopUnit', ['active', 'deactivating', 'inactive']),
])
def test_acknowledged_job_must_reach_requested_state(monkeypatch, method, states):
    observed = iter(states)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return next(observed) if argv[0] == 'systemctl' else ''
    monkeypatch.setattr(operations, 'run', run)
    monkeypatch.setattr(operations.time, 'sleep', lambda *_: None)
    operations.service_action('fixture.service', method)
    assert len(calls) == 4 and method in calls[0]


def test_service_transition_is_bounded_and_failed_start_rejected(monkeypatch):
    monkeypatch.setattr(operations, 'run', lambda *a, **k: 'failed')
    with pytest.raises(RuntimeError, match='failed to start'):
        operations.service_action('fixture.service', 'StartUnit')
    clock = iter([0, 61])
    monkeypatch.setattr(operations.time, 'monotonic', lambda: next(clock))
    with pytest.raises(RuntimeError, match='timed out'):
        operations.service_action('fixture.service', 'StartUnit')


def test_same_image_is_verified_without_stop_or_backup(deployment, monkeypatch):
    _, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    result = upgrade.upgrade(OLD, unit, backups, apply=True)
    assert result['status'] == 'unchanged' and ready == [OLD]
    assert not any('StopUnit' in call or 'StartUnit' in call for call in calls)
    assert not list(backups.iterdir())


def test_lost_stop_acknowledgement_resumes_old_instance(deployment, monkeypatch):
    reference, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    run = operations.run
    failed = False
    def lost(argv, **kwargs):
        nonlocal failed
        result = run(argv, **kwargs)
        if 'StopUnit' in argv and not failed:
            failed = True
            raise RuntimeError('acknowledgement lost')
        return result
    monkeypatch.setattr(operations, 'run', lost)
    with pytest.raises(RuntimeError, match='acknowledgement lost'):
        upgrade.upgrade(reference, unit, backups, apply=True)
    assert ready == [OLD]
    assert any('StartUnit' in call for call in calls)
    assert json.loads(next(backups.glob('upgrade-*/result.json')).read_text()) == {'status': 'failed', 'rollback': 'ready'}


def test_old_running_image_is_not_resolved_through_pulled_channel(deployment, monkeypatch):
    _, unit, backups, manifest = deployment
    unit.write_text(unit.read_text().replace(OLD, 'ghcr.io/example/lookout:main'))
    calls, ready = simulate(monkeypatch, manifest, fail='ready')
    real = operations.run
    def channel(argv, **kwargs):
        if argv[:3] == ['podman', 'image', 'inspect'] and argv[-1] == 'ghcr.io/example/lookout:main':
            argv = [*argv[:-1], NEW]
        return real(argv, **kwargs)
    monkeypatch.setattr(operations, 'run', channel)
    with pytest.raises(RuntimeError):
        upgrade.upgrade('ghcr.io/example/lookout:main', unit, backups, apply=True)
    assert ready == [NEW, OLD]
    assert 'Image=' + OLD in unit.read_text()


def test_upgrade_lock_is_owned_by_instance_not_backup_directory(deployment):
    _, unit, _, _ = deployment
    first = operations.Quadlet(unit)
    second = operations.Quadlet(unit)
    assert operations.instance_lock(first) == operations.instance_lock(second)
    with operations.upgrade_lock(operations.instance_lock(first)):
        with pytest.raises(BlockingIOError):
            with operations.upgrade_lock(operations.instance_lock(second)):
                pytest.fail('concurrent upgrade admitted')


@pytest.mark.parametrize('engine', ['docker', 'podman'])
@pytest.mark.parametrize('status', ['exited', 'running', 'paused', 'restarting', None])
def test_data_user_check_rechecks_listed_container_state(tmp_path, monkeypatch, engine, status):
    from types import SimpleNamespace
    data = tmp_path / 'data'
    service = SimpleNamespace(engine=engine, settings={'data': data if engine == 'podman' else 'fixture-data'})
    info = {'State': {'Status': status} if status else {},
            'Mounts': [{'Name': 'fixture-data', 'Source': str(data)}]}

    def run(argv):
        if argv == [engine, 'ps', '-q']:
            return 'listed-container'
        assert argv == [engine, 'inspect', 'listed-container']
        return json.dumps([info])

    monkeypatch.setattr(operations, 'run', run)
    if status == 'exited':
        operations.no_data_users(service)
    else:
        with pytest.raises(RuntimeError, match='another running container'):
            operations.no_data_users(service)


def test_docker_lock_is_scoped_by_daemon_and_instance(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setattr(operations, 'run', lambda *_: '"daemon-id"')
    service = SimpleNamespace(engine='docker', instance='instance-a')
    first = operations.instance_lock(service)
    assert first == operations.instance_lock(service)
    service.instance = 'instance-b'
    assert first != operations.instance_lock(service)


@pytest.mark.parametrize('reference', ['image', '--flag', 'ghcr.io/example/lookout', 'ghcr.io/example/lookout:main\n'])
def test_image_reference_requires_explicit_registry_identity(reference):
    with pytest.raises(ValueError):
        operations.resolve_image('docker', reference)


def test_registry_pull_is_pinned_before_metadata_probe(monkeypatch):
    calls = []
    reference = 'ghcr.io/example/lookout:main'
    def run(argv, **kwargs):
        calls.append(argv)
        if 'inspect' in argv:
            return json.dumps([{'Id': NEW, 'Os': 'linux', 'Architecture': 'amd64',
                'RepoDigests': ['ghcr.io/example/lookout@sha256:' + '3' * 64],
                'Config': {'User': '10001:10001', 'Labels': {
                    'org.opencontainers.image.version': '0.1.0',
                    'org.opencontainers.image.revision': 'a' * 40}}}])
        if argv[1] == 'run':
            assert NEW in argv and reference not in argv
            return '["0.1.0", 2]'
        return ''
    monkeypatch.setattr(operations, 'run', run)
    result = operations.resolve_image('podman', reference)
    assert calls[0] == ['podman', 'pull', '--retry=0', reference]
    assert result['image'] == NEW and result['reference'] == reference
    assert result['storage'] == 2 and result['platform'] == 'linux/amd64'


def test_automation_keeps_host_update_and_backup_separate(tmp_path, monkeypatch):
    install = module('install')
    monkeypatch.setattr(install.Path, 'home', lambda: tmp_path)
    calls = []
    monkeypatch.setattr(install, 'run', lambda argv, **kwargs: calls.append(argv))
    monkeypatch.setattr(install, 'manager', lambda *args: calls.append(args))
    monkeypatch.setattr(install, 'service_action', lambda *args: calls.append(args))
    unit = tmp_path / 'lookout.container'
    monkeypatch.setattr(install, 'refresh_tools', lambda engine, image, link: link)
    install.install_automation('lookout', tmp_path, unit, 'ghcr.io/example/lookout:main', NEW)
    user_units = tmp_path / '.config/systemd/user'
    update = (user_units / 'lookout-update.service').read_text()
    backup = (user_units / 'lookout-backup.service').read_text()
    assert '--image ghcr.io/example/lookout:main' in update
    assert '--unit ' + str(unit) in update and '--quiet' in update
    assert '--workflow checks.yml' in update
    assert '--tools-link ' + str(tmp_path / 'automation/current') in update
    assert 'OnUnitInactiveSec=5min' in (user_units / 'lookout-update.timer').read_text()
    assert '--backup-dir ' + str(tmp_path / 'backups') in backup
    assert '@' not in update and '@' not in backup
    assert ('EnableUnitFiles', 'asbb', '2', 'lookout-update.timer', 'lookout-backup.timer', 'false', 'false') in calls
    assert ('lookout-update.timer', 'StartUnit') in calls
    assert ('lookout-backup.timer', 'StartUnit') in calls
    with pytest.raises(ValueError, match='already exist'):
        install.install_automation('lookout', tmp_path, unit, 'ghcr.io/example/lookout:main', NEW)


def test_rootless_installer_requires_linger_before_creating_directories(tmp_path, monkeypatch):
    install = module('install')
    monkeypatch.setattr(install.os, 'geteuid', lambda: 1001)
    monkeypatch.setattr(install, 'run', lambda argv, **kwargs: 'true' if argv[0] == 'podman' else 'no')
    destination = tmp_path / 'instance'
    with pytest.raises(ValueError, match='linger'):
        install.install(NEW, 'lookout', engine='podman', directory=destination,
                        network='pasta', auto_update='ghcr.io/example/lookout:main')
    assert not destination.exists()


@pytest.mark.parametrize('reference', [NEW, NEW.removeprefix('sha256:')])
def test_podman_bare_image_ids_are_normalized(monkeypatch, reference):
    def run(argv, **kwargs):
        assert argv[1] != 'pull'
        if 'inspect' in argv:
            return json.dumps([{'Id': NEW.removeprefix('sha256:'), 'Os': 'linux', 'Architecture': 'amd64',
                'Config': {'User': '10001:10001', 'Labels': {
                    'org.opencontainers.image.version': '0.1.0',
                    'org.opencontainers.image.revision': 'a' * 40}}}])
        return '["0.1.0", 2]'
    monkeypatch.setattr(operations, 'run', run)
    assert operations.resolve_image('podman', reference)['image'] == NEW


@pytest.mark.parametrize('replace', [False, True])
def test_publication_syncs_file_before_publish_and_directory_after(tmp_path, monkeypatch, replace):
    staged, destination = tmp_path / 'staged', tmp_path / 'result'
    staged.write_bytes(b'complete')
    events = []
    real_sync = operations.os.fsync
    real_publish = operations.os.replace if replace else operations.os.link

    def sync(fd):
        events.append('sync')
        return real_sync(fd)

    def publish(*args):
        events.append('publish')
        return real_publish(*args)

    monkeypatch.setattr(operations.os, 'fsync', sync)
    monkeypatch.setattr(operations.os, 'replace' if replace else 'link', publish)
    operations.publish_file(staged, destination, replace=replace)
    assert events == ['sync', 'publish', 'sync']
    assert destination.read_bytes() == b'complete'
    assert staged.exists() is not replace


def test_exclusive_publication_rejects_dangling_symlink(tmp_path):
    destination = tmp_path / 'result'
    destination.symlink_to('missing')
    with pytest.raises(FileExistsError):
        operations.write_exclusive(destination, b'complete')
    assert destination.is_symlink()
    assert not (tmp_path / 'missing').exists()
    assert list(tmp_path.iterdir()) == [destination]


def test_reviewed_unit_replacement_preserves_permissions(tmp_path):
    unit = tmp_path / 'unit'
    unit.write_text('reviewed')
    unit.chmod(0o640)
    operations.write_unit(unit, 'reviewed', 'replacement')
    assert unit.read_text() == 'replacement'
    assert unit.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [unit]


@pytest.mark.parametrize('failure_at,published', [(1, False), (2, True)])
def test_publication_sync_failures_are_reported_and_staging_is_cleaned(tmp_path, monkeypatch,
                                                                       failure_at, published):
    destination = tmp_path / 'result'
    count = 0
    real_sync = operations.os.fsync

    def fail_sync(fd):
        nonlocal count
        count += 1
        if count == failure_at:
            raise OSError('injected sync failure')
        return real_sync(fd)

    monkeypatch.setattr(operations.os, 'fsync', fail_sync)
    with pytest.raises(OSError, match='injected sync failure'):
        operations.write_exclusive(destination, b'complete')
    assert destination.exists() is published
    assert list(tmp_path.iterdir()) == ([destination] if published else [])


@pytest.mark.parametrize('engine', ['docker', 'podman'])
@pytest.mark.parametrize('tag', ['main', 'sha-' + 'a' * 40])
def test_registry_comparison_uses_metadata_without_pulling(monkeypatch, engine, tag):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ['image', 'inspect']:
            return json.dumps([{'Os': 'linux', 'Architecture': 'amd64'}])
        assert argv[1:3] == ['manifest', 'inspect']
        return json.dumps({'schemaVersion': 2, 'config': {'digest': OLD}})
    monkeypatch.setattr(operations, 'run', run)
    assert operations.unchanged_image(engine, 'ghcr.io/owner/lookout:' + tag, OLD)
    assert len(calls) == 2 and not any('pull' in call for call in calls)


def test_registry_index_selects_running_platform(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ['image', 'inspect']:
            return json.dumps([{'Os': 'linux', 'Architecture': 'arm64', 'Variant': 'v8'}])
        if '@' in argv[-1]:
            assert argv[-1] == 'ghcr.io/owner/lookout@' + NEW
            return json.dumps({'schemaVersion': 2, 'config': {'digest': OLD}})
        return json.dumps({'manifests': [
            {'digest': 'sha256:' + '3' * 64, 'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'digest': NEW, 'platform': {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}},
        ]})
    monkeypatch.setattr(operations, 'run', run)
    assert operations.unchanged_image('podman', 'ghcr.io/owner/lookout:main', OLD)
    assert len(calls) == 3


@pytest.mark.parametrize('digest,expected', [(OLD, True), (NEW, False)])
def test_registry_digest_controls_update_not_tag_name(monkeypatch, digest, expected):
    monkeypatch.setattr(operations, 'run', lambda argv, **kwargs: json.dumps(
        [{'Os': 'linux', 'Architecture': 'amd64'}] if argv[1] == 'image'
        else {'schemaVersion': 2, 'config': {'digest': digest}}))
    assert operations.unchanged_image('docker', 'ghcr.io/owner/lookout:main', OLD) is expected


def test_manifest_failure_does_not_fall_back_to_pull(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'image':
            return json.dumps([{'Os': 'linux', 'Architecture': 'amd64'}])
        raise RuntimeError('registry unavailable')
    monkeypatch.setattr(operations, 'run', run)
    with pytest.raises(RuntimeError, match='registry unavailable'):
        operations.unchanged_image('docker', 'ghcr.io/owner/lookout:main', OLD)
    assert not any('pull' in call for call in calls)


@pytest.mark.parametrize('loader', ['release-upgrade', 'upgrade'])
def test_unchanged_registry_channel_never_pulls_or_stops(deployment, monkeypatch, loader):
    _, unit, backups, manifest = deployment
    worker = upgrade if loader == 'release-upgrade' else module('upgrade')
    calls, ready = simulate(monkeypatch, {**manifest, 'image': OLD})
    monkeypatch.setattr(worker, 'run', operations.run)
    monkeypatch.setattr(worker, 'healthy', upgrade.healthy)
    result = worker.upgrade('ghcr.io/owner/lookout:main', unit, backups, apply=True)
    assert result['status'] == 'unchanged' and ready == [OLD]
    assert not any('pull' in call or 'StopUnit' in call or 'StartUnit' in call for call in calls)
    assert not list(backups.iterdir())


@pytest.mark.parametrize('message', ['unexpected EOF', 'connection reset by peer', 'TLS handshake timeout'])
def test_registry_transport_retry_is_bounded_and_does_not_expose_stderr(monkeypatch, message):
    calls, delays = [], []
    def execute(argv, **kwargs):
        calls.append(kwargs['timeout'])
        return subprocess.CompletedProcess(argv, 125 if len(calls) < 3 else 0, 'image-id', message)
    monkeypatch.setattr(operations.subprocess, 'run', execute)
    monkeypatch.setattr(operations.time, 'sleep', delays.append)
    assert operations.run(['podman', 'pull', 'ghcr.io/fixture/image:main'], retry_transport=True) == 'image-id'
    assert len(calls) == 3 and delays == [1, 2]
    assert all(0 < budget <= 120 for budget in calls)


@pytest.mark.parametrize('message,retry,expected', [('unexpected EOF', True, 3),
    ('unauthorized: fixture-secret', True, 1), ('unexpected EOF', False, 1)])
def test_registry_failures_preserve_failure_and_limit_attempts(monkeypatch, message, retry, expected):
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 125, '', message)
    monkeypatch.setattr(operations.subprocess, 'run', execute)
    monkeypatch.setattr(operations.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='exit 125') as failure:
        operations.run(['podman', 'pull', 'ghcr.io/fixture/image:main'], retry_transport=retry)
    assert 'fixture-secret' not in str(failure.value)
    assert len(calls) == expected


def test_podman_single_image_projection_uses_raw_manifest(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ['podman', 'image', 'inspect']:
            return json.dumps([{'Os': 'linux', 'Architecture': 'amd64'}])
        if argv[:3] == ['podman', 'manifest', 'inspect']:
            return json.dumps({'schemaVersion': 2, 'manifests': None})
        assert argv == ['skopeo', 'inspect', '--raw', 'docker://ghcr.io/owner/lookout:main']
        return json.dumps({'schemaVersion': 2, 'config': {'digest': OLD}})
    monkeypatch.setattr(operations, 'run', run)
    assert operations.unchanged_image('podman', 'ghcr.io/owner/lookout:main', OLD)
    assert not any('pull' in call for call in calls)


@pytest.mark.parametrize('engine', ['docker', 'podman'])
def test_failed_pull_waits_for_next_scheduler_attempt(monkeypatch, engine):
    calls = []
    def failed(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', 'EOF')
    monkeypatch.setattr(operations.subprocess, 'run', failed)
    monkeypatch.setattr(operations.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='pull failed'):
        operations.resolve_image(engine, 'ghcr.io/example/lookout:main')
    assert len(calls) == 1
    assert calls[0][1] == 'pull'
    assert ('--retry=0' in calls[0]) == (engine == 'podman')


@pytest.mark.parametrize('address', ['10.230.50.181', '127.0.0.1', '0.0.0.0', '::1'])
def test_listener_survives_image_replacement(deployment, address):
    _, unit, _, _ = deployment
    mapping = operations.published_port(address, 18730)
    text = unit.read_text().replace('127.0.0.1:18730:8080', mapping)
    assert operations.quadlet(operations.replace_image(text, NEW))['ports'] == [mapping]


def test_quadlet_credentials_reach_preflight_without_token_in_argv(deployment, tmp_path, monkeypatch):
    _, unit, _, _ = deployment
    path = tmp_path / 'github.env'
    path.write_text('LOOKOUT_GITHUB_TOKEN=fixture_secret\n')
    path.chmod(0o600)
    unit.write_text(unit.read_text().replace('[Container]', '[Container]\nEnvironmentFile=' + str(path)))
    # The fixture models a service owner distinct from the test process UID.
    original = operations.os.fstat
    def owned(fd):
        values = list(original(fd)); values[4] = 10001
        return operations.os.stat_result(values)
    monkeypatch.setattr(operations.os, 'fstat', owned)
    service = operations.Quadlet(unit)
    command = operations.image_command(service, NEW, '-m', 'tracker.runtime_checks')
    assert command[command.index('--env-file') + 1] == str(path)
    assert 'fixture_secret' not in ' '.join(command)


def test_rootless_subprocess_does_not_inherit_root_bus(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(operations.os, 'geteuid', lambda: 1001)
    monkeypatch.setattr(operations.Path, 'is_dir', lambda path: str(path) == '/run/user/1001')
    monkeypatch.setattr(operations.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir='/home/service'))
    environment = operations.user_environment({'HOME': '/root', 'XDG_RUNTIME_DIR': '/run/user/0',
        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/0/bus', 'PATH': '/usr/bin', 'HTTPS_PROXY': 'proxy'})
    assert environment['HOME'] == '/home/service'
    assert environment['XDG_RUNTIME_DIR'] == '/run/user/1001'
    assert environment['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/run/user/1001/bus'
    assert environment['HTTPS_PROXY'] == 'proxy'
