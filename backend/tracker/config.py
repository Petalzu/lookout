"""Trusted deployment settings and release catalogs; never supplied by HTTP."""
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

from tracker import catalog
from tracker.catalog import read_input
from tracker.identity import request_url
from tracker.monitors.commits.model import package_policies as commit_packages
from tracker.monitors.github.model import settings as github_settings


def load(path):
    path = Path(path).absolute()
    raw = read_input(path)
    path = path.resolve()
    config = tomllib.loads(raw.decode())
    if 'packages' in config:
        raise ValueError('inline packages are not supported; use packages_config with root package tables')
    # Hash the bytes parsed, not a later read: collectors use these stamps to
    # reject publication after an in-flight configuration edit.
    input_hashes = {str(path): hashlib.sha256(raw).hexdigest()}
    _collector_settings(config)
    catalog.load(config, path, input_hashes)
    config['input_hashes'] = input_hashes
    config['config_digest'] = input_hashes[str(path)]
    _distribution_settings(config)
    _target_bindings(config)
    _spec_settings(config)
    _origin_settings(config)
    github_settings(config)
    commit_packages(config)
    return config


def _collector_settings(config):
    for key, default in (('obs_interval_seconds', 60), ('build_interval_seconds', 15), ('nvchecker_interval_seconds', 21600),
                         ('nvchecker_timeout_seconds', 7200), ('build_history_interval_seconds', 300)):
        value = config['collector'].setdefault(key, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if config['collector']['build_interval_seconds'] < 10:
        raise ValueError('build_interval_seconds must be at least 10')
    for interval, stale, default in (('obs_interval_seconds', 'obs_stale_after_seconds', 300),
                                      ('build_interval_seconds', 'obs_stale_after_seconds', 300),
                                      ('nvchecker_interval_seconds', 'stale_after_seconds', 86400)):
        if config['collector'].get(stale, default) <= config['collector'][interval]:
            raise ValueError(f'{stale} must exceed {interval}')


def _distribution_settings(config):
    # Distribution presentation data has one owner; the frontend knows no
    # BuildSystem categories. CSS values are deliberately limited to hex colors.
    config.setdefault('openruyi', {}).setdefault('buildsystems', {})
    dependencies = config['openruyi'].setdefault('dependencies', {})
    if (not isinstance(dependencies, dict) or any(
            not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,99}', key)
            or not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_+.-]{1,200}', value)
            for key, value in dependencies.items())):
        raise ValueError('openruyi.dependencies maps dependency identities to source package names')
    environments = config['openruyi'].setdefault('dependency_environments', {})
    allowed_environment = {'sys_platform', 'os_name', 'platform_system', 'platform_machine',
                           'implementation_name', 'platform_python_implementation'}
    if (not isinstance(environments, dict) or set(environments) - {'pep508'}
            or not isinstance(environments.get('pep508', {}), dict)
            or set(environments.get('pep508', {})) - allowed_environment
            or any(not isinstance(value, str) or not 1 <= len(value) <= 100
                   for value in environments.get('pep508', {}).values())):
        raise ValueError('openruyi.dependency_environments.pep508 requires explicit target facts')
    for name, appearance in config['openruyi']['buildsystems'].items():
        if (not isinstance(name, str) or not name or len(name) > 100
                or not isinstance(appearance, dict)
                or not {'background', 'foreground'} <= appearance.keys()
                or set(appearance) - {'background', 'foreground', 'icon'}
                or any(not isinstance(v, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', v)
                       for k, v in appearance.items() if k != 'icon')
                or ('icon' in appearance and (not isinstance(appearance['icon'], str)
                    or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', appearance['icon'])))):
            raise ValueError('openruyi.buildsystems requires hex colors and an optional local icon identifier')


def _target_bindings(config):
    ids = [t['id'] for t in config['targets']]
    identities = [(t['repository'], t['architecture']) for t in config['targets']]
    if len(ids) != 3 or len(set(ids)) != 3 or len(set(identities)) != 3:
        raise ValueError('exactly three distinct target identities are required')
    for binding in config['packages'].values():
        for track in [binding.get('compare'), *binding.get('watch', [])]:
            if track and track not in config['native']:
                raise ValueError(f'unknown configured track: {track}')


def _spec_settings(config):
    spec = config.get('spec', {})
    config['spec'] = {
        # The image sets TRACKER_SPEC_REPO: it enables SPEC collection even
        # without a [spec] table. An empty environment value does not override repo.
        'repo': os.environ.get('TRACKER_SPEC_REPO') or spec.get('repo'),
        'url': spec.get('url'),
        'branch': spec.get('branch'),
        'source_url_template': spec.get('source_url_template'),
        'macro_package': spec.get('macro_package', config['collector'].get('spec_macro_package')),
        'extra_macro_packages': spec.get('extra_macro_packages', []),
        'local_sources': spec.get('local_sources', {}),
        'changelog_limit': spec.get('changelog_limit', 20),
        'fetch_timeout_seconds': spec.get('fetch_timeout_seconds', 300),
    }
    def safe_name(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_+.-]{1,200}', value)) and value not in ('.', '..')

    extra = config['spec']['extra_macro_packages']
    if (not isinstance(extra, list) or len(extra) > 16 or not all(safe_name(p) for p in extra)
            or len(set(extra)) != len(extra) or config['spec']['macro_package'] in extra):
        raise ValueError('spec.extra_macro_packages requires distinct package names')
    sources = config['spec']['local_sources']
    if (not isinstance(sources, dict) or any(
            not safe_name(package) or not isinstance(names, list) or len(names) > 16
            or not all(safe_name(name) for name in names) or len(set(names)) != len(names)
            for package, names in sources.items())):
        raise ValueError('spec.local_sources maps packages to distinct local source filenames')
    if type(config['spec']['changelog_limit']) is not int or not 1 <= config['spec']['changelog_limit'] <= 200:
        raise ValueError('spec.changelog_limit must be an integer in 1..200')
    timeout = config['spec']['fetch_timeout_seconds']
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('spec.fetch_timeout_seconds must be a positive integer')
    interval = spec.get('interval_seconds', 60)
    if type(interval) is not int or interval <= 0:
        raise ValueError('spec.interval_seconds must be a positive integer')
    config['spec']['interval_seconds'] = interval
    for key in ('url', 'branch'):
        value = config['spec'][key]
        if value is None and not config['spec']['repo']:
            continue
        if not isinstance(value, str) or not value or value.startswith('-') or any(c in value for c in '\r\n\x00'):
            raise ValueError(f'spec.{key} must be configured as a non-empty safe string')
    repo = config['spec']['repo']
    if repo is not None and (not isinstance(repo, str) or not repo):
        raise ValueError('spec.repo must be a non-empty path or omitted')



def _origin_settings(config):
    for value in (config['obs']['api_url'], config['obs']['web_url']):
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('OBS base URL must not contain credentials or a query')
    template = config['spec']['source_url_template']
    if template is not None and not spec_source_url({'url': config['spec']['url'], 'source_url_template': template}, 'PACKAGE', 'REF'):
        raise ValueError('spec.source_url_template must be a public HTTP(S) URL containing {ref} and {path}')


def require_unchanged(config, path=None):
    """Confirm the exact inputs already validated by load(), without parsing again."""
    try:
        inputs = config['input_hashes']
        if path is not None:
            tracker = Path(path)
            if tracker.is_symlink() or inputs.get(str(tracker.resolve())) != config['config_digest']:
                raise ValueError('changed tracker input')
        if any(hashlib.sha256(read_input(name)).hexdigest() != expected for name, expected in inputs.items()):
            raise ValueError('changed input')
    except (OSError, ValueError) as error:
        raise ValueError('configuration changed or unavailable during collection; result not published') from error


def track_fingerprint(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def public_source(entry):
    """Keep secrets, executable commands, arbitrary config out of the public API."""
    if entry.get('source') in ('crates_index', 'anitya_stable'):
        # Publish the same protocol provenance for shorthand and explicit rules.
        # Consumers see one identity, not both an authoring key and a derived URL.
        try:
            url = request_url(entry)
        except ValueError:
            return {'source': entry['source']}
        entry = {**entry, 'url': url}
        entry.pop('cratesio', None)
    result = {}
    for key in ('source', 'pypi', 'cratesio', 'cpan', 'anitya', 'github', 'gitlab', 'gitea', 'git', 'url'):
        value = entry.get(key)
        if not isinstance(value, str):
            continue
        if key in ('url', 'git'):
            try:
                u = urlsplit(value)
                if u.scheme not in ('https', 'http') or not u.hostname:
                    continue
                value = urlunsplit((u.scheme, u.hostname + (f':{u.port}' if u.port else ''), u.path, '', ''))
            except ValueError:
                continue
        result[key] = value
    if entry.get('source') in ('git', 'github', 'gitlab', 'gitea'):
        for key in ('branch', 'path', 'host'):
            value = entry.get(key)
            if isinstance(value, str) and 0 < len(value) <= 256 and not re.search(r'[\r\n]', value):
                result[key] = value
        for key in ('use_commit', 'use_latest_tag', 'use_latest_release', 'use_max_tag', 'use_max_release'):
            if isinstance(entry.get(key), bool):
                result[key] = entry[key]
    # The endpoint identifies an Anitya project independently of its parser.
    # Expose that identity, never arbitrary query strings or credentials.
    try:
        u = urlsplit(entry.get('url', ''))
        ids = parse_qs(u.query).get('project_id', [])
        if (u.scheme == 'https' and u.hostname == 'release-monitoring.org'
                and u.port in (None, 443)
                and u.path.rstrip('/') == '/api/v2/versions'
                and len(ids) == 1 and re.fullmatch(r'[1-9][0-9]{0,11}', ids[0])):
            result['project_id'] = int(ids[0])
            result['project_url'] = 'https://release-monitoring.org/project/' + ids[0] + '/'
    except (ValueError, TypeError):
        # Keep the rule without optional Anitya provenance when its URL is invalid.
        pass
    return result


# A release-line label is normally implicit in the package name: a trailing "-N[.N...]"
# means that maintenance line. Only genuine exceptions need an explicit binding.
_TRACK_LABEL = re.compile(r'-(\d+(?:\.\d+)*)$')

def derive_track_label(name):
    m = _TRACK_LABEL.search(name)
    return f'{m.group(1)}.x' if m else 'stable'

def binding(config, name):
    return resolve_binding(name, config['native'], config.get('packages', {}).get(name, {}))


def resolve_binding(name, native_ids, overrides):
    """The same defaults apply to operator configuration and its saved projection."""
    return {**overrides, 'compare': overrides.get('compare', name if name in native_ids else None),
            'watch': overrides.get('watch', []),
            'track_label': overrides.get('track_label') or derive_track_label(name)}


def spec_source_url(origin, name, ref):
    """Writer-owned provenance selects the URL; the frontend never guesses a forge.

    Known forge conventions are defaults. Other hosts require one explicit
    operator template, not a new code adapter. Never interpolate unescaped input.
    """
    try:
        u = urlsplit(origin.get('url', ''))
        if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password or u.query or u.fragment:
            return None
        template = origin.get('source_url_template')
        base = urlunsplit((u.scheme, u.netloc, u.path.rstrip('/').removesuffix('.git'), '', ''))
        if template is None:
            if u.hostname == 'github.com':
                template = base + '/tree/{ref}/{path}'
            elif u.hostname.startswith('gitlab.') or u.hostname == 'gitlab.com':
                template = base + '/-/tree/{ref}/{path}'
            else:
                return None
        if not isinstance(template, str) or '{ref}' not in template or '{path}' not in template:
            return None
        url = template.format(ref=quote(ref, safe=''), path='SPECS/' + quote(name, safe=''))
        result = urlsplit(url)
        if (result.scheme not in ('https', 'http') or not result.hostname or result.username
                or result.password or result.query or result.fragment or any(c in url for c in '\r\n{}')):
            return None
        return url
    except (ValueError, KeyError, TypeError, IndexError):
        return None
