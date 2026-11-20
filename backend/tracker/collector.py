"""A bounded, single-writer collection command, independent of HTTP request handling."""
import argparse
import concurrent.futures
import json
from copy import deepcopy
from datetime import datetime
from urllib.parse import quote

from tracker import config as cfg
from tracker import state
from tracker.monitors.build import obs
from tracker.monitors.source import git as spec_git
from tracker.monitors.source import rpm as native_spec
from tracker.monitors.version import nvchecker as nv

HISTORY_VERSION_BATCH_SIZE = 12  # at most 24 extra OBS requests per collection (before HTTP retries)

def collect(config, old, client, now, source_limit=None):
    if old.get('generation') and old.get('obs') and old['obs'] != config['obs']:
        raise ValueError('OBS scope changed; use a separate snapshot database')
    new = deepcopy(old)
    prior_targets = {t['id']: (t['repository'], t['architecture']) for t in old.get('targets', [])}
    next_targets = {t['id']: (t['repository'], t['architecture']) for t in config['targets']}
    for tid in prior_targets.keys() | next_targets.keys():
        if prior_targets.get(tid) != next_targets.get(tid):
            # A label/id is not OBS provenance. Never carry another repository's
            # successes into a repointed target, even during the history cache TTL.
            for builds in new['builds'].values():
                builds.pop(tid, None)
            new['components'].pop('build_history:' + tid, None)
    new.update(last_attempt=now, mode='live', generation=old['generation'] + 1,
               targets=config['targets'], bindings={name: cfg.binding(config, name) for name in config.get('packages', {})})
    new['native_ids'] = list(config['native'])
    new['presentation'] = config.get('openruyi', {})
    new['obs'] = config['obs']
    new['stale_after_seconds'] = config['collector'].get('stale_after_seconds', 86400)
    new['obs_stale_after_seconds'] = config['collector'].get('obs_stale_after_seconds', new['stale_after_seconds'])
    new['build_history_interval_seconds'] = config['collector'].get('build_history_interval_seconds', 300)
    components = new['components']
    project = quote(config['obs']['project'], safe='')
    def stage(key, action):
        try:
            value = action()
            components[key] = state.success(components.get(key, {}), {}, now)
            return value
        except Exception as e:
            # Persist a category, never URLs with auth, raw stderr, headers, or local paths.
            components[key] = state.failure(components.get(key, {}), f'{key}: {type(e).__name__}', now)
            return None
    def validate_targets():
        obs.validate_targets(client.get(f'/source/{project}/_meta'), config['obs']['project'], config['targets'])
        return True
    verified = stage('targets', validate_targets)
    inv = stage('inventory', lambda: obs.inventory(client.get(f'/source/{project}')))
    if inv is not None:
        new['inventory'] = inv
        actual = {owner for owner in inv.values()}
        # Only a successful, validated COMPLETE authoritative inventory can remove a source.
        new['sources'] = {name: new['sources'].get(name, {}) for name in sorted(actual)}
        new['builds'] = {name: values for name, values in new['builds'].items() if name in inv}
    index = stage('source_index', lambda: obs.source_index(client.get(f'/source/{project}?view=info&parse=1')))
    if index is not None:
        new['index'] = index
    if inv is not None and index is not None:
        macro_error = None
        try:
            macro_files = native_spec.macro_paths(config['collector'], project, index)
        except ValueError as e:
            macro_files, macro_error = [], str(e)
        # The two successful responses still must agree before version facts can be refreshed.
        candidates = []
        for name, old_source in new['sources'].items():
            source = dict(old_source)
            needs_native = (config['collector'].get('native_spec_fallback', False)
                            and (not state.usable_version(source.get('version')) or source.get('native_query'))
                            and (source.get('native_query', {}).get('context', {}).get('resolver') != native_spec.RESOLVER
                                 or source.get('native_macro_paths', []) != macro_files or macro_error))
            current_hash = index.get(name, {}).get('srcmd5')
            if not current_hash:
                new['sources'][name] = state.failure(source, 'source index entry unavailable', now)
            elif (source.get('version') or source.get('raw_version')) and source.get('srcmd5') == current_hash and not needs_native:
                new['sources'][name] = {**source, 'checked_at': now, 'attempted_at': now, 'error': None}
            else:
                new['sources'][name] = state.failure(source, 'source version awaiting observation', now)
                candidates.append(name)
        # Priority is explicit native mappings, then never/oldest attempted. No package-specific code.
        candidates.sort(key=lambda n: (cfg.binding(config, n)['compare'] is None,
                                       old['sources'].get(n, {}).get('attempted_at') or '', n))
        limit = source_limit if source_limit is not None else config['collector'].get('source_batch_size', 150)
        selected = candidates if limit == 0 else candidates[:limit]
        def fetch_source(name):
            previous = new['sources'][name]
            try:
                raw = client.get(f'/source/{project}/{quote(name, safe="")}?view=info&parse=1')
                fact = obs.source_info(raw, name, index[name]['srcmd5'])
                if not fact['version'] and config['collector'].get('native_spec_fallback', False):
                    try:
                        if macro_error:
                            raise ValueError(macro_error)
                        fact = native_spec.resolve(client, project, name, fact, macro_files)
                    except Exception as e:
                        fact['version_error'] = f'native rpmspec unavailable: {type(e).__name__}'
                if not fact.get('version') and state.usable_version(previous.get('version')):
                    return name, state.failure({**previous, 'unresolved_observation': fact}, fact.get('version_error') or 'new source version unresolved', now)
                return name, state.success(previous, {**fact, 'checked_at': now}, now)
            except Exception as e:
                return name, state.failure(previous, f'source version unavailable: {type(e).__name__}', now)
        workers = config['collector'].get('source_workers', 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="obs-source") as pool:
            for name, fact in pool.map(fetch_source, selected, buffersize=workers):
                new['sources'][name] = fact
    else:
        for name, previous in new['sources'].items():
            new['sources'][name] = state.failure(previous, 'source inventory/index could not be confirmed', now)
    # Exactly one bounded bulk history request per target, not one per package.
    # Last-success facts remain useful when refresh fails; failure never erases them.
    if verified:
        for target in config['targets']:
            tid = target['id']
            key = 'build_history:' + tid
            previous = components.get(key, {})
            interval = config['collector'].get('build_history_interval_seconds', 300)
            if not state.stale({'fetched_at': previous.get('attempted_at')}, datetime.fromisoformat(now), interval):
                continue
            repository, arch = quote(target['repository'], safe=''), quote(target['architecture'], safe='')
            history = stage(key, lambda: obs.last_successes(client.get(
                f'/build/{project}/{repository}/{arch}/_jobhistory?code=lastfailures')))
            for name in new['inventory']:
                fact = new['builds'].setdefault(name, {}).setdefault(tid, {})
                fact['history_attempted_at'] = now
                if history is not None:
                    fact['history_checked_at'] = now
                    fact['history_error'] = None
                    fact['history_unresolved'] = name in history and history[name] is None
                    observed = history.get(name)
                    cached = fact.get('last_success') or {}
                    if observed and obs.success_identity(observed) == obs.success_identity(cached) and cached.get('version'):
                        # A fresh bulk record still contains MACRO. Do not erase
                        # the already verified RPM version of this exact build.
                        observed = {**cached, **observed, 'version': cached['version']}
                    fact['last_success'] = observed or fact.get('last_success')
                else:
                    fact['history_error'] = components[key]['error']
    if verified:
        refresh_success_versions(config, new, client, now)
    return new

