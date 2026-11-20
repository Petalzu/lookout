"""Collect commit batches outside the shared snapshot writer lock."""
import time
from bisect import insort
from datetime import datetime, timedelta
from operator import itemgetter
from pathlib import Path
from typing import Any

from tracker import config as cfg
from tracker import state
from tracker.monitors.commits.analysis import analyze
from tracker.monitors.commits.git import GitError, Repository
from tracker.monitors.commits.model import (
    METHOD_VERSION,
    digest,
    evidence_key,
    json_size,
    package_policies,
    polling,
    settings,
    source_binding,
)
from tracker.monitors.commits.rules import RuleError, match_rules
from tracker.monitors.commits.scan import advance
from tracker.monitors.source.release import pinned_revision, pinned_tag, primary_source_url
from tracker.monitors.version.compare import evaluate


def source_targets(version, policy, repository):
    targets: dict[str, dict[str, Any] | None] = {'current': None, 'upgrade': None}
    revision = version.subject['revision']
    if not revision or version.source_unavailable or version.identity_conflict:
        return targets

    source = version.source
    source_url = primary_source_url(source)
    pinned = pinned_revision(source)
    tag = pinned_tag(source)
    repo_url = repository.url.removesuffix('.git')
    oid_length = 64 if repository.object_format == 'sha256' else 40
    for side, release in [('current', version.subject['version']),
                          ('upgrade', version.target_version if version.upgrading else None)]:
        mapping = policy.mappings.get(release) if release else None
        if (mapping and mapping.source_revision == revision
                and (side == 'upgrade' or mapping.source_url == source_url)):
            target: dict[str, Any] = dict(oid=mapping.oid, mapping_version=mapping.version, ref=mapping.ref,
                                          source_url=mapping.source_url, review_url=mapping.review_url)
        elif side == 'current' and pinned:
            target = dict(oid=pinned.current, mapping_version=0, ref=None, source_url=source_url, review_url=None)
        else:
            continue

        if side == 'current':
            if pinned and (pinned.repository != repo_url or pinned.current != target['oid']):
                continue
            if tag:
                ref = 'refs/tags/' + tag['tag']
                if tag['repository'] != repo_url or target['ref'] not in (None, ref):
                    continue
                target['ref'] = ref
        if len(target['oid']) == oid_length:
            targets[side] = dict(target, side=side, version=release, revision=revision, source_kind='upstream')
    return targets


def _batch_input(config, snapshot, name, policy, options, now):
    """Bind the same release decision and targets used for analysis to the publication key."""
    repository = options.repositories[policy.repository]
    version = evaluate(snapshot, name, now)
    targets = source_targets(version, policy, repository)
    key = digest(dict(package=name, method_version=METHOD_VERSION, subject=version.subject, upgrading=version.upgrading,
                      source=source_binding(version.source), target=version.target_version,
                      identity_conflict=version.identity_conflict, source_unavailable=version.source_unavailable,
                      upstream_identity=version.upstream.get('source'), targets=targets,
                      policy=policy.model_dump(), repository=repository.model_dump(),
                      monitor_policy=options.model_dump(exclude={'repositories'}), config=config.get('config_digest')))
    return key, version, targets


def _fresh(saved, at, ttl):
    return bool(saved and saved.get('checked_at') and not saved.get('retained')
                and not saved.get('error') and saved.get('coverage', {}).get('complete')
                and not state.stale(saved, datetime.fromisoformat(at), ttl))


