"""Collect commit batches outside the shared snapshot writer lock."""
import json
import time
from datetime import datetime, timedelta
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
    source = version.source
    targets = {}
    source_url = primary_source_url(source)
    tag = pinned_tag(source)
    pinned = pinned_revision(source)
    for side, release in [('current', version.subject['version']),
                          ('upgrade', version.target_version if version.upgrading else None)]:
        target: dict[str, Any] | None = None
        mapping = policy.mappings.get(release) if release else None
        if (mapping and mapping.source_revision == version.subject['revision']
                and (side == 'upgrade' or mapping.source_url == source_url)):
            target = dict(oid=mapping.oid, version=release, revision=version.subject['revision'],
                          mapping_version=mapping.version, ref=mapping.ref, source_url=mapping.source_url,
                          review_url=mapping.review_url, side=side, source_kind='upstream')
        elif (side == 'current' and pinned and pinned.repository.rstrip('/') == repository.url.removesuffix('.git')):
            target = dict(oid=pinned.current, version=release, revision=version.subject['revision'],
                          mapping_version=0, ref=None, source_url=source_url, review_url=None,
                          side=side, source_kind='upstream')
        if target and side == 'current' and pinned and (
                pinned.repository.rstrip('/') != repository.url.removesuffix('.git')
                or pinned.current != target['oid']):
            target = None
        if target and side == 'current' and tag and (
                tag['repository'].rstrip('/') != repository.url.removesuffix('.git')):
            target = None
        if target and len(target['oid']) != (64 if repository.object_format == 'sha256' else 40):
            target = None
        targets[side] = (target if version.subject['revision'] and not version.source_unavailable
                         and not version.identity_conflict else None)
    return targets


def input_key(config, snapshot, name, policy, repository):
    version = evaluate(snapshot, name)
    return digest(dict(package=name, method_version=METHOD_VERSION, subject=version.subject, upgrading=version.upgrading,
                       source=source_binding(version.source),
                       target=version.target_version, identity_conflict=version.identity_conflict,
                       source_unavailable=version.source_unavailable,
                       upstream_identity=version.upstream.get('source'),
                       targets=source_targets(version, policy, repository),
                       policy=policy.model_dump(), repository=repository.model_dump(),
                       monitor_policy=settings(config).model_dump(exclude={'repositories'}),
                       config=config.get('config_digest')))


def _fresh(saved, at, ttl):
    return bool(saved and saved.get('checked_at') and not saved.get('retained')
                and not saved.get('error') and saved.get('coverage', {}).get('complete')
                and not state.stale({'fetched_at': saved['checked_at']}, datetime.fromisoformat(at), ttl))


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
    end = min(len(progress['ids']) + options.batch_size, len(ids))
    for value in ids[len(progress['ids']):end]:
        if repo.deadline is not None and time.monotonic() >= repo.deadline:
            raise GitError('batch_timeout')
        matches = match_rules(records[value], policy.rules, timeout_seconds=options.regex_timeout_seconds,
                              text_limit=options.text_limit)
        if matches:
            total_matches += 1
            selected.append(dict(oid=value, reasons=matches[:128], reasons_truncated=len(matches) > 128))
            selected.sort(key=lambda item: item['oid'])
            del selected[options.max_findings:]
            while selected and len(json.dumps(selected, ensure_ascii=True).encode()) > options.max_batch_bytes:
                selected.pop()
    progress.update(ids=ids[:end], matches=selected, total=total_matches)
    selected = sorted(selected, key=lambda item: item['oid'])
    return progress, selected[:options.max_findings], total_matches


def _scheduled(visible, targets, evidence, repository, rule_digest, analysis_range, options, at):
    queued = []
    for item in visible:
        for target in targets.values():
            if target is None:
                continue
            key = evidence_key(repository.url, item['oid'], target, rule_digest, analysis_range)
            prior = evidence.get(key, {})
            if not _fresh(prior, at, options.stale_after_seconds):
                queued.append((prior.get('attempted_at') or '', key))
    return {key for _, key in sorted(queued)[:options.batch_size]}


