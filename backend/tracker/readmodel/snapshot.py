"""Read-only package composition from current monitor observations."""
import re
from datetime import UTC, datetime
from urllib.parse import quote

from tracker import state
from tracker.monitors import model as monitor_model
from tracker.monitors.requires import model as requirements
from tracker.monitors.version import compare as version_status
from tracker.readmodel import monitors as monitor_views
from tracker.readmodel.monitors import component_ttl, observed_at


def next_transition(snapshot, now):
    """Earliest freshness boundary for this snapshot, not an arbitrary cache TTL.

    The observation set is conservative (unused track/history facts may shorten
    caching, never prolong it). Deduplicate timestamps before date arithmetic:
    thousands of source/build entries commonly share a single poll timestamp.
    """
    ttl = component_ttl(snapshot, 'nvchecker')
    obs_ttl = component_ttl(snapshot, 'builds')
    history_ttl = component_ttl(snapshot, 'build_history:')
    checks = set()
    def observe(fact, lifetime):
        stamp = fact.get('checked_at') or fact.get('fetched_at')
        if isinstance(stamp, str) and stamp:
            checks.add((stamp, lifetime))
    for fact in snapshot['sources'].values():
        observe(fact, obs_ttl)
    for name in snapshot.get('specs', {}):
        fact = state.current_source(snapshot, name)
        observe(fact, fact['stale_after_seconds'])
    for fact in snapshot['tracks'].values():
        observe(fact, ttl)
    for targets in snapshot['builds'].values():
        for fact in targets.values():
            observe(fact, obs_ttl)
            observe({'fetched_at': fact.get('history_checked_at')}, history_ttl)
    for key, fact in snapshot['components'].items():
        observe(fact, component_ttl(snapshot, key))
    for providers in snapshot.get('monitors', {}).values():
        for fact in providers.values():
            observe(fact, snapshot.get('monitor_stale_after_seconds', 86400))
            for scope in fact.get('scope_checks', {}).values():
                observe(scope, snapshot.get('monitor_stale_after_seconds', 86400))
    for batches in snapshot.get('commits_batches', {}).values():
        batch = batches.get('active', {})
        observe(batch, batch.get('stale_after_seconds', 86400))
    changes = (state.next_stale_change({'fetched_at': stamp}, now, lifetime) for stamp, lifetime in checks)
    return min((change for change in changes if change is not None), default=None)

def project_monitors(snapshot, now=None):
    now = now or datetime.now(UTC)
    flavors = {}
    for name, owner in snapshot['inventory'].items():
        flavors.setdefault(owner, []).append(name)
    modules = monitor_views.registry(snapshot)
    versions = version_status.evaluate_all(snapshot, now)
    resolver = requirements.Resolver(snapshot, now)
    rows = []
    for name in sorted(state.package_names(snapshot), key=lambda n: (n.casefold(), n)):
        version = versions[name]
        context = monitor_views.Context(snapshot, name, now, version, flavors.get(name, [name]),
                                        monitor_model.project(snapshot, name, now, version=version), resolver)
        results = {module.id: module.read(context) for module in modules}
        monitor_views.compose_version(results, version)
        monitor_views.retained_dimensions(results)
        rows.append(dict(name=name, detail_url=f'/packages/{quote(name, safe="")}', monitors=results))
    return rows, collection(snapshot, rows, now)


