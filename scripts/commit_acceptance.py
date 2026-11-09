#!/usr/bin/env python3
"""Bounded, offline acceptance: complete-window labels, replay, crash recovery and repeats.

Run with --work-root and --output under an approved persistent experiment directory.
Cold means fresh Repository Python caches, not cold OS/disk. No cache dropping or network.
Synthetic collector tests inject local Git object IO and a fixed configuration; they do not
claim SPEC isolation, production NFS durability or human semantic relevance acceptance.
"""
import argparse
import base64
import json
import os
import resource
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from commit_case_oracle import RawGit, environment, fingerprint, sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

FIXED_AT = '2026-01-02T03:04:05+00:00'
RUNNER_VERSION = 2


def write_new(path, data):
    with Path(path).open('x', encoding='utf-8') as output:
        output.write(json.dumps(data, indent=2) + '\n')


def disk_bytes(path):
    return sum(p.stat().st_size for p in Path(path).rglob('*') if p.is_file())


def limits():
    if hasattr(os, 'sched_setaffinity'):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024 ** 3, 4 * 1024 ** 3))
    os.nice(10)


def fixed_fixture(path):
    path.mkdir()
    git = RawGit(path)
    git.run('init', '-q', '-b', 'main')
    git.run('config', 'user.name', 'Acceptance Fixture')
    git.run('config', 'user.email', 'fixture@example.invalid')
    git.run('config', 'commit.gpgsign', 'false')
    (path / 'target').mkdir()
    (path / 'target' / 'riscv.txt').write_text('base\n')
    (path / 'generic.txt').write_text('base\n')
    oids = []
    env = dict(os.environ, GIT_AUTHOR_DATE=FIXED_AT, GIT_COMMITTER_DATE=FIXED_AT,
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
    for index in range(29):
        if index:
            name = 'target/riscv.txt' if index % 3 == 0 else 'generic.txt'
            with (path / name).open('a') as output:
                output.write(f'fixed object {index:02d}\n')
        git.run('add', '.')
        subprocess.run(['git', '-c', f'safe.directory={path}', '-c', 'core.hooksPath=/dev/null',
                        '-c', 'commit.gpgsign=false', '-C', str(path), 'commit', '-q', '-m',
                        'base' if not index else (f'RISCV: fixed change {index}' if index % 3 == 0 else f'generic change {index}')],
                       env=env, check=True, capture_output=True)
        oids.append(git.text('rev-parse', 'HEAD'))
    return {'path': str(path), 'base': oids[0], 'initial_tip': oids[24], 'append_tip': oids[28],
            'oids': oids, 'clock': FIXED_AT, 'history_count': 28}


def config_snapshot(fixture, rule_variant=False):
    from tracker import state
    from tracker.config import track_fingerprint
    config = {'obs': {'api_url': 'https://example.invalid', 'web_url': 'https://example.invalid', 'project': 'fixture'},
              'targets': [], 'native': {'binutils': {'source': 'manual'}}, 'packages': {},
              'collector': {'source_batch_size': 100, 'source_workers': 1, 'stale_after_seconds': 86400},
              'config_digest': 'fixed-offline-acceptance-v1'}
    snapshot = state.empty()
    snapshot.update(generation=1, last_attempt=FIXED_AT, mode='fixture', targets=[], bindings={},
                    obs=config['obs'], native_ids=['binutils'], stale_after_seconds=86400)
    snapshot['inventory'] = {'binutils': 'binutils'}
    snapshot['sources']['binutils'] = state.success({}, {'version': '3.9.0', 'srcmd5': 'fixed', 'rev': '1'}, FIXED_AT)
    snapshot['index']['binutils'] = {'srcmd5': 'fixed', 'rev': '1'}
    snapshot['tracks']['binutils'] = state.success({}, {'version': '3.9.0', 'source_checked_at': None,
        'source': {'source': 'fixture'}, 'configuration_fingerprint': track_fingerprint(config['native']['binutils'])}, FIXED_AT)
    snapshot['specs'] = {'binutils': {'head': fixture['base'], 'fetched_at': FIXED_AT, 'error': None,
        'source_origin': {'url': 'https://example.invalid/spec', 'branch': 'main'},
        'native_query': {'spec_sha256': 'a' * 64, 'context': {'resolver': 6}},
        'metadata': {'version': '3.9.0', 'sources': [{'number': 0, 'url': 'https://example.invalid/source.tar.xz'}]}}}
    revision = state.current_source(snapshot, 'binutils')['revision']
    config['commits'] = {'repositories': {'fixture': {'url': 'https://example.invalid/upstream.git',
        'web_url': 'https://example.invalid/upstream', 'ref': 'refs/heads/acceptance',
        'window_start': fixture['base'], 'analysis_start': fixture['base']}}, 'batch_size': 3}
    rules = [{'id': 'riscv', 'keywords': ['RISCV']}]
    if rule_variant:
        rules.append({'id': 'generic', 'paths': ['generic.txt']})
    config['packages']['binutils'] = {'monitors': {'commits': {'repository': 'fixture', 'rules': rules,
        'mappings': {'3.9.0': {'oid': fixture['initial_tip'], 'source_revision': revision,
            'source_url': 'https://example.invalid/source.tar.xz', 'review_url': 'https://example.invalid/review'}}}}}
    return config, snapshot


def projection(snapshot):
    return {key: snapshot.get(key) for key in
            ('commits_records', 'commits_scans', 'commits_evidence', 'commits_batches')}



def verify_fixture(snapshot, fixture, tip, rule_variant=False):
    """Check fixed construction facts, independently of matching and full recomputation."""
    stop = fixture['oids'].index(tip)
    ids = fixture['oids'][1:stop + 1]
    expected = set(ids if rule_variant else (value for index, value in enumerate(fixture['oids'])
                                             if 0 < index <= stop and index % 3 == 0))
    batch = snapshot['commits_batches']['binutils']['active']
    scan = snapshot['commits_scans']['fixture']['active']
    assert set(scan['ids']) == set(ids) and scan['tip'] == tip and scan['complete']
    assert {entry['oid'] for entry in batch['entries']} == expected, 'fixed candidate OID set differs'
    assert batch['selected'] == batch['total'] == len(expected), 'fixed candidate counts differ'
    target = batch['targets']['current']
    assert target['oid'] == fixture['initial_tip'] and target['version'] == '3.9.0'
    assert target['side'] == 'current' and target['source_kind'] == 'upstream'
    assert target['revision'] == batch['source_identity']['revision']
    assert batch['targets']['upgrade'] is None
    for entry in batch['entries']:
        current, upgrade = entry['sides']['current'], entry['sides']['upgrade']
        included = fixture['oids'].index(entry['oid']) <= 24
        assert current['status'] == ('original_reachable' if included else 'no_evidence_in_range')
        assert current['matches'] == ([entry['oid']] if included else []) and current['reverts'] == []
        assert current['checked_at'] == FIXED_AT and not current.get('retained')
        assert current['target'] == target and current['coverage']['complete']
        assert current['coverage']['target'] == fixture['initial_tip']
        assert upgrade['reason'] == 'no_candidate_release' and upgrade['target'] is None
    return {'passed': True, 'candidate_oids': sorted(expected), 'candidate_count': len(expected),
            'scan_tip': tip, 'target_oid': fixture['initial_tip'], 'rule_variant': rule_variant,
            'basis': 'fixed construction: indices divisible by three, union generic path on replay; target index24'}


def worker(request_path):
    limits()
    from tracker import state
    from tracker.monitors.commits import collector
    from tracker.monitors.commits.git import Repository
    req = json.loads(Path(request_path).read_text())
    folder = Path(req['folder'])
    fixture = req['fixture']
    config, initial = config_snapshot(fixture, req.get('rules', False))
    collector.cfg.require_unchanged = lambda *_: None
    db = folder / 'state.db'
    if not db.exists():
        state.commit(db, initial)
    marker = folder / 'fault.json'
    calls, scanned = [], set()

    def crash(stage):
        if req.get('fault') == stage:
            with marker.open('x') as output:
                json.dump({'stage': stage, 'pid': os.getpid(), 'clock': FIXED_AT,
                           'db_sha256': sha256(db.read_bytes()), 'git_calls': len(calls),
                           'mechanism': 'os._exit(73) at synchronous hook; no sleep',
                           'fetched_tip': RawGit(folder / 'git-cache').text('rev-parse', '--verify', 'refs/lookout/fetched'),
                           'git_cache_bytes': disk_bytes(folder / 'git-cache')}, output)
                output.flush()
                os.fsync(output.fileno())
            os._exit(73)

    cache = folder / 'git-cache'
    if not cache.exists():
        cache.mkdir()
        RawGit(cache).run('init', '--bare', '-q')

    class MeasuredRepository(Repository):
        def _execute(self, args, input_data=None):
            calls.append(list(args))
            return super()._execute(args, input_data)

        def read_commit(self, value):
            result = super().read_commit(value)
            scanned.add(value)
            return result

        def fetch(self, url, ref, *, depth=2048, object_format='sha1'):
            # Test-only transport seam: exact local public fixture, never arbitrary configured URLs.
            upstream = RawGit(fixture['path'])
            tip = upstream.text('rev-parse', '--verify', ref + '^{commit}')
            calls.extend([command['argv'] for command in upstream.commands])
            import shlex
            upload = shlex.join(['git', '-c', 'safe.directory=' + fixture['path'],
                                 '-c', 'safe.directory=' + fixture['path'] + '/.git', 'upload-pack'])
            transport = RawGit(self.path)
            transport.run('-c', 'protocol.file.allow=always', 'fetch', '--quiet',
                          '--no-tags', '--no-write-fetch-head', '--upload-pack=' + upload, fixture['path'], tip)
            calls.extend([command['argv'] for command in transport.commands])
            self.run('update-ref', 'refs/lookout/fetched', tip)
            crash('objects_acquired')
            return tip

    repo = MeasuredRepository(cache, timeout=90)
    raw = RawGit(fixture['path'])
    original_build = collector.build_batch

    def build(*args, **kwargs):
        result = original_build(*args, **kwargs)
        crash('batch_computed')
        return result

    collector.build_batch = build
    original_commit = state.commit

    def commit(*args, **kwargs):
        crash('before_publish')
        result = original_commit(*args, **kwargs)
        crash('after_publish')
        return result

    state.commit = commit
    advance = collector.advance
    collector.advance = lambda repo, policy, prior, records, at, budget, **kw: advance(
        repo, policy, prior, records, at, 1000 if req.get('full') else budget, fetch=True)
    cold = req.get('cache') == 'cold'

    def run(tip, *, warm=False):
        nonlocal repo
        raw.run('update-ref', 'refs/heads/acceptance', tip)
        if cold and not warm:
            repo = MeasuredRepository(cache, timeout=90)
        rounds = 0
        while True:
            result = collector.collect(config, db, repositories={'fixture': repo}, now=FIXED_AT)
            rounds += 1
            errors = {key: value for key, value in result.get('components', {}).items()
                      if key.startswith('commits:') and value.get('error')}
            if errors:
                raise RuntimeError(f'collector errors: {errors}')
            active = result.get('commits_batches', {}).get('binutils', {}).get('active', {})
            if active.get('status') == 'ok' and result['commits_scans']['fixture']['active'].get('complete'):
                verify_fixture(result, fixture, tip, len(config['packages']['binutils']['monitors']['commits']['rules']) > 1)
                return result, rounds
            if rounds > 50:
                raise RuntimeError(f'collector did not converge: {active}')

    scenario = req.get('scenario', 'first_scan')
    if req.get('benchmark'):
        if scenario in ('unchanged_poll', 'append_commit', 'rule_reevaluation'):
            run(fixture['initial_tip'], warm=True)
        if scenario == 'rule_reevaluation':
            config, _ = config_snapshot(fixture, True)
        if req.get('cache') == 'hot' and scenario == 'first_scan':
            run(fixture['initial_tip'], warm=True)
            db.unlink()
            state.commit(db, initial)
        setup_git_bytes = disk_bytes(cache)
        calls.clear()
        scanned.clear()
        start_disk = disk_bytes(folder)
        started = time.perf_counter()
        result, rounds = run(fixture['append_tip'] if scenario == 'append_commit' else fixture['initial_tip'])
        elapsed = time.perf_counter() - started
        measured = {'seconds': elapsed, 'git_calls': len(calls),
                    'git_commands': calls, 'unique_commit_reads': len(scanned),
                    'scanned_objects': len(result['commits_records']['fixture']),
                    'rss_self_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    'rss_children_kib': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                    'disk_delta_bytes': disk_bytes(folder) - start_disk,
                    'git_cache_delta_bytes': disk_bytes(cache) - setup_git_bytes,
                    'rounds_to_converge': rounds}
    else:
        if req.get('evolution'):
            run(fixture['initial_tip'], warm=True)
        result, rounds = run(fixture['append_tip'])
        measured = {'rounds_to_converge': rounds, 'git_calls': len(calls)}
    verified = verify_fixture(result, fixture,
        fixture['append_tip'] if not req.get('benchmark') or scenario == 'append_commit' else fixture['initial_tip'],
        len(config['packages']['binutils']['monitors']['commits']['rules']) > 1)
    write_new(folder / 'result.json', {'pid': os.getpid(), 'clock': FIXED_AT, 'metrics': measured,
        'independent_expected': verified,
        'projection': projection(result), 'projection_sha256': sha256(json.dumps(projection(result), sort_keys=True).encode())})
    return 0


def spawn(folder, fixture, **options):
    folder.mkdir()
    request = {'folder': str(folder), 'fixture': fixture, **options}
    write_new(folder / 'request.json', request)
    return invoke(folder)


def invoke(folder):
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(folder / 'request.json')]
    started = time.perf_counter()
    with (folder / ('process-' + str(len(list(folder.glob('process-*.log')))) + '.log')).open('w') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=240)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
    result_path = folder / 'result.json'
    return {'pid': process.pid, 'returncode': code, 'command': command,
            'seconds': time.perf_counter() - started,
            'result': json.loads(result_path.read_text()) if result_path.exists() else None}