def _checked(repo, repository, candidate, target, observed_at, at, saved):
    result: dict[str, Any]
    try:
        try:
            repo.resolve(target['oid'])
        except GitError:
            repo.fetch(repository.url, target['oid'], depth=repository.fetch_depth, object_format=repository.object_format)
        if target.get('ref'):
            observed = repo.fetch(repository.url, target['ref'], depth=repository.fetch_depth, object_format=repository.object_format)
            if observed != target['oid']:
                raise GitError('tag_drift')
        result = dict(analyze(repo, candidate, target['oid'], history_limit=repository.history_limit,
                              history_start=repository.analysis_start))
    except GitError as error:
        result = dict(status='unknown', reason=error.code, matches=[], reverts=[], strict_manifest=None, coverage={'complete': False})
    succeeded = result['coverage'].get('complete') or result['status'] in (
        'original_reachable', 'strict_patch_evidence')
    result.update(checked_at=observed_at if succeeded else None)
    if not result['coverage'].get('complete') and saved and saved.get('checked_at'):
        return {**saved, 'retained': True, 'error': result['reason'], 'attempted_at': at}
    return result


def _side(side, target, upgrading, candidate, key, saved, scheduled, repo, repository, options, scan, at, evidence):
    if target is None:
        reason = 'no_candidate_release' if side == 'upgrade' and not upgrading else 'target_unresolved'
        return dict(status='unknown', reason=reason, matches=[], reverts=[], strict_manifest=None,
                    coverage={'complete': False}, checked_at=None)
    if _fresh(saved, at, options.stale_after_seconds):
        return dict(saved)
    if key in scheduled:
        result = _checked(repo, repository, candidate, target, scan['observed_at'], at, saved)
        evidence[key] = {**result, 'attempted_at': at, 'target': target, 'candidate': candidate}
        return result
    return ({**saved, 'retained': True} if saved else
            dict(status='unknown', reason='pending', matches=[], reverts=[], strict_manifest=None,
                 coverage={'complete': False}, checked_at=None))


def _incomplete(result):
    return result.get('reason') != 'no_candidate_release' and (
        result.get('retained') or not result.get('checked_at') or not result.get('coverage', {}).get('complete'))