def _matching(policy, repository, options, scan, records, previous_rules, repo, rule_digest):
    ids = scan.get('ids', [])[:scan.get('cursor', 0)]
    progress_key = digest({'rules': rule_digest, 'text_limit': options.text_limit,
                           'regex_timeout': options.regex_timeout_seconds,
                           'max_findings': options.max_findings, 'max_bytes': options.max_batch_bytes,
                           'matcher_version': 1})
    progress = dict(previous_rules or {})
    old_ids = progress.get('ids', [])
    if (progress.get('rule_digest') != progress_key or ids[:len(old_ids)] != old_ids
            or progress.get('repository') != repository.url):
        progress = {'rule_digest': progress_key, 'repository': repository.url, 'ids': [], 'matches': [], 'total': 0}
    selected = list(progress['matches'])
    total_matches = progress.get('total', len(selected))
    selected_bytes = None
    end = min(len(progress['ids']) + options.batch_size, len(ids))
    for value in ids[len(progress['ids']):end]:
        if repo.deadline is not None and time.monotonic() >= repo.deadline:
            raise GitError('batch_timeout')
        matches = match_rules(records[value], policy.rules, timeout_seconds=options.regex_timeout_seconds,
                              text_limit=options.text_limit)
        if matches:
            total_matches += 1
            if len(selected) == options.max_findings and value > selected[-1]['oid']:
                continue
            # For a nonempty ASCII JSON array, brackets and ", " separators cost two bytes per item.
            # Rebuild this transient sum only on the first insertion into a resumed selection.
            if selected_bytes is None:
                selected_bytes = sum(json_size(item) + 2 for item in selected)
            entry = dict(oid=value, reasons=matches[:128], reasons_truncated=len(matches) > 128)
            insort(selected, entry, key=itemgetter('oid'))
            selected_bytes += json_size(entry) + 2
            while len(selected) > options.max_findings or selected_bytes > options.max_batch_bytes:
                selected_bytes -= json_size(selected.pop()) + 2
    progress.update(ids=ids[:end], matches=selected, total=total_matches)
    return progress


def _scheduled(visible, targets, evidence, repository, rule_digest, analysis_range, options, at):
    queued = []
    for item in visible:
        for target in targets:
            key = evidence_key(repository.url, item['oid'], target, rule_digest, analysis_range)
            prior = evidence.get(key, {})
            if not _fresh(prior, at, options.stale_after_seconds):
                queued.append((prior.get('attempted_at') or '', key))
    return {key for _, key in sorted(queued)[:options.batch_size]}


def _ref_errors(repo, repository, targets):
    # A release ref is mutable. Every candidate in this batch shares its observation, even a fresh cache hit.
    errors = {}
    for side, target in targets.items():
        if target and target['ref']:
            try:
                observed = repo.fetch(repository.url, target['ref'], depth=repository.fetch_depth,
                                      object_format=repository.object_format)
                if observed != target['oid']:
                    errors[side] = 'tag_drift'
            except GitError as error:
                errors[side] = error.code
    return errors


def _unknown(reason) -> dict[str, Any]:
    return dict(status='unknown', reason=reason, matches=[], reverts=[], strict_manifest=None,
                coverage={'complete': False}, checked_at=None)


def _checked(repo, repository, candidate, target) -> dict[str, Any]:
    try:
        try:
            repo.resolve(target['oid'])
        except GitError:
            repo.fetch(repository.url, target['oid'], depth=repository.fetch_depth, object_format=repository.object_format)
        return dict(analyze(repo, candidate, target['oid'], history_limit=repository.history_limit,
                            history_start=repository.analysis_start))
    except GitError as error:
        return _unknown(error.code)


def _side(side, target, ref_error, upgrading, candidate, key, scheduled, repo, repository, options, scan, at, evidence):
    if target is None:
        return _unknown('no_candidate_release' if side == 'upgrade' and not upgrading else 'target_unresolved')
    saved = evidence.get(key)
    if not ref_error and key not in scheduled:
        if not saved:
            return _unknown('pending')
        return dict(saved) if _fresh(saved, at, options.stale_after_seconds) else {**saved, 'retained': True}
    result = _unknown(ref_error) if ref_error else _checked(repo, repository, candidate, target)
    complete = result['coverage'].get('complete')
    if not complete and saved and saved.get('checked_at'):
        result = {**saved, 'retained': True, 'error': result['reason']}
    else:
        succeeded = complete or result['status'] in ('original_reachable', 'strict_patch_evidence')
        result['checked_at'] = scan['observed_at'] if succeeded else None
    evidence[key] = {**result, 'attempted_at': at, 'target': target, 'candidate': candidate}
    return result


def _incomplete(result):
    return result.get('reason') != 'no_candidate_release' and (
        result.get('retained') or not result.get('checked_at') or not result.get('coverage', {}).get('complete'))