def recovery(root, fixture):
    control = spawn(root / 'full-control', fixture, full=True)
    incremental = spawn(root / 'incremental', fixture)
    appended = spawn(root / 'append-incremental', fixture, evolution=True)
    reports = []
    expected = control['result']['projection'] if control['result'] else None
    for stage in ('objects_acquired', 'batch_computed', 'before_publish', 'after_publish'):
        folder = root / ('recovery-' + stage)
        failed = spawn(folder, fixture, fault=stage)
        marker = json.loads((folder / 'fault.json').read_text()) if (folder / 'fault.json').exists() else None
        import sqlite3

        from tracker import state
        with sqlite3.connect(folder / 'state.db') as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchall()
        before_restart = projection(state.read(folder / 'state.db'))
        req = json.loads((folder / 'request.json').read_text())
        shutil.copyfile(folder / 'request.json', folder / 'fault-request.json')
        req.pop('fault')
        (folder / 'restart-request.json').write_text(json.dumps(req, indent=2) + '\n')
        shutil.copyfile(folder / 'restart-request.json', folder / 'request.json')
        resumed = invoke(folder)
        actual = resumed['result']['projection'] if resumed['result'] else None
        reports.append({'stage': stage, 'fault_process': failed, 'fault_marker': marker, 'restart_process': resumed,
                        'equal_to_full': actual == expected and expected is not None,
                        'different_pids': failed['pid'] != resumed['pid'],
                        'pre_restart_integrity': integrity,
                        'pre_restart_projection': before_restart,
                        'passed': integrity == [('ok',)] and failed['returncode'] == 73 and resumed['returncode'] == 0
                                  and failed['pid'] != resumed['pid'] and actual == expected and expected is not None})
    incremental_equal = incremental['result'] is not None and incremental['result']['projection'] == expected
    append_equal = appended['result'] is not None and appended['result']['projection'] == expected
    return {'full': control, 'incremental': incremental, 'incremental_equal_to_full': incremental_equal,
            'append_incremental': appended, 'append_equal_to_full': append_equal,
            'comparison_fields': ['commits_records', 'commits_scans', 'commits_evidence', 'commits_batches'],
            'faults': reports, 'passed': incremental_equal and append_equal and all(r['passed'] for r in reports)}