def refresh_builds(config, old, client, now=None):
    """Return only status-owned patches from one project-wide request."""
    builds = {name: {} for name in old['inventory']}
    try:
        data = client.get('/build/' + quote(config['obs']['project'], safe='') + '/_result')
        results = obs.build_results(data, config['obs']['project'], config['targets'])
        error = None
    except Exception as exc:
        results, error = None, 'OBS build result unavailable: ' + type(exc).__name__
    # Timestamp the response, not the start of a potentially slow request.
    now = now or state.utcnow()
    complete = results is not None
    for target in config['targets']:
        tid = target['id']
        values = results.get(tid) if results is not None else None
        for name in old['inventory']:
            previous = {key: value for key, value in old['builds'].get(name, {}).get(tid, {}).items()
                        if key in state.BUILD_FIELDS['builds']}
            if values is not None and name in values:
                builds[name][tid] = state.success(previous, values[name], now)
            else:
                complete = False
                builds[name][tid] = state.failure(
                    previous, error or 'OBS target/package result unavailable', now)
        if values is None:
            complete = False
    prior = old['components'].get('builds', {})
    component = (state.success(prior, {}, now) if complete else
        state.failure(prior, error or 'OBS target/package result unavailable', now))
    return {'builds': builds, 'components': {'builds': component}}