def build_batch(binding, policy, options, scan, records, previous_evidence: dict[str, dict[str, Any]],
                repo, at, previous_rules=None):
    repository = options.repositories[policy.repository]
    input_key, version, targets = binding
    rule_digest = digest([rule.model_dump() for rule in policy.rules])
    progress = _matching(policy, repository, options, scan, records, previous_rules, repo, rule_digest)
    visible, total_matches = progress['matches'], progress['total']
    scanned = scan['cursor']
    evidence = dict(previous_evidence)
    analysis_range = {'target_history_limit': repository.history_limit, 'start': repository.analysis_start}
    entries, batch_bytes = [], 0
    ref_errors = _ref_errors(repo, repository, targets) if visible else {}
    eligible = [target for side, target in targets.items() if target and side not in ref_errors]
    scheduled = _scheduled(visible, eligible, evidence, repository, rule_digest, analysis_range, options, at)
    matching_complete = len(progress['ids']) == scanned and scan.get('complete')
    pending = (not matching_complete or not scan.get('history_complete', False)
               or len(visible) != total_matches)
    active_keys = set()
    for item in visible:
        entry = {**item, 'sides': {}}
        for side, target in targets.items():
            result: dict[str, Any]
            key = evidence_key(repository.url, item['oid'], target, rule_digest, analysis_range)
            active_keys.add(key)
            result = _side(side, target, ref_errors.get(side), version.upgrading, item['oid'], key, scheduled,
                           repo, repository, options, scan, at, evidence)
            pending = pending or _incomplete(result)
            result.pop('candidate', None)
            result.pop('target', None)
            entry['sides'][side] = {**result, 'target': target, 'key': key}
            if result.get('reason') != 'no_candidate_release':
                entry['sides'][side]['attempted_at'] = evidence.get(key, {}).get('attempted_at')
        size = json_size(entry)
        if batch_bytes + size > options.max_batch_bytes:
            pending = True
            break
        batch_bytes += size
        entries.append(entry)
    batch = dict(method_version=METHOD_VERSION, stale_after_seconds=options.stale_after_seconds, web_url=repository.web_url, source_identity=version.subject, source_binding=source_binding(version.source),
                 upstream_binding=digest(version.upstream.get('source')),
                 target_version=version.target_version if version.upgrading else None, input_key=input_key,
                 repository=policy.repository, targets=targets, scan_tip=scan.get('tip'),
                 rule_digest=rule_digest, selected=len(entries), total=total_matches,
                 status='partial' if pending else 'ok', checked_at=scan.get('observed_at'),
                 attempted_at=at, entries=entries, coverage={'complete': bool(matching_complete and scan.get('history_complete')),
                 'matching_processed': len(progress['ids']), 'scanned': scanned,
                 'start': scan.get('start'), 'tip': scan.get('tip'), 'truncated': len(entries) != total_matches})
    stamps = [side['checked_at'] for entry in entries for side in entry['sides'].values()
              if side.get('checked_at')]
    batch['checked_at'] = min(stamps, default=scan.get('observed_at')
                              if not entries and matching_complete else None)
    batch['id'] = digest({key: batch[key] for key in
                          ('input_key', 'scan_tip', 'rule_digest', 'coverage', 'entries')})
    # Keep a bounded historical tail; never discard the active batch's evidence.
    historical = sorted((key for key in evidence if key not in active_keys),
                        key=lambda key: evidence[key].get('attempted_at') or '', reverse=True)
    keep = active_keys | set(historical[:options.max_findings * 2])
    return batch, {key: value for key, value in evidence.items() if key in keep}, progress