def benchmark(root, fixture, repeats):
    results, summaries = [], []
    for scenario in ('first_scan', 'unchanged_poll', 'append_commit', 'rule_reevaluation'):
        for cache in ('cold', 'hot'):
            rows = []
            for index in range(repeats):
                row = spawn(root / f'perf-{scenario}-{cache}-{index}', fixture,
                            scenario=scenario, cache=cache, benchmark=True)
                row.update(scenario=scenario, cache=cache, repeat=index)
                rows.append(row)
                results.append(row)
            valid = [row['result']['metrics'] for row in rows if row['returncode'] == 0 and row['result']]
            summary = {'scenario': scenario, 'cache': cache, 'samples': len(rows),
                       'successes': len(valid), 'failures': len(rows) - len(valid)}
            for field in ('seconds', 'git_calls', 'unique_commit_reads', 'scanned_objects', 'rss_self_kib',
                          'rss_children_kib', 'disk_delta_bytes', 'git_cache_delta_bytes'):
                values = [row[field] for row in valid]
                summary[field] = {'median': statistics.median(values), 'min': min(values), 'max': max(values)} if values else None
            summaries.append(summary)
    return {'repeats': repeats, 'definition': {'cold': 'new Repository object before timed scenario; Python application caches empty',
            'hot': 'same Repository object warmed by untimed setup; every repetition has a fresh independent worker process',
            'os_cache': 'not dropped or controlled; no cold-disk claim',
            'rss': 'Linux ru_maxrss KiB, separate lifetime maxima for worker and its children including untimed warmup (not simultaneous sum)',
            'disk': 'logical experiment directory bytes delta including local fetched Git objects and SQLite; fixed origin objects unchanged',
            'seams': ['fixed now', 'local exact-object Git fetch transport injection, no HTTPS', 'fixed config reload check bypass',
                      'full comparison overrides only scan processing budget, not config identities'],
            'git_calls': 'collector subprocess calls including test-only local fetch; fixture ref setup excluded',
            'repetition_unit': 'one complete scenario to convergence; convergence rounds are not independent samples'},
            'summary': summaries, 'runs': results,
            'passed': all(row['successes'] >= 5 and row['failures'] == 0 for row in summaries)}