def build_patch(snapshot, phase):
    return {name: {tid: {key: value for key, value in fact.items()
                         if key in state.BUILD_FIELDS[phase]}
                   for tid, fact in targets.items()}
            for name, targets in snapshot['builds'].items()}


def refresh_success_versions(config, snapshot, client, now):
    """Fill unresolved history only, with a fair bounded queue and identity cache."""
    interval = config['collector'].get('build_history_interval_seconds', 300)
    candidates = []
    for target in config['targets']:
        for name, builds in snapshot['builds'].items():
            fact = builds.get(target['id'], {})
            success = fact.get('last_success') or {}
            identity = obs.success_identity(success)
            if success.get('version') or not all(identity) or fact.get('history_error') or fact.get('history_unresolved'):
                continue
            same_attempt = fact.get('version_attempt_identity') == list(identity)
            attempted = fact.get('version_attempted_at') if same_attempt else None
            if not state.stale({'fetched_at': attempted}, datetime.fromisoformat(now), interval):
                continue
            candidates.append((attempted or '', name, target['id'], target, fact))
    # Never-attempted entries precede retryable failures: one bad package cannot
    # starve the remaining missing versions. No network is on the API read path.
    candidates.sort(key=lambda row: row[:3])
    for _, name, _, target, fact in candidates[:HISTORY_VERSION_BATCH_SIZE]:
        success = fact['last_success']
        fact['version_attempt_identity'] = list(obs.success_identity(success))
        fact['version_attempted_at'] = now
        try:
            fields = obs.resolve_success_version(client, config['obs']['project'], target, name, success)
            fact['last_success'] = {**success, **fields, 'version_error': None}
        except Exception as error:
            # Keep timestamp/hash and any prior success facts, never substitute
            # source.version or an unrelated architecture/build's binary.
            fact['last_success'] = {**success, 'version_error': f'RPM version unavailable: {type(error).__name__}'}

def merge_upstreams(config, latest, tracks, error, now, selected=None):
    """Merge into the newest snapshot: a slow upstream run must not rewind OBS."""
    if latest.get('obs') and latest['obs'] != config['obs']:
        raise ValueError('OBS scope changed while checking upstreams')
    fields = dict(tracks=tracks if selected is None else {**latest['tracks'], **tracks},
                  native_ids=list(config['native']), nv_digest=config.get('nv_digest'),
                  bindings={name: cfg.binding(config, name) for name in config.get('packages', {})})
    components = {}
    if selected is None:
        prior = {**latest['components'].get('nvchecker', {}),
                 'options_fingerprint': cfg.track_fingerprint(config.get('native_options', {}))}
        components['nvchecker'] = (state.failure(prior, error or 'some upstream tracks failed', now)
            if error or any(t.get('error') for t in tracks.values()) else state.success(prior, {}, now))
    return state.merge(latest, 'upstreams', fields, components)


