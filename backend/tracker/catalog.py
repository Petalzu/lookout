"""Compose release-owned catalogs with explicitly named operator overrides."""
import hashlib
import json
import stat
import tomllib
from pathlib import Path

from tracker.monitors.version import rules

POLICY_FIELDS = {'compare', 'watch', 'comparable', 'not_applicable', 'track_label', 'monitors'}
DISTRIBUTION_FIELDS = {'buildsystems', 'dependencies', 'dependency_environments', 'github', 'commits'}


def read_input(path):
    path = Path(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise ValueError('configuration input does not exist: ' + str(path)) from error
    if not stat.S_ISREG(mode):
        raise ValueError('configuration input must be a regular file: ' + str(path))
    return path.read_bytes()


def policies(document):
    for name, policy in document.items():
        if not name or not isinstance(policy, dict) or set(policy) - POLICY_FIELDS:
            raise ValueError(f'{name}: expected a root package policy table')
        monitors = policy.get('monitors', {})
        if (not isinstance(monitors, dict)
                or any(value is not False and not isinstance(value, dict) for value in monitors.values())):
            raise ValueError(f'{name}: monitor identity must be a table, or false to disable it')
    return document


def merge_policies(base, overrides):
    result = dict(base)
    for name, values in overrides.items():
        previous = result.get(name, {})
        result[name] = {**previous, **values}
        if 'monitors' in values:
            # One adapter identity is indivisible: never combine an old vendor
            # with a new product. Different monitors retain independent owners.
            result[name]['monitors'] = {**previous.get('monitors', {}), **values['monitors']}
    return result


def merge_distribution(base, overrides):
    for document in (base, overrides):
        if not isinstance(document, dict) or set(document) - DISTRIBUTION_FIELDS:
            raise ValueError('unknown distribution catalog field')
        for key, value in document.items():
            if not isinstance(value, dict):
                raise ValueError('openruyi.' + key + ' must be a table')
    return {key: {**base.get(key, {}), **overrides.get(key, {})}
            for key in base.keys() | overrides.keys()}


def load(config, path, hashes):
    def table(reference):
        if not isinstance(reference, str) or not reference:
            raise ValueError('catalog reference must name a configuration file')
        file = path.parent / reference
        raw = read_input(file)
        if any(file.samefile(loaded) for loaded in hashes):
            raise ValueError('catalogs and overrides must use a distinct file from other inputs')
        file = file.resolve()
        hashes[str(file)] = hashlib.sha256(raw).hexdigest()
        return str(file), tomllib.loads(raw.decode())

    def native(reference):
        file, document = table(reference)
        loaded = rules.parse(document, hashes[file])
        return file, loaded

    collector = config['collector']
    nvpath, base = native(collector.get('nvchecker_config', 'nvchecker.toml'))
    override_path, entries, options = None, {}, {}
    if 'version_overrides' in collector:
        override_path, override = native(collector['version_overrides'])
        entries, options = override.entries, override.options
    exclusions = collector.get('exclude_tracks', [])
    if (not isinstance(exclusions, list) or any(not isinstance(name, str) or not name for name in exclusions)
            or len(set(exclusions)) != len(exclusions)):
        raise ValueError('exclude_tracks must contain distinct track names')
    effective = {**base.entries, **entries}
    for name in exclusions:
        effective.pop(name, None)
    effective_options = {**base.options, **options}

    packages_path, packages = None, {}
    if 'packages_config' in config:
        packages_path, packages = table(config['packages_config'])
        policies(packages)
    package_override_path, package_overrides = None, {}
    if 'package_overrides' in config:
        package_override_path, package_overrides = table(config['package_overrides'])
        policies(package_overrides)
    distribution = {}
    distribution_path = None
    if 'distribution_config' in config:
        distribution_path, distribution = table(config['distribution_config'])
    config['openruyi'] = merge_distribution(distribution, config.get('openruyi', {}))
    config.update(nvpath=nvpath, packages_path=packages_path, distribution_path=distribution_path,
                  version_overrides_path=override_path, package_overrides_path=package_override_path,
                  native=effective, native_options=effective_options,
                  native_options_path=override_path if 'keyfile' in options else nvpath,
                  packages=merge_policies(packages, package_overrides))
    # Text/input guards include every file, whereas observation keys contain
    # only the effective rule or query. An unrelated catalog edit is not a flush.
    merged = bool(override_path or exclusions)
    config['nv_digest'] = (hashlib.sha256(json.dumps({'entries': effective, 'options': effective_options},
                            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                           if merged else hashes[nvpath])


def local_inputs(config, root):
    return {name: digest for name, digest in config['input_hashes'].items()
            if Path(name).is_relative_to(root)}


def shared_inputs(config, root):
    return {name: digest for name, digest in config['input_hashes'].items()
            if not Path(name).is_relative_to(root)}