def collection(snapshot, rows, now):
    errors = [v['error'] for v in snapshot['components'].values() if v.get('error')]
    if snapshot.get('last_attempt') and any(state.stale(v, now, component_ttl(snapshot, k))
                                          for k, v in snapshot['components'].items()):
        errors.append('collection observations are stale')
    failed_sources = sum(monitor_model.check_failed(row['monitors']['source']['check']) for row in rows)
    if failed_sources:
        errors.append(f'Source checks failed for {failed_sources} of {len(rows)} packages')
    if 'targets' not in snapshot:
        errors.append('Initial package collection is pending')
    components = snapshot['components']
    return dict(obs_updated_at=observed_at([components.get(k, {}) for k in
                                                ('targets', 'inventory', 'source_index', 'builds')]),
                      upstream_updated_at=observed_at([components.get('nvchecker', {})]),
                      last_attempt=snapshot['last_attempt'], mode=snapshot['mode'], errors=errors,
                      build_service_url=snapshot.get('obs', {}).get('web_url'),
                      source_repository=source_repository(snapshot),
                      generation=snapshot['generation'], packages=len(rows),
                      tracked_packages=sum(bool(r['monitors']['version']['data']['track']) for r in rows))


def source_repository(snapshot):
    """Use the repository checkpoint, not a package's last changed commit.

    Incremental parsing retains older package heads. Origins must agree before
    attributing the global checkpoint to one repository and branch.
    """
    head = snapshot['components'].get('spec_git', {}).get('head')
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', head):
        return None
    origins = {(origin.get('url'), origin.get('branch')) for spec in snapshot.get('specs', {}).values()
               if (origin := spec.get('source_origin'))}
    if len(origins) != 1:
        return None
    url, branch = origins.pop()
    if not url or not branch:
        return None
    return dict(url=url, branch=branch, revision=head)


def refresh_build_clock(snapshot, rows, now):
    """Only used before the cached projection's next semantic time boundary.

    Storage proved the entire successful status vector unchanged. Reuse source,
    version and evidence projections only while both build clocks are fresh:
    a recovered or future-dated heartbeat changes more than display timestamps.
    """
    stamp = snapshot['components']['builds']['fetched_at']
    if (state.stale({'fetched_at': stamp}, now, component_ttl(snapshot, 'builds'))
            or any(row['monitors']['build']['check']['stale'] for row in rows)):
        return project_monitors(snapshot, now)
    updated = []
    for row in rows:
        build = row['monitors']['build']
        targets = [{**target, 'updated_at': stamp,
                    'flavors': [{**fact, 'fetched_at': stamp, 'attempted_at': stamp, 'updated_at': stamp}
                                for fact in target['flavors']]} for target in build['data']['targets']]
        build = {**build, 'check': {**build['check'], 'checked_at': stamp, 'attempted_at': stamp},
                 'data': {**build['data'], 'targets': targets}}
        updated.append({**row, 'monitors': {**row['monitors'], 'build': build}})
    return updated, collection(snapshot, updated, now)



def monitor_summary(row, focus=''):
    return {**row, 'monitors': {mid: monitor_views.summary(result, focused=mid == focus)
                              for mid, result in row['monitors'].items()}}


def upstream_failures(observations):
    """Group identical provider errors for triage, without claiming a common cause."""
    from urllib.parse import urlsplit
    groups = {}
    for name, observation in observations:
        error = observation.get('error')
        if not error:
            continue
        source = observation.get('source') or {}
        provider = source.get('source') or 'unknown'
        url = source.get('url') or source.get('git')
        if isinstance(url, str):
            try:
                provider = urlsplit(url).hostname or provider
            except ValueError:
                pass  # Retain the provider kind if saved URL evidence is malformed.
        key = (provider, error)
        group = groups.setdefault(key, {'provider': provider, 'error': error, 'packages': []})
        group['packages'].append(name)
    return [{**group, 'count': len(group['packages']), 'packages': sorted(group['packages'])}
            for _, group in sorted(groups.items())]


def component_status(components):
    """Expose collection health, not resumable provider payloads or cursors."""
    fields = ('attempted_at', 'fetched_at', 'checked_at', 'error', 'next_poll_at',
              'stale_after_seconds', 'retry_at', 'page', 'round_started', 'reconciled_at',
              'selected_track_count', 'selected_packages', 'head', 'mode')
    return {name: {key: value[key] for key in fields if key in value}
            for name, value in components.items()}