def refresh_specs(config, old_specs, names, logs, now, describe=native_spec.describe, *, observed_at=None, tree_head=None):
    """Rebuild per-package SPEC entries from one already-computed changelog map.

    `logs` is spec_git.changelogs() output: every package's changelog from a single
    history traversal. Each package's head is its newest bucketed commit; when it is
    unchanged and metadata already parsed cleanly, the librpm parse is skipped (the
    only real per-package cost). Changed or previously failed packages are re-read and
    re-parsed. Resolver and macro provenance changes also invalidate cached metadata;
    keeping a commit fixed is not enough to prove the parser environment is unchanged.
    If history is unavailable, tree_head identifies the verified input tree while
    logs retain earlier history. Per-package parse failures remain explicit.
    """
    spec = config['spec']
    repo, git = spec['repo'], spec.get('git', 'git')
    if not repo:
        return old_specs
    macros = [item for package in spec_git.macro_packages(spec)
              for item in spec_git.read_macros(repo, package, git=git)]
    native_spec.validate_macros(macros)
    origin = {k: spec.get(k) for k in ('url', 'branch', 'source_url_template')}
    observed_at = observed_at or now
    result = {}
    for name in names:
        previous = old_specs.get(name, {})
        entries = logs.get(name, [])
        head = tree_head or (entries[0]['commit'] if entries else None)
        previous_context = previous.get('native_query', {}).get('context', {})
        source_names = spec.get('local_sources', {}).get(name, [])
        same_parser = (previous_context.get('resolver') == native_spec.RESOLVER
                       and previous_context.get('additional_macros') == [provenance for provenance, _ in macros]
                       and [p['name'] for p in previous_context.get('local_sources', [])] == source_names)
        if (head is not None and previous.get('head') == head and same_parser
                and previous.get('metadata') is not None and not previous.get('error')):
            # SPECS/<name> unchanged since last observation: refresh changelog only, skip parsing.
            result[name] = state.success(previous, {'head': head, 'changelog': entries,
                                                     'metadata': previous['metadata'], 'source_origin': origin}, observed_at)
            result[name]['attempted_at'] = now
            continue
        data = spec_git.read_spec(repo, name, git=git)
        try:
            inputs = {'local_sources': spec_git.read_local_sources(repo, name, source_names, git)} if source_names else {}
        except ValueError:
            described = {'metadata': None, 'metadata_error': 'Configured local SPEC inputs unavailable or invalid'}
        else:
            try:
                described = describe(data, macros=macros, **inputs) if data is not None else {
                    'metadata': None, 'metadata_error': 'SPEC not found in clone'}
            except native_spec.MacroInputError:
                raise
            except native_spec.SpecInputError as error:
                described = {'metadata': None, 'metadata_error': str(error)}
        if error := described.get('metadata_error'):
            # A failed attempt does not establish a new source revision or erase
            # the last successful parse. Its old provenance must stay attached.
            entry = state.failure(previous, error, now)
            if previous.get('metadata') is None:
                entry.update(head=head, changelog=entries, metadata=None,
                             native_query=described.get('native_query', {}), source_origin=origin)
        else:
            entry = state.success(previous, {'head': head, 'changelog': entries,
                                            'metadata': described['metadata'],
                                            'native_query': described.get('native_query', {}),
                                            'source_origin': origin}, observed_at)
            entry['attempted_at'] = now
        result[name] = entry
    return result


def merge_specs(config, latest, specs, error, now, *, context=None):
    """Merge SPEC results into the newest snapshot without rewinding OBS/upstream."""
    prior = latest['components'].get('spec_git', {})
    component = (state.failure({**prior, **(context or {})}, error, now) if error
                 else state.success(prior, context or {}, now))
    return state.merge(latest, 'specs', {'specs': specs,
                       'spec_interval_seconds': config['spec'].get('interval_seconds', 60)},
                       {'spec_git': component})