def build_batch(config, snapshot, name, policy, options, scan, records, previous_evidence: dict[str, dict[str, Any]],
                repo, at, previous_rules=None):
    repository = options.repositories[policy.repository]
    version = evaluate(snapshot, name)
    targets = source_targets(version, policy, repository)
    rule_digest = digest([rule.model_dump() for rule in policy.rules])
    progress, visible, total_matches = _matching(policy, repository, options, scan, records,
                                                previous_rules, repo, rule_digest)
    ids = scan.get('ids', [])[:scan.get('cursor', 0)]
    evidence = dict(previous_evidence)
    analysis_range = {'target_history_limit': repository.history_limit, 'start': repository.analysis_start}
    entries, batch_bytes = [], 0
    scheduled = _scheduled(visible, targets, evidence, repository, rule_digest, analysis_range, options, at)
    matching_complete = len(progress['ids']) == len(ids) and scan.get('complete')
    pending = (not matching_complete or not scan.get('history_complete', False)
               or len(visible) != total_matches)
    active_keys = set()
    for item in visible:
        entry = {**item, 'sides': {}}
        for side, target in targets.items():
            result: dict[str, Any]
            key = evidence_key(repository.url, item['oid'], target, rule_digest, analysis_range)
            active_keys.add(key)
            saved = evidence.get(key)
            result = _side(side, target, version.upgrading, item['oid'], key, saved, scheduled,
                           repo, repository, options, scan, at, evidence)
            pending = pending or _incomplete(result)
            result.pop('candidate', None)
            result.pop('target', None)
            entry['sides'][side] = {**result, 'target': target, 'key': key}
            if result.get('reason') != 'no_candidate_release':
                entry['sides'][side]['attempted_at'] = evidence.get(key, {}).get('attempted_at')
        size = len(json.dumps(entry, ensure_ascii=True).encode())
        if batch_bytes + size > options.max_batch_bytes:
            pending = True
            break
        batch_bytes += size
        entries.append(entry)
    batch = dict(method_version=METHOD_VERSION, stale_after_seconds=options.stale_after_seconds, web_url=repository.web_url, source_identity=version.subject, source_binding=source_binding(version.source),
                 upstream_binding=digest(version.upstream.get('source')),
                 target_version=version.target_version if version.upgrading else None, input_key=input_key(config, snapshot, name, policy, repository),
                 repository=policy.repository, targets=targets, scan_tip=scan.get('tip'),
                 rule_digest=rule_digest, selected=len(entries), total=total_matches,
                 status='partial' if pending else 'ok', checked_at=scan.get('observed_at'),
                 attempted_at=at, entries=entries, coverage={'complete': bool(matching_complete and scan.get('history_complete')),
                 'matching_processed': len(progress['ids']), 'scanned': len(ids),
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
        records = {key: dict(value) for key, value in snapshot.get('commits_records', {}).items()}
        scans = {key: dict(value) for key, value in snapshot.get('commits_scans', {}).items()}
        evidence = {key: dict(value) for key, value in snapshot.get('commits_evidence', {}).items()}
        batches = {key: dict(value) for key, value in snapshot.get('commits_batches', {}).items()}
        inputs = {name: input_key(config, snapshot, name, package, options.repositories[package.repository])
                  for name, package in packages.items() if name in state.package_names(snapshot)}
        components = {}
        for identity in sorted({policy.repository for policy in packages.values()}):
            policy = options.repositories[identity]
            prior = scans.get(identity, {}).get('active', {})
            component = snapshot.get('components', {}).get('commits:' + identity, {})
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
                    components['commits:' + identity] = dict(attempted_at=at, fetched_at=scan['observed_at'], error=None)
                    ordered_packages = sorted(packages.items(), key=lambda item:
                        snapshot.get('components', {}).get('commits:package:' + item[0], {}).get('attempted_at') or '')
                    for name, package in ordered_packages:
                        if package.repository != identity or name not in state.package_names(snapshot):
                            continue
                        package_component = 'commits:package:' + name
                        try:
                            batch, package_evidence, rule_progress = build_batch(
                                config, snapshot, name, package, options, scan, current_records,
                                evidence.get(name, {}), repo, at, batches.get(name, {}).get('rules'))
                            evidence[name] = package_evidence
                            batches.setdefault(name, {}).update(active=batch, rules=rule_progress)
                            components[package_component] = dict(attempted_at=at, error=None)
                        except (GitError, RuleError, ValueError) as error:
                            components[package_component] = dict(attempted_at=at,
                                error=getattr(error, 'code', type(error).__name__))
            except (GitError, RuleError, ValueError) as error:
                failures = component.get('failures', 0) + 1
                retry = datetime.fromisoformat(at) + timedelta(seconds=polling(config).delay(failures))
                components['commits:' + identity] = dict(component, attempted_at=at, failures=failures,
                    error=getattr(error, 'code', type(error).__name__), retry_at=retry.isoformat(timespec='seconds'))
        with state.writer_lock(db, timeout=60):
            cfg.require_unchanged(config)
            previous = state.read_cached(db)
            latest = previous[0]
            valid_batches = {}
            for name, package in packages.items():
                valid_batches[name] = {'policy': {'repository': package.repository,
                    'stale_after_seconds': options.stale_after_seconds}}
                batch = batches.get(name, {}).get('active')
                if batch and name in state.package_names(latest):
                    expected = input_key(config, latest, name, package, options.repositories[package.repository])
                    if batch['input_key'] == inputs.get(name) == expected:
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