def collect(config, db, *, repositories=None, now=None):
    options, packages = settings(config), package_policies(config)
    at = now or state.utcnow()
    with state.writer_lock(str(db) + '.commits'):
        snapshot = state.read(db)
        if not packages and not any(snapshot.get(key) for key in
                ('commits_batches', 'commits_records', 'commits_scans', 'commits_evidence')):
            return snapshot
        records = dict(snapshot.get('commits_records', {}))
        scans = dict(snapshot.get('commits_scans', {}))
        evidence = dict(snapshot.get('commits_evidence', {}))
        batches = dict(snapshot.get('commits_batches', {}))
        started = datetime.fromisoformat(at)
        inputs = {name: _batch_input(config, snapshot, name, package, options, started)
                  for name, package in packages.items() if name in state.package_names(snapshot)}
        components = {}
        for identity in sorted({policy.repository for policy in packages.values()}):
            policy = options.repositories[identity]
            prior = scans.get(identity, {}).get('active', {})
            component_key = 'commits:' + identity
            fingerprint = policy.fingerprint
            component = snapshot.get('components', {}).get(component_key, {})
            if component.get('fingerprint') != fingerprint:
                component = {}
            if component.get('retry_at') and datetime.fromisoformat(at) < datetime.fromisoformat(component['retry_at']):
                continue
            cache_identity = identity + '-' + digest({'url': policy.url, 'format': policy.object_format})[:16]
            repo = (repositories or {}).get(identity) or Repository(Path(db).parent / 'commits-cache' / cache_identity,
                timeout=policy.timeout_seconds, output_limit=policy.output_limit, disk_limit=policy.disk_limit)
            try:
                repo.begin_round()
                with state.writer_lock(str(repo.path) + '.lookout'):
                    prior_records = records.get(identity, {})
                    if prior.get('repository_url') not in (None, policy.url):
                        prior_records = {}
                    scan, current_records = advance(repo, policy, prior, prior_records, at,
                                                    options.batch_size, fetch=repositories is None)
                    scan['repository_url'] = policy.url
                    records[identity] = current_records
                    scans[identity] = {'active': scan}
                    components[component_key] = dict(attempted_at=at, fetched_at=scan['observed_at'], error=None)
                    ordered_packages = sorted(packages.items(), key=lambda item:
                        snapshot.get('components', {}).get('commits:package:' + item[0], {}).get('attempted_at') or '')
                    for name, package in ordered_packages:
                        if package.repository != identity or name not in inputs:
                            continue
                        package_component = 'commits:package:' + name
                        try:
                            batch, package_evidence, rule_progress = build_batch(
                                inputs[name], package, options, scan, current_records,
                                evidence.get(name, {}), repo, at, batches.get(name, {}).get('rules'))
                            evidence[name] = package_evidence
                            batches[name] = dict(active=batch, rules=rule_progress)
                            components[package_component] = dict(attempted_at=at, error=None)
                        except (GitError, RuleError, ValueError) as error:
                            components[package_component] = dict(attempted_at=at,
                                error=getattr(error, 'code', type(error).__name__))
            except (GitError, RuleError, ValueError) as error:
                failures = component.get('failures', 0) + 1
                retry = datetime.fromisoformat(at) + timedelta(seconds=polling(config).delay(failures))
                components[component_key] = dict(component, attempted_at=at, failures=failures,
                    error=getattr(error, 'code', type(error).__name__), retry_at=retry.isoformat(timespec='seconds'))
            components[component_key]['fingerprint'] = fingerprint
        with state.writer_lock(db, timeout=60):
            cfg.require_unchanged(config)
            previous = state.read_cached(db)
            latest = previous[0]
            # A live run can cross an upstream TTL while Git is busy. A replay uses its supplied clock.
            published = started if now is not None else datetime.fromisoformat(state.utcnow())
            current_options = settings(config)
            valid_batches = {}
            for name, package in packages.items():
                valid_batches[name] = {'policy': {'repository': package.repository,
                    'stale_after_seconds': options.stale_after_seconds}}
                batch = batches.get(name, {}).get('active')
                if batch and name in inputs and name in state.package_names(latest):
                    expected, _, _ = _batch_input(config, latest, name, package, current_options, published)
                    if batch['input_key'] == inputs[name][0] == expected:
                        valid_batches[name].update(active=batch, rules=batches[name].get('rules', {}))
            active_repositories = {package.repository for package in packages.values()}
            records = {key: value for key, value in records.items() if key in active_repositories}
            scans = {key: value for key, value in scans.items() if key in active_repositories}
            evidence = {key: value for key, value in evidence.items() if key in packages}
            fields = dict(commits_records=records, commits_scans=scans, commits_evidence=evidence,
                          commits_batches=valid_batches)
            result = state.merge(latest, 'commits', fields, components)
            valid_components = {'commits:' + key for key in active_repositories}
            valid_components.update('commits:package:' + key for key in packages)
            for key in list(result['components']):
                if key.startswith('commits:') and key not in valid_components:
                    del result['components'][key]
            if {key: value for key, value in result.items() if key != 'generation'} == {
                    key: value for key, value in latest.items() if key != 'generation'}:
                return latest
            state.commit(db, result, previous=previous)
            return result