def check_specs(config, db, describe=native_spec.describe):
    """Fetch, reconcile changed packages and failed parses, then publish a checkpoint.

    A missing ancestor or changed parser policy reconciles all packages. An unchanged
    commit needs no log traversal unless a failed parse must be retried.
    """
    spec = config['spec']
    if not spec['repo']:
        return state.read(db)
    repo, git = spec['repo'], spec.get('git', 'git')
    with state.writer_lock(str(db) + '.specs'):
        ok, fetch_error = spec_git.fetch(
            repo, git=git, timeout=spec['fetch_timeout_seconds'],
            retries=config['collector'].get('spec_fetch_retries', 2),
        )
        old = state.read(db)
        specs = old.get('specs', {})
        error = fetch_error
        context = None
        now = state.utcnow()
        prior = old['components'].get('spec_git', {})
        head, head_error = spec_git.head(repo, git=git)
        origin = {k: spec.get(k) for k in ('url', 'branch', 'source_url_template')}
        offline = (not ok and not head_error and head and prior.get('head') == head
                   and prior.get('fetched_at') and specs
                   and all(fact.get('source_origin') == origin for fact in specs.values()))
        if ok or offline:
            error = head_error
            if not error and head:
                if prior.get('catalog_head') == head:
                    names = list(specs)
                else:
                    names, error = spec_git.packages(repo, head, git)
                names = names if names is not None else []
                fingerprint = cfg.track_fingerprint({
                    'resolver': native_spec.RESOLVER,
                    **{key: spec.get(key) for key in ('url', 'branch', 'source_url_template',
                                                     'macro_package', 'extra_macro_packages', 'local_sources', 'changelog_limit')},
                })
                incremental = (not prior.get('history_error')
                               and prior.get('input_fingerprint') == fingerprint
                               and spec_git.is_ancestor(repo, prior.get('head'), head, git))
                logs = {}
                history_error = None
                if not error and incremental and prior['head'] != head:
                    logs, history_error = spec_git.changelogs(repo, spec['changelog_limit'], git=git,
                                                             since=prior['head'])
                if incremental and any(package in logs for package in spec_git.macro_packages(spec)):
                    incremental = False
                if not error and not incremental:
                    logs, history_error = spec_git.changelogs(repo, spec['changelog_limit'], git=git)
                    selected = set(names)
                else:
                    added = set(names) - specs.keys()
                    retry = {name for name, fact in specs.items()
                             if fact.get('error') or fact.get('metadata') is None} & set(names)
                    if (added or retry) and not error and not history_error:
                        history_names = added | retry
                        history, history_error = spec_git.changelogs(
                            repo, spec['changelog_limit'], git=git,
                            names=None if history_names == set(names) else sorted(history_names))
                        logs.update(history)
                    selected = (set(logs) | added | retry) & set(names)
                    for name in selected - added:
                        combined = {entry['commit']: entry for entry in
                                    logs.get(name, []) + specs.get(name, {}).get('changelog', [])}
                        logs[name] = list(combined.values())[:spec['changelog_limit']]
                if history_error:
                    # History is presentation data, not permission to read the tree.
                    # Bind reparsed metadata to HEAD, never to a retained old log entry.
                    selected = set(names)
                    logs = {name: specs.get(name, {}).get('changelog', []) for name in names}
                if not error:
                    try:
                        refreshed = refresh_specs(config, specs, sorted(selected), logs, now, describe,
                                                  observed_at=prior['fetched_at'] if offline else now,
                                                  tree_head=head if history_error else None)
                    except (spec_git.MacroReadError, native_spec.MacroInputError) as failure:
                        error = str(failure)
                    else:
                        observed_head, changed_error = spec_git.head(repo, git=git)
                        if changed_error or observed_head != head:
                            error = changed_error or 'SPEC HEAD changed during parsing'
                        else:
                            specs = {name: refreshed[name] if name in refreshed else
                                     specs[name] if offline else state.success(specs[name], {}, now)
                                     for name in names}
                            context = {'head': head, 'catalog_head': head, 'input_fingerprint': fingerprint,
                                       'mode': 'incremental' if incremental else 'full',
                                       'selected_packages': len(selected), 'history_error': history_error}
                            error = history_error
            elif not error:
                error = 'SPEC HEAD unavailable'
        elif not error:
            error = 'SPEC fetch failed'
        if not ok:
            error = '; '.join(dict.fromkeys(e for e in (fetch_error or 'SPEC fetch failed', error) if e))
        # Offline parsing can repair evidence, but cannot confirm remote freshness.
        with state.writer_lock(db, timeout=60):
            previous = state.read_cached(db)
            latest = previous[0]
            snapshot = merge_specs(config, latest, specs, error, now, context=context)
            state.commit(db, snapshot, previous=previous)
    return snapshot