def labels(cache_root, manifest):
    from tracker.monitors.commits.git import Repository
    from tracker.monitors.commits.model import Rule
    from tracker.monitors.commits.rules import match_rules
    labels = []
    for name, policy in manifest['repositories'].items():
        case = next(c for c in manifest['cases'] if c['repository'] == name)
        raw, repository = RawGit(cache_root / policy['cache']), Repository(cache_root / policy['cache'], timeout=90)
        ids = raw.text('rev-list', '--reverse', '--topo-order', case['target'], '^' + case['start'], '--').splitlines()
        rules = [Rule(id='riscv-text', keywords=['riscv', 'risc-v']),
                 Rule(id='riscv-path', paths=['target/riscv/**', 'llvm/lib/Target/RISCV/**',
                                             'llvm/test/CodeGen/RISCV/**'])]
        rows = []
        for oid in ids:
            header, _, message = raw.run('cat-file', 'commit', oid)[0].partition(b'\n\n')
            parents = [line[7:].decode() for line in header.splitlines() if line.startswith(b'parent ')]
            paths = set()
            for parent in parents or [None]:
                args = [parent, oid] if parent else ['--root', oid]
                paths.update(p for p in raw.run('diff-tree', '-r', '--no-commit-id', '--name-only', '-z',
                                              '--no-renames', *args, '--')[0].split(b'\0') if p)
            # Independent finite scope, not the configured glob/keyword matcher implementation.
            prefixes = (b'target/riscv/', b'llvm/lib/Target/RISCV/', b'llvm/test/CodeGen/RISCV/')
            path_label = any(path.startswith(prefixes) for path in paths)
            text_label = b'riscv' in message.lower() or b'risc-v' in message.lower()
            technical = path_label or text_label
            matched = match_rules(repository.read_commit(oid), rules)
            rows.append({'oid': oid, 'subject': message.decode('utf-8', 'replace').splitlines()[0],
                         'paths': [base64.b64encode(p).decode() for p in sorted(paths)],
                         'message_sha256': sha256(message), 'technical_path_label': path_label,
                         'technical_message_label': text_label, 'technical_scope_label': technical,
                         'machine_prediction': bool(matched), 'machine_reasons': matched,
                         'human_relevance': None, 'review_status': 'pending_human_review'})
        tp = sum(row['technical_scope_label'] and row['machine_prediction'] for row in rows)
        fp = sum(not row['technical_scope_label'] and row['machine_prediction'] for row in rows)
        fn = sum(row['technical_scope_label'] and not row['machine_prediction'] for row in rows)
        tn = sum(not row['technical_scope_label'] and not row['machine_prediction'] for row in rows)
        labels.append({'repository': name, 'url': policy['url'], 'start': case['start'], 'target': case['target'],
            'complete_window_count': len(ids), 'rules': [rule.model_dump() for rule in rules],
            'rules_sha256': sha256(json.dumps([rule.model_dump() for rule in rules], sort_keys=True).encode()),
            'technical_evaluation': {'definition': 'case-sensitive declared directory prefixes OR case-insensitive message substring riscv/risc-v',
                'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn, 'denominator': len(rows),
                'precision': tp / (tp + fp) if tp + fp else None, 'recall': tp / (tp + fn) if tp + fn else None,
                'limitation': 'reproducible lexical/path scope ONLY; not semantic project relevance and not human-reviewed truth'},
            'semantic_evaluation': {'tp': None, 'fp': None, 'fn': None, 'denominator': len(rows),
                'reviewed': 0, 'pending': len(rows), 'precision': None, 'recall': None},
            'rows': rows, 'raw_git_commands': raw.commands})
    return labels


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--work-root', type=Path)
    parser.add_argument('--cache-root', type=Path)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'backend/tests/fixtures/commits-real-cases.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--only', choices=('all', 'recovery', 'performance', 'labels'), default='all')
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker)
    if not args.work_root or not args.output:
        parser.error('--work-root and --output are required')
    if args.repeats < 5:
        parser.error('at least five independent repetitions per scenario/cache pair are required')
    if args.output.exists() or args.work_root.exists():
        parser.error('work-root and output must be new paths')
    if args.only in ('all', 'labels') and not args.cache_root:
        parser.error('--cache-root is required for complete real-window labels')
    limits()
    args.work_root.mkdir(parents=True)
    before = fingerprint(ROOT)
    from tracker.monitors.commits.model import METHOD_VERSION
    report = {'schema': RUNNER_VERSION, 'method_version': METHOD_VERSION, 'source': before,
              'environment': environment(), 'fixed_clock': FIXED_AT,
              'manifest_sha256': sha256(args.manifest.read_bytes()),
              'rules_version': 'finite-independent-path-message-scope-v2',
              'limits': {'cpu_affinity_count': 2, 'address_space_bytes': 4 * 1024 ** 3,
                         'nice': os.getpriority(os.PRIO_PROCESS, 0)},
              'scope': 'synthetic offline collector recovery/performance and complete bounded real technical labels'}
    if args.only != 'labels':
        fixture = fixed_fixture(args.work_root / 'fixed-upstream')
        report['fixture'] = fixture
        if args.only in ('all', 'recovery'):
            report['recovery'] = recovery(args.work_root, fixture)
        if args.only in ('all', 'performance'):
            report['performance'] = benchmark(args.work_root, fixture, args.repeats)
    if args.only in ('all', 'labels'):
        report['labels'] = labels(args.cache_root, json.loads(args.manifest.read_text()))
    report['source_after'] = fingerprint(ROOT)
    report['source_unchanged'] = before == report['source_after']
    label_checks = []
    for group in report.get('labels', []):
        rows, metrics = group['rows'], group['technical_evaluation']
        expected_counts = {
            'tp': sum(row['technical_scope_label'] and row['machine_prediction'] for row in rows),
            'fp': sum(not row['technical_scope_label'] and row['machine_prediction'] for row in rows),
            'fn': sum(row['technical_scope_label'] and not row['machine_prediction'] for row in rows),
            'tn': sum(not row['technical_scope_label'] and not row['machine_prediction'] for row in rows)}
        label_checks.append(bool(len(rows) == group['complete_window_count'] == metrics['denominator']
            and all(metrics[key] == value for key, value in expected_counts.items())
            and sum(expected_counts.values()) == len(rows)
            and expected_counts['fp'] == expected_counts['fn'] == 0))
    report['labels_consistent'] = all(label_checks)
    report['passed'] = report['source_unchanged'] and report['labels_consistent'] and all(
        report[key]['passed'] for key in ('recovery', 'performance') if key in report)
    write_new(args.output, report)
    print(json.dumps({'report': str(args.output), 'passed': report['passed'],
                      'source_unchanged': report['source_unchanged']}, sort_keys=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