def check_upstreams(config, config_path, db, run_nv=nv.run, tracks=None, attempt=None, due=False):
    selected = nv.selected_names(config, tracks)
    # Only one upstream job, but no snapshot-writer lock during remote requests.
    with state.writer_lock(str(db) + '.upstreams'):
        old = state.read(db)
        now = state.utcnow()
        if due:
            if selected is not None:
                raise ValueError('automatic selection cannot be combined with explicit tracks')
            selected = nv.due_names(config, old, datetime.fromisoformat(now))
        def guard():
            cfg.require_unchanged(config, config_path)
        def publish(completed):
            if not completed or set(completed) - set(config['native']):
                raise ValueError('unexpected partial upstream result scope')
            for name, fact in completed.items():
                if fact.get('configuration_fingerprint') != cfg.track_fingerprint(config['native'][name]):
                    raise ValueError('partial upstream result belongs to a different rule')
            with state.writer_lock(db, timeout=60):
                guard()
                previous = state.read_cached(db)
                latest = merge_upstreams(config, previous[0], completed, None, now, selected=list(completed))
                state.commit(db, latest, previous=previous)
        if due and not selected:
            tracks, error = {}, None
        elif selected is None:
            tracks, error = run_nv(config, old['tracks'], now, on_results=publish)
        elif due:
            tracks, error = run_nv(config, old['tracks'], now, tracks=selected, on_results=publish)
        else:
            tracks, error = run_nv(config, old['tracks'], now, tracks=selected)
        if selected is not None and set(tracks) != set(selected):
            raise ValueError('selected upstream results do not match requested tracks')
        with state.writer_lock(db, timeout=60):
            guard()
            previous = state.read_cached(db)
            latest = previous[0]
            if due:
                retained = {name: fact for name, fact in latest['tracks'].items() if name in config['native']}
                retained.update(tracks)
                bindings = {name: cfg.binding(config, name) for name in config.get('packages', {})}
                if (not selected and retained == latest['tracks'] and latest.get('bindings') == bindings
                        and latest.get('nv_digest') == config['nv_digest']
                        and latest.get('native_ids') == list(config['native'])):
                    if attempt is not None:
                        attempt.update(selected_track_count=0, track_errors={}, command_error=None)
                    return latest
                snapshot = merge_upstreams(config, latest, retained, error, now)
                # The oldest successful member dates total coverage. A subset must
                # not relabel untouched tracks as freshly checked.
                successes = [fact.get('fetched_at') for fact in retained.values()]
                prior = latest['components'].get('nvchecker', {})
                complete = len(retained) == len(config['native']) and all(successes)
                component = {**prior, 'attempted_at': now, 'selected_track_count': len(selected or ()),
                             'options_fingerprint': cfg.track_fingerprint(config.get('native_options', {})),
                             'error': error or ('some upstream tracks failed' if not complete or
                                               any(fact.get('error') for fact in retained.values()) else None)}
                if complete:
                    component['fetched_at'] = min(successes, key=datetime.fromisoformat, default=now)
                snapshot['components']['nvchecker'] = component
            else:
                snapshot = merge_upstreams(config, latest, tracks, error, now, selected)
            state.commit(db, snapshot, previous=previous)
        if attempt is not None:
            attempt.update(selected_track_count=len(tracks),
                           track_errors={name: fact['error'] for name, fact in tracks.items() if fact.get('error')},
                           command_error=error)
    return snapshot

def collect_obs(config, db, source_limit=None):
    """Serialize OBS jobs separately; network must not block upstream/SPEC commits."""
    with state.writer_lock(str(db) + '.obs'):
        client = obs.Client(config)
        try:
            observed = collect(config, state.read(db), client, state.utcnow(), source_limit)
            with state.writer_lock(db, timeout=60):
                previous = state.read_cached(db)
                latest = previous[0]
                if latest.get('obs') and latest['obs'] != config['obs']:
                    raise ValueError('OBS scope changed during collection')
                # OBS owns these fields only. A concurrent upstream/spec job may
                # have advanced the snapshot while this network phase was running.
                snapshot = state.merge(latest, 'obs',
                    {**{key: observed[key] for key in state.PHASE_FIELDS['obs'] if key != 'builds'},
                     'builds': build_patch(observed, 'obs')},
                    {key: value for key, value in observed['components'].items()
                     if key in ('targets', 'inventory', 'source_index') or key.startswith('build_history:')})
                state.commit(db, snapshot, previous=previous)
        finally:
            client.close()
    return snapshot

def collect_builds(config, db):
    """The fast OBS lane owns statuses only; history keeps its own cadence."""
    with state.writer_lock(str(db) + '.builds'):
        old = state.read(db)
        if not old['inventory']:
            return old  # The authoritative inventory collector initializes scope.
        if old.get('obs') != config['obs'] or old.get('targets') != config['targets']:
            raise ValueError('OBS build scope awaits metadata collection')
        # A heartbeat already retries with backoff: no immediate HTTP retry burst.
        client = obs.Client(config, attempts=1)
        try:
            observed = refresh_builds(config, old, client)
        finally:
            client.close()
        with state.writer_lock(db, timeout=60):
            previous = state.read_cached(db)
            latest = previous[0]
            if latest.get('obs') != old['obs'] or latest.get('targets') != old['targets']:
                raise ValueError('OBS scope changed while checking builds')
            patches = {name: facts for name, facts in observed['builds'].items()
                       if name in latest['inventory']}
            component = observed['components']['builds']
            if state.commit_build_heartbeat(db, latest, patches, component):
                snapshot = state.read(db)
            else:
                snapshot = state.merge(latest, 'builds', {'builds': patches}, {'builds': component})
                state.commit(db, snapshot, previous=previous)
    return snapshot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--db', required=True)
    p.add_argument('--source-limit', type=int, help='0: full initial fill; default: configured bounded batch')
    p.add_argument('--only', choices=('all', 'obs', 'obs-metadata', 'builds', 'upstreams', 'specs', 'monitors', 'github', 'commits'), default='all',
                   help='independent collection phases share one serialized snapshot writer; obs-metadata owns source/history, builds owns status; obs runs both')
    p.add_argument('--track', action='append', help='check only this configured upstream track; repeat with --only upstreams')
    p.add_argument('--due', action='store_true', help='select changed, expired or retryable upstream tracks')
    args = p.parse_args()
    if args.source_limit is not None and args.source_limit < 0:
        p.error('--source-limit must be nonnegative')
    if args.track is not None and args.only != 'upstreams':
        p.error('--track requires --only upstreams')
    if args.due and (args.only != 'upstreams' or args.track):
        p.error('--due requires --only upstreams without --track')
    config = cfg.load(args.config)
    try:
        selected = nv.selected_names(config, args.track)
    except ValueError as error:
        p.error(str(error))
    attempt = {}
    try:
        # OBS and upstream phases each take the writer lock only around their own local
        # commit. Remote upstream requests never run while the snapshot lock is held.
        snapshot = None
        if args.only in ('all', 'obs', 'obs-metadata'):
            snapshot = collect_obs(config, args.db, args.source_limit)
        if args.only in ('all', 'obs', 'builds'):
            snapshot = collect_builds(config, args.db)
        if args.only in ('all', 'upstreams'):
            snapshot = check_upstreams(config, args.config, args.db, tracks=selected, attempt=attempt,
                                       **({'due': True} if args.due else {}))
        if args.only in ('all', 'specs'):
            snapshot = check_specs(config, args.db)
        if args.only in ('all', 'monitors'):
            from tracker.monitors import runner as monitor
            snapshot = monitor.collect(config, args.config, args.db)
        if args.only in ('all', 'github'):
            from tracker.monitors.github.collector import collect as collect_github
            snapshot = collect_github(config, args.db)
        if args.only in ('all', 'commits'):
            from tracker.monitors.commits.collector import collect as collect_commits
            snapshot = collect_commits(config, args.db)
    except BlockingIOError:
        print(json.dumps({'error': 'collector already running'}))
        return 75
    if snapshot is None:
        raise ValueError('No collection phase selected')
    collection_errors = [v['error'] for v in snapshot['components'].values() if v.get('error')]
    owners = {'obs': ('obs', 'builds'), 'obs-metadata': ('obs',)}.get(args.only, (args.only,))
    errors = [v['error'] for key, v in snapshot['components'].items() if v.get('error')
              and (args.only == 'all' or any(state.owns_component(owner, key) for owner in owners))]
    result = dict(generation=snapshot['generation'], packages=len(state.package_names(snapshot)),
                  source_versions=sum(bool(state.current_source(snapshot, name).get('version'))
                                      for name in state.package_names(snapshot)),
                  tracks=len(snapshot['tracks']), errors=errors)
    if selected is not None or args.due:
        errors = ([attempt['command_error']] if attempt['command_error'] else []) + [
            f'{name}: {error}' for name, error in attempt['track_errors'].items()]
        result.update(attempt, errors=errors)
    if collection_errors != errors:
        result['collection_errors'] = collection_errors
    print(json.dumps(result, ensure_ascii=False))
    return 2 if errors else 0

if __name__ == '__main__':
    raise SystemExit(main())
