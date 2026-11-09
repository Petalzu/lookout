// Render the production SSR bundle against deterministic API observations.
import assert from 'node:assert/strict';
import './http-cache.cjs';
import './redocly.cjs';
import './render-cache.mjs';
import {noteRows} from '../src/lib/document.ts';
import {spawn, spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {createServer, request} from 'node:http';
import {gunzipSync, brotliDecompressSync} from 'node:zlib';

const note = (column, span, label) => ({column, span, values: [{text: label}]});
const notes = [note(0, 3, 'evidence'), note(0, 3, 'more evidence'),
  note(3, 1, 'rva23 reason'), note(5, 1, 'x86 reason')];
const packed = noteRows(notes, 6);
assert.deepEqual(packed.map(row => row.map(cell => [cell.column, cell.span])),
  [[[0, 3], [3, 1], [4, 1], [5, 1]], [[0, 3], [3, 3]]]);
assert.deepEqual(packed.flat().flatMap(cell => cell.values).map(value => value.text),
  ['evidence', 'rva23 reason', 'x86 reason', 'more evidence']);
assert.deepEqual(noteRows([], 6), []);
assert.deepEqual(notes.map(cell => cell.values[0].text),
  ['evidence', 'more evidence', 'rva23 reason', 'x86 reason']);

const serving = process.argv.includes('--serve');
let delays = {};
let revision = 0;

const filtered = (conditions, parameters = {}) => '/?' + new URLSearchParams([
  ...Object.entries(parameters), ...conditions.flat().map(([dimension, value]) => ['select', `${dimension}=${value}`]),
]);
const targets = ['rva23', 'rva20', 'x86_64'].map(id => ({id, label: id, repository: id, architecture: 'riscv64'}));
const makePackage = (name, patch = {}) => ({
  buildsystem: null, buildsystem_status: 'not_declared', maintenance: [], maintenance_findings: [], monitor_checks: [], presentation: {buildsystems: {}},
  name, current: '2.0', latest: '2.1', relation: 'outdated', track: name, track_label: name,
  current_build_success: true, last_successful_version: '2.0', stale: false,
  needs_attention: false, upstream_updated_at: '2026-09-19T10:50:00Z', detail_url: `/packages/${name}`, version_error: null,
  builds: targets.map(target => ({target: target.id, label: target.label, repository: target.repository,
    architecture: target.architecture, raw_status: 'succeeded', text: '✓', kind: 'ok', issue: false,
    log_url: null, stale: false, updated_at: '2026-09-19T11:10:00Z', matches_source: true,
    last_success: {version: '2.0', time: '2026-09-19T11:00:00+00:00', srcmd5: 'a'}, flavors: []})),
  spec: {source_path: `SPECS/${name}`, source_url: `https://gitlab.example.org/team/packaging/-/tree/review/SPECS/${name}`, metadata: {summary: 'Fixture package', url: 'https://example.org/upstream'}, changelog: [
    {commit: 'abcdef0123456789', author: 'Fixture author', date: '2026-09-19T09:00:00Z', subject: 'Fixture change', signed_off_by: []},
  ], error: null},
  ...patch,
});
const packages = [
  makePackage('security', {version_annotations: [{monitor:'security',label:'Advisory',count:8,scope:'current',target_version:null,stale:false,finding_ids:[]}], maintenance_findings: [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({
    monitor: 'security', id: `CVE-2026-100${i}`, label: 'Advisory', title: `CVE-2026-100${i}`,
    facts: [{key: 'Observed version', code: 'query', value: '2.0', source: 'OSV', url: 'https://api.osv.dev/v1/query', status: 'observed'},
      {key: 'Exploit probability', code: 'epss_probability', value: 0.00396, source: 'FIRST', url: 'https://api.first.org/data/v1/epss', status: 'observed'},
      {key: 'Reported fixes', code: 'fixed_events', value: ['3.0'], source: 'OSV', url: 'https://osv.dev/vulnerability/fixture', status: 'observed'},
      {key: 'KEV', value: i === 1 ? false : null, source: 'CISA', url: 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog', status: i === 1 ? 'observed' : 'unavailable'}],
    evidence_url: `https://nvd.nist.gov/vuln/detail/CVE-2026-100${i}`,
    scope: 'current', tags: [], stale: false,
  }))}),
  makePackage('license-evidence', {version_annotations: [{monitor:'license',label:'LicenseDiff',count:1,scope:'upgrade',target_version:'2.1',stale:false,finding_ids:[]}], maintenance_findings: [{
    monitor: 'license', id: 'license-target', label: 'LicenseDiff', title: 'MIT → Apache-2.0',
    evidence_url: 'https://example.org/license', scope: 'upgrade', target_version: '2.1',
    tags: [], stale: false, facts: [
      {key: 'Licenses', value: ['MIT', 'Apache-2.0'], source: 'Registry', url: 'https://example.org/license', status: 'observed'},
      {key: 'Unavailable field', value: null, source: 'Registry', url: 'https://example.org/license', status: 'unavailable'},
      {key: 'False field', value: false, source: 'Registry', url: 'https://example.org/license', status: 'observed'},
    ],
  }]}),
  makePackage('success', {buildsystem: 'custom', buildsystem_status: 'declared', maintenance: [{label:'NewSignal', count:2, stale:false}],
    activity: [{label:'PR', count:2, stale:false}, {label:'Issue', count:1, stale:false}],
    spec: {...makePackage('success').spec, metadata: {summary: 'Fixture package',
      url: 'https://example.org/upstream', license: 'MIT', description: 'Fixture source description'}}}),
  makePackage('watch-preview', {watch: [
    {id:'widget@preview',version:'2.2rc1',error:null,stale:false},
    {id:'widget@nightly',version:'2.3dev1',error:'fetch failed',stale:false},
    {id:'widget@missing',error:'no result',stale:true},
  ]}),
  makePackage('ahead', {relation: 'ahead'}),
  makePackage('check-error', {checks: {version: {status: 'error', error: 'Fixture provider timeout'}}}),
  ...[false, null].map(matches_source => makePackage(matches_source === false ? 'old-source' : 'unknown-source', {
    builds: makePackage('base').builds.map(build => ({...build, matches_source})),
  })),
  makePackage('arch-version', {builds: makePackage('base').builds.map(build => ({...build, last_success: {...build.last_success, version: '2.0.arch'}}))}),
  makePackage('failed-same-version', {builds: makePackage('base').builds.map(build => ({...build, kind: 'error', issue: true, raw_status: 'failed', text: 'Failed', matches_source: false}))}),
  makePackage('long-version', {current: '0.7+git20231216.05e79eb', latest: null, relation: 'untracked', track: null, builds: makePackage('base').builds.map(build => ({...build, last_success: {...build.last_success, version: '0.7+git20231216.05e79eb'}}))}),
  makePackage('failed', {current_build_success: false, last_successful_version: '9.9-shared-should-not-render', stale: true, builds: makePackage('base').builds.map((build, i) => ({...build, text: 'Failed', kind: 'error', issue: true, raw_status: 'failed', stale: true, matches_source: false, last_success: {version: ['1.9', '1.8', '1.7'][i], time: '2026-09-19T11:00:00Z', srcmd5: String(i)}}))}),
  makePackage('untracked', {relation: 'untracked', track: null, current_build_success: false}),
  makePackage('untracked-unavailable-source', {relation: 'unknown', track: null, current: null, current_build_success: null}),
  makePackage('unknown', {relation: 'unknown', current_build_success: null, last_successful_version: null}),
  makePackage('unresolved-version', {builds: [{...makePackage('base').builds[0], last_success: {version: null, time: '2026-09-19T11:00:00Z', srcmd5: 'macro'}}]}),
  makePackage('multibuild', {builds: [{...makePackage('base').builds[0], last_success: null, flavors: [
    {package: 'multibuild:one', text: '✓', kind: 'ok', raw_status: 'succeeded', stale: false, log_url: null, updated_at: '2026-09-19T11:10:00Z', last_success: {version: '1.1', time: '2026-09-19T10:00:00Z', srcmd5: 'one'}},
    {package: 'multibuild:two', text: 'Failed', kind: 'error', issue: true, raw_status: 'failed', stale: false, log_url: null, updated_at: '2026-09-19T11:10:00Z', last_success: {version: '1.0', time: '2026-09-18T10:00:00Z', srcmd5: 'two'}},
  ]}]}),
  makePackage('missing-history', {current_build_success: null, last_successful_version: null,
    builds: targets.map(target => ({target: target.id, label: target.label, repository: target.repository,
      architecture: target.architecture, raw_status: 'unknown', text: 'No result', kind: 'muted',
      log_url: null, stale: false, updated_at: null, matches_source: null, last_success: null, flavors: []}))}),
];
packages.push(makePackage('advisory-current', {latest: '2.0', relation: 'current',
  maintenance_findings: [packages[0].maintenance_findings[0]],
}));
// The fixture keeps concise package variants above; this is its only wire mapping.
// The fixture builds domain inputs; pages receive only reading documents.
const catalog = [
  {id: 'source', title: 'Source', kind: 'source'},
  {id: 'version', title: 'Version', kind: 'version'},
  {id: 'build', title: 'Build', kind: 'build'},
  {id: 'security', title: 'Advisory', kind: 'evidence'},
  {id: 'license', title: 'LicenseDiff', kind: 'evidence'},
  {id: 'yanked', title: 'Release files', kind: 'evidence'},
  {id: 'requires', title: 'Dependencies', kind: 'requires'},
  {id: 'eol', title: 'EOL', kind: 'evidence'},
  {id: 'github', title: 'GitHub', kind: 'activity'},
];
// Synthetic runtime assessments, not live registry versions or host evaluation.
const constraint = (expression, release) => ({expression, source: 'Fixture registry',
  url: `https://example.org/fixture/${release}/requirements`});
const observedDependency = version => ({version, revision: 'fixture-source', origin: 'spec',
  checked_at: '2026-09-19T11:00:00Z', stale: false});
const runtimeRequirement = (dependency, patch = {}) => ({dependency, name: dependency, kind: 'runtime',
  scheme: 'pep440', identity: null, condition: null, extras: [], optional: false, package: dependency, mapping: 'mapped',
  current: constraint('>=1.0', 'current'), target: constraint('>=2.0', 'target'),
  observed: observedDependency('2.1'), satisfaction: 'satisfied', reason: null,
  target_satisfaction: 'satisfied', target_reason: null, changed: true, ...patch});
packages.push(makePackage('requires-upgrade', {requirements: [
  runtimeRequirement('python', {name: 'Python', current: constraint('>=3.8', 'current'),
    target: constraint('>=3.10', 'target'), observed: observedDependency('3.11.8')}),
  runtimeRequirement('libwidget', {scheme: 'rpm_version', observed: observedDependency('0.9'),
    satisfaction: 'unsatisfied', target_satisfaction: 'unsatisfied'}),
  runtimeRequirement('optional-library', {name: 'Optional library', optional: true,
    condition: '(platform_python_implementation == "CPython" and sys_platform != "android") and extra == "speedups"',
    satisfaction: 'unknown', reason: 'condition_not_evaluated',
    target_satisfaction: 'unknown', target_reason: 'condition_not_evaluated'}),
  runtimeRequirement('libexample', {scheme: 'numeric_minimum', current: constraint('1.80', 'current'),
    target: constraint('1.82', 'target'), observed: observedDependency('1.81.0'), target_satisfaction: 'unsatisfied'}),
]}));
packages.push(makePackage('requires-current', {latest: '2.0', relation: 'current', requirements: [
  runtimeRequirement('libcurrent', {target: null, target_satisfaction: 'unknown',
    target_reason: 'requirement_not_observed', changed: false}),
]}));
packages.push(makePackage('requires-target-only', {requirements: [
  runtimeRequirement('libtarget', {current: null, observed: observedDependency('1.9'),
    satisfaction: 'unknown', reason: 'requirement_not_observed', target_satisfaction: 'unsatisfied', changed: false}),
]}));
packages.push(makePackage('build-reason', {builds: makePackage('base').builds.map((b, index) => ({...b,
  raw_status: index === 0 ? 'unresolvable' : 'building', text: index === 0 ? 'Unresolvable' : 'Building',
  issue: index === 0, kind: 'pending', details: index === 0 ? 'nothing provides <fixture-dependency>' : 'worker://internal-host'}))}));
packages.push(makePackage('yanked', {
  maintenance: [{label: 'Withdrawn', count: 1, stale: false}],
  maintenance_findings: [{monitor: 'yanked', id: 'withdrawn', label: 'Withdrawn', title: 'Release withdrawn',
    scope: 'current', target_version: null, tags: [], stale: false, evidence_url: 'https://example.org/release',
    facts: [{key: 'Files', value: 3, source: 'Registry', url: 'https://example.org/release', status: 'observed'}]}],
}));
function monitored(pkg, detail = true, focus = '') {
  const check = {status: 'ok', stale: false, checked_at: '2026-09-19T11:10:00Z', attempted_at: null,
    error: null, note: null, changed_at: null, evidence_revision: null};
  const data = {
    github: {kind: 'activity', labels: pkg.activity || []},
    source: {kind: 'source', version: pkg.current, revision: 'fixture', buildsystem: pkg.buildsystem,
      buildsystem_status: pkg.buildsystem_status, ...pkg.spec, obs: {}},
    version: {kind: 'version', current: pkg.current, latest: pkg.latest, relation: pkg.relation, track: pkg.track,
      annotations: pkg.version_annotations || [], track_label: pkg.track_label, stale: pkg.stale, error: pkg.version_error, last_known_relation: pkg.relation,
      updated_at: pkg.upstream_updated_at, upstream: {}, watch: pkg.watch || []},
    build: {kind: 'build', targets: targets.map(t => pkg.builds.find(b => b.target === t.id) || {...makePackage('base').builds.find(b => b.target === t.id), text: 'No result', raw_status: 'unknown', kind: 'muted', matches_source: null, last_success: null}), source_version: pkg.current,
      source_success: pkg.current_build_success, last_successful_version: pkg.last_successful_version},
    requires: {kind: 'requires', current_version: pkg.current, target_version: pkg.latest,
      requirements: pkg.requirements || [], findings: [], labels: [], finding_count: (pkg.requirements || []).length},
  };
  const monitors = Object.fromEntries(catalog.map(m => [m.id, {id: m.id, title: m.title, check: {...check},
    data: data[m.id] || {kind: 'evidence', labels: m.id === 'yanked' ? pkg.maintenance : [],
      findings: pkg.maintenance_findings.filter(f => f.monitor === m.id)}}]));
  monitors.version.dimensions = {view: !pkg.track ? ['untracked'] : ['outdated', 'changed'].includes(pkg.relation) ? ['updates'] : []};
  monitors.version.dimensions.maintenance = monitors.version.dimensions.view.map(value =>
    ({untracked: 'Untracked', updates: 'Outdated'})[value]);
  for (const [id, failure] of Object.entries(pkg.checks || {})) Object.assign(monitors[id].check, failure);
  for (const result of Object.values(monitors)) {
    if (result.data.kind !== 'evidence') continue;
    const findings = result.data.findings;
    const labels = new Map();
    for (const finding of findings) {
      for (const label of new Set([finding.label, ...finding.tags])) {
        const previous = labels.get(label) || {label, count: 0, stale: false};
        labels.set(label, {...previous, count: previous.count + 1, stale: previous.stale || finding.stale});
      }
    }
    if (findings.length) result.data.labels = [...labels.values()];
    result.data.finding_count = findings.length;
    result.data.entries = result.id === focus ? findings.map(({id, title, evidence_url, stale, scope, target_version, tags}) =>
      ({id, title, evidence_url, stale, scope, target_version, tags})) : [];
  }
  monitors.build.check.checked_at = pkg.builds.every(b => b.updated_at) ? pkg.builds[0].updated_at : null;
  if (!detail) {
    for (const result of Object.values(monitors)) {
      for (const key of ['findings', 'metadata', 'changelog', 'obs', 'upstream', 'watch']) delete result.data[key];
    }
  }
  return {name: pkg.name, detail_url: pkg.detail_url, monitors, presentation: pkg.presentation};
}
const fixtureBridge = fileURLToPath(new URL('../../backend/tests/presentation_fixture.py', import.meta.url));
function project(operation, payload, query = {}) {
  const result = spawnSync(process.env.PYTHON || 'python3', [fixtureBridge], {
    input: JSON.stringify({operation, payload, query}), encoding: 'utf8',
    // Bounded queries repeat their URLs in links, exceeding spawnSync's 1 MiB default.
    maxBuffer: 16 * 1024 * 1024,
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}
let unavailable = false;
let health = 'ok';
let errorStreamClosed;
let ready = {status: 200, body: {status: 'degraded', generation: 1}};
let appearancePalette = {custom: {background: '#123456', foreground: '#ffffff', icon: 'gopher'}};
const publicPaths = [
  '/api/v2/packages', '/api/v2/packages/security', '/api/v2/packages/security/activity', '/api/v2/packages/security/commits', '/api/v2/tracks/widget',
  '/api/v2/targets', '/api/v2/status', '/api/v2/export', '/api/v2/packages:batchGet',
];
const forwardedRequests = [];
const mock = createServer((req, res) => {
  if (serving && req.url === '/_fixture' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      const change = JSON.parse(body);
      if ('unavailable' in change) unavailable = change.unavailable;
      if ('delays' in change) delays = change.delays;
      if ('current' in change) packages[0].current = change.current;
      if ('revision' in change) revision = change.revision;
      if (change.add) packages.push(makePackage(change.add));
      res.writeHead(200, {'Content-Type': 'application/json'});
      res.end(JSON.stringify({packages: packages.length, revision}));
    });
    return;
  }
  forwardedRequests.push(req.url);
  if (unavailable) { res.writeHead(503, {'Content-Type':'application/json'});res.end('{}');return; }
  const url = new URL(req.url, 'http://localhost');
  if (publicPaths.includes(url.pathname) && url.searchParams.has('proxy_fixture')) {
    const status = Number(url.searchParams.get('fixture_status')) || 200;
    const headers = {'Content-Type': 'application/json'};
    if (url.pathname === '/api/v2/export') headers['Content-Disposition'] = 'attachment; filename="fixture.json"';
    res.writeHead(status, headers);
    res.end(JSON.stringify({path: url.pathname, query: [...url.searchParams]})); return;
  }
  if (url.pathname === '/healthz') {
    if (health === 'stream-error') {
      errorStreamClosed = once(res, 'close');
      res.writeHead(503, {'Content-Type': 'application/json'});
      res.write('{');
      return;
    }
    if (health === 'stall') return;
    if (health === 'disconnected') { req.socket.destroy(); return; }
    res.writeHead(health === 'error' ? 503 : 200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({status: health === 'ok' ? 'ok' : 'not-ok'})); return;
  }
  if (url.pathname === '/readyz') {
    res.writeHead(ready.status, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(ready.body)); return;
  }
  if (url.pathname === '/api/ui/packages' && url.searchParams.get('monitor') === 'not-registered') {
    res.writeHead(422, {'Content-Type': 'application/json'}); res.end('{}'); return;
  }
  if (['/api/ui/packages/security/commits', '/api/ui/packages/commits-first-failure/commits'].includes(url.pathname)) {
    const initialFailure = url.pathname.includes('commits-first-failure');
    const cursor = url.searchParams.get('cursor');
    const side = {status: 'original_reachable', reason: null, matches: ['a'.repeat(40)], reverts: [],
      strict_manifest: null, checked_at: '2026-01-01T00:00:00Z', coverage: {complete: true},
      target: {oid: 'b'.repeat(40), version: '1.0'}};
    const check = {configured: true, status: initialFailure ? 'error' : 'ok', stale: initialFailure,
      checked_at: initialFailure ? null : '2026-01-01T00:00:00Z', attempted_at: '2026-01-02T00:00:00Z',
      error: initialFailure ? 'timeout <script>first collection</script>' : null, failures: []};
    const document = project('commits', {name: initialFailure ? 'commits-first-failure' : 'security',
      status: url.searchParams.get('status'), rule: url.searchParams.get('rule'),
      page: {check, repository: 'fixture', batch_id: initialFailure ? null : 'fixture-batch',
      coverage: initialFailure ? {} : {complete: true}, total: initialFailure ? 0 : 2,
      next_cursor: initialFailure || cursor ? null : 'next-batch-page',
      items: initialFailure ? [] : [{oid: (cursor ? 'c' : 'a').repeat(40), subject: '<script>commit fixture</script>',
        url: 'https://example.org/upstream/commit/' + 'a'.repeat(40),
        reasons: [{rule: 'riscv-path', field: 'path', fragment: 'target/riscv/cpu.c', path: 'target/riscv/cpu.c'}],
        sides: {current: side, upgrade: {status: 'unknown', reason: 'no_candidate_release', matches: [],
          reverts: [], strict_manifest: null, coverage: {}, checked_at: null, target: null}}}],
    }});
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(document)); return;
  }
  if (url.pathname === '/api/ui/packages/security/activity') {
    const cursor = url.searchParams.get('cursor');
    const document = project('activity', {name: 'security', kind: 'pr', page: {
      total: 2, next_cursor: cursor ? null : 'second',
      items: [{id: cursor ? '2' : '1', repository: 'owner/repo', number: cursor ? 2 : 1,
        author: 'contributor', kind: 'pr', status: cursor ? 'merged' : 'draft', title: '<script>fixture title</script>',
        url: 'https://github.com/owner/repo/pull/1', updated_at: '2026-01-01T00:00:00Z',
        paths_complete: true, available: true, association: []}],
    }});
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(document)); return;
  }
  const selected = packages.find(pkg => url.pathname === `/api/ui/packages/${pkg.name}`);
  const focus = url.searchParams.get('monitor') || '';
  const section = url.searchParams.get('section') || 'results';
  const payload = url.pathname === '/api/ui/theme'
    ? {buildsystems: appearancePalette}
    : selected ? monitored(selected) : {
      monitors: catalog, section, presentation: {buildsystems: appearancePalette},
      items: packages.map(pkg => monitored(pkg, true, focus)),
      targets,
      collection: {build_service_url: 'https://build.example.org', source_repository: {url: 'https://github.com/fixture/repo.git', branch: 'stable/3', revision: 'abcdef0123456789abcdef0123456789abcdef01'}, obs_updated_at: '2026-09-19T11:10:00Z', upstream_updated_at: '2026-09-19T10:50:00Z', last_attempt: null, mode: 'live', errors: ['intentional fixture error'], generation: 1,
        packages: packages.length, tracked_packages: packages.length - 1}};
  const query = [...url.searchParams];
  const document = project(url.pathname === '/api/ui/theme' ? 'theme' : selected ? 'detail' : 'list', payload, query);
  const response = JSON.stringify(url.pathname.startsWith('/api/ui/') ? document : payload);
  const send = () => { res.writeHead(200, {'Content-Type': 'application/json'}); res.end(response); };
  const delay = delays[url.searchParams.get('q')];
  if (Number.isInteger(delay) && delay > 0 && delay <= 2000) {
    const timer = setTimeout(send, delay);
    res.once('close', () => clearTimeout(timer));
  } else send();
});
mock.listen(serving ? 8099 : 0, '127.0.0.1'); await once(mock, 'listening');
const reserve = createServer(); reserve.listen(0, '127.0.0.1'); await once(reserve, 'listening');
const port = serving ? 8080 : reserve.address().port; await new Promise(resolve => reserve.close(resolve));
const child = spawn(process.execPath, ['server.mjs'], {env: {...process.env,
  HOST: '127.0.0.1', PORT: String(port), TRACKER_API_URL: `http://127.0.0.1:${mock.address().port}`}, stdio: ['ignore', 'pipe', 'pipe']});
let logs = ''; child.stdout.on('data', chunk => logs += chunk); child.stderr.on('data', chunk => logs += chunk);
async function read(path, cookie) {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, {headers: cookie ? {cookie} : {}});
  assert.equal(response.status, 200, `${path}: ${logs}`); return response.text();
}
const mainHTML = html => html.slice(html.indexOf('<main'), html.indexOf('</main>'));
const rowKeys = html => new Set([...mainHTML(html).matchAll(/<tr data-key="([^"]+)"/g)].map(([, key]) => key));
async function assertGlobalSearch(html, name, perPage = null) {
  const form = html.match(/<form\b[^>]*role="search"[^]*?<\/form>/)?.[0];
  assert.ok(form, 'search form is present');
  assert.match(form, /method="get"/);
  const action = form.match(/\baction="([^"]+)"/)?.[1];
  assert.equal(action, '/');
  const parameters = new URLSearchParams([...form.matchAll(/<input\b([^>]*)>/g)].map(([, attributes]) => {
    const field = attributes.match(/\bname="([^"]+)"/)?.[1];
    assert.ok(field, 'search inputs have names');
    return [field, attributes.match(/\bvalue="([^"]*)"/)?.[1] || ''];
  }));
  assert.deepEqual([...parameters.keys()].sort(), perPage ? ['per_page', 'q'] : ['q']);
  assert.equal(parameters.get('per_page'), perPage);
  parameters.set('q', name);
  const result = await read(`${action}?${parameters}`);
  assert.deepEqual(rowKeys(result), new Set([name]));
}
function assertCollapsedChecksLast(html) {
  const details = [...html.matchAll(/<details\b([^>]*)>([^]*?)<\/details>/g)];
  assert.ok(details.every(([, attrs]) => /id="(?:checks|requires-conditions)"/.test(attrs)),
    'only check diagnostics and dependency condition programs are collapsible');
  const checks = details.find(([, attrs]) => /id="checks"/.test(attrs));
  const [whole, attributes, body] = checks;
  assert.doesNotMatch(attributes, /(?:^|\s)open(?:\s|=|$)/, 'Checks starts closed');
  assert.match(body, /^\s*<summary\b[^>]*>Checks<\/summary>/);
  assert.match(body, /Collection checks/);
  assert.match(html.slice(checks.index + whole.length), /^\s*<\/div>/, 'Checks is the final main section');
  const changelog = html.match(/<section\b[^>]*id="changelog"[^>]*>/);
  if (changelog) assert.ok(changelog.index < checks.index, 'Checks follows Changelog');
}
async function wire(path, headers = {}, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = request({host:'127.0.0.1',port,path,method,headers}, response => {
      const chunks=[];response.on('data', chunk=>chunks.push(chunk));response.on('end',()=>resolve({status:response.statusCode,headers:response.headers,body:Buffer.concat(chunks)}));response.on('error',reject);
    });req.on('error',reject);req.end();
  });
}
try {
  for (let retry = 0; retry < 100; retry++) {
    try { await read('/livez'); break; } catch (error) {
      if (retry === 99 || child.exitCode !== null) throw new Error(`SSR did not start: ${logs}`, {cause: error});
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  if (serving) {
    console.log(JSON.stringify({web: port, fixture: mock.address().port}));
    await once(process, 'SIGTERM');
  } else {
  async function probe(path, expected, body) {
    const result = await fetch(`http://127.0.0.1:${port}${path}`, {signal: AbortSignal.timeout(5000)});
    assert.equal(result.status, expected, path);
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await result.json(), body, path);
  }
  assert.equal((await wire('/healthz')).status, 404);
  ready = {status: 503, body: {status: 'unavailable'}};
  await probe('/livez', 200, {status: 'ok'});
  await probe('/readyz', 503, ready.body);
  ready = {status: 200, body: {status: 'degraded', generation: 1}};
  await probe('/readyz', 200, ready.body);
  for (const failure of ['error', 'not-ok', 'disconnected', 'stall']) {
    health = failure;
    await probe('/livez', 503, {status: 'unavailable'});
  }
  health = 'stream-error';
  await probe('/livez', 503, {status: 'unavailable'});
  // The rejected body must close before the helper's two-second request timeout.
  await Promise.race([errorStreamClosed, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('Rejected API response body was not cancelled')), 1000);
    timer.unref();
  })]);
  health = 'ok';
  unavailable = true;
  for (const path of ['/livez', '/readyz']) {
    const result = await fetch(`http://127.0.0.1:${port}${path}`);
    assert.equal(result.status, 503);
    assert.equal(result.headers.get('cache-control'), 'no-store');
  }
  unavailable = false;
  console.log('PASS health: liveness, readiness, degraded, no snapshot, failure, timeout, no-store');
  for (const path of publicPaths) {
    const query = '?proxy_fixture=1&build=rva23%3Afailed&build=rva20%3Ablocked&q=with%20space&names=first&names=second&include=version&include=security';
    const result = await wire(path + query);
    assert.equal(result.status, 200, path);
    assert.deepEqual(JSON.parse(result.body), {path, query: [...new URLSearchParams(query)]});
    if (path.endsWith('/export')) assert.equal(result.headers['content-disposition'], 'attachment; filename="fixture.json"');
  }
  for (const status of [404, 422, 503]) {
    const result = await wire('/api/v2/status?proxy_fixture=1&fixture_status=' + status);
    assert.equal(result.status, status, 'proxy preserves upstream status');
  }
  for (const path of ['/api/v1/status', '/api/v1/packages', '/api/v2/internal', '/api/v2/packages/security/extra']) {
    const count = forwardedRequests.length;
    assert.equal((await wire(path)).status, 404, path);
    assert.equal(forwardedRequests.length, count, 'rejected route must not contact backend');
  }
  console.log('PASS API proxy: v2 routes, query, status, export header, rejected paths');
  const apiReference = await read('/api');
  const article = apiReference.match(/<article\b[^>]*>([^]*?)<\/article>/)[1];
  assert.match(article, /href="\/openapi\.json"/);
  assert.match(article, /GET \/api\/v2/);
  assert.match(article, /href="\/api\/v2\/export"/);
  assert.match(article, /href="\/api\/v2\/packages\/bash"/);
  const examples = [...article.matchAll(/<pre\b[^>]*><code\b[^>]*><a\b[^>]*href="([^"]+)"/g)]
    .map(([, href]) => new URL(href.replaceAll('&amp;', '&'), 'http://localhost'));
  assert.equal(examples.length, 2);
  assert.equal(examples[0].pathname, '/api/v2/packages');
  assert.equal(examples[0].searchParams.get('search'), 'observations');
  assert.equal(examples[0].searchParams.get('detail'), 'full');
  assert.equal(examples[1].pathname, '/api/v2/packages:batchGet');
  const names = examples[1].searchParams.getAll('names');
  assert.equal(names.length, 2);
  assert.equal(new Set(names).size, names.length);
  assert.ok(names.every(Boolean));
  assert.deepEqual(examples[1].searchParams.getAll('include'), ['version', 'security']);
  const activityPage = await read('/packages/security/activity?kind=pr');
  assert.match(activityPage, /title="draft"/);
  assert.match(activityPage, />contributor<\/span>/);
  assert.doesNotMatch(activityPage, /<style(?:\s|>)/, 'activity styling must respect style-src self');
  assert.match(activityPage, /&lt;script&gt;fixture title&lt;\/script&gt;/);
  assert.doesNotMatch(activityPage, /<script>fixture title/);
  assert.match(activityPage, /class="document-icon"/);
  assert.match(activityPage, /data-entry-page/);
  assert.match(activityPage, /hx-swap="outerHTML"/);
  const more = activityPage.match(/class="entry-more" href="([^"]+)"/)[1].replaceAll('&amp;', '&');
  const older = await read(more);
  assert.match(older, /title="merged"/);
  assert.doesNotMatch(older, /class="entry-more"/);
  console.log('PASS activity: bounded history, state icons, escaped titles, progressive pagination');
  const commitsPage = await read('/packages/security/commits?status=original_reachable&rule=riscv-path');
  assert.match(commitsPage, /Original commit in target history/);
  assert.match(commitsPage, /No comparable newer release/);
  assert.match(commitsPage, /target\/riscv\/cpu.c/);
  assert.match(commitsPage, /&lt;script&gt;commit fixture&lt;\/script&gt;/);
  assert.doesNotMatch(commitsPage, /<script>commit fixture/);
  assert.match(commitsPage, />Collection<[^]*?>Ok<\/span>/);
  assert.match(commitsPage, />Configuration<[^]*?>Configured<\/span>/);
  assert.match(commitsPage, />Last attempt<[^]*?2026-01-02 00:00:00 UTC/);
  const commitMore = commitsPage.match(/class="entry-more" href="([^"]+)"/)[1].replaceAll('&amp;', '&');
  assert.match(commitMore, /status=original_reachable/);
  assert.match(commitMore, /rule=riscv-path/);
  const nextCommits = await read(commitMore);
  assert.doesNotMatch(nextCommits, /class="entry-more"/);
  const initialCommitFailure = await read('/packages/commits-first-failure/commits');
  const initialCommitSection = initialCommitFailure.match(/<section\b[^>]*id="commits"[^]*?<\/section>/)[0];
  assert.match(initialCommitSection, />Collection<[^]*?>Error<\/span>/);
  assert.match(initialCommitSection, />Configuration<[^]*?>Configured<\/span>/);
  assert.match(initialCommitSection, />Refresh error<[^]*?timeout &lt;script&gt;first collection&lt;\/script&gt;/);
  assert.match(initialCommitSection, />Last attempt<[^]*?2026-01-02 00:00:00 UTC/);
  assert.doesNotMatch(initialCommitSection, /<script>first collection|>Observed<\/dt>|class="entry-more"/);
  assert.doesNotMatch(initialCommitSection, /Original commit in target history|>Ok<\/span>|Complete declared window/);
  console.log('PASS commits: historical states, initial collection error, escaping and filter-preserving progressive pagination');


  console.log('PASS API reference: OpenAPI, endpoint table and query links');

  const invalidSelection = await wire('/?monitor=not-registered');
  assert.equal(invalidSelection.status, 422);
  assert.match(invalidSelection.body.toString(), /Invalid filter selection/);
  const listing = await read('/');
  assert.match(listing, /<title>openRuyi Lookout<\/title>/);
  assert.match(listing, /class="brand"[^]*?<span>openRuyi Lookout<\/span>/);
  for (const [path, heading] of [['/about', 'About'], ['/api', 'API'], ['/packages/failed', 'failed']]) {
    const html = await read(path);
    assert.ok(html.includes(`<title>${heading} · openRuyi Lookout</title>`));
    assert.match(html, /class="count">openRuyi Lookout<\/span>/);
  }
  assert.match(listing, /lang="en"/);
  const packageHeader = listing.match(/<thead>[^]*?<\/thead>/)[0];
  const packageMenuID = packageHeader.match(/popovertarget="([^"]+)"[^>]*aria-label="Filter Package"/)[1];
  assert.ok(packageHeader.includes(`id="${packageMenuID}"`));
  const globalNavigation = packageHeader.match(/<div[^>]*role="group" aria-label="BuildSystem"[^]*?<\/div>/)[0];
  assert.match(packageHeader, /<h2>BuildSystem<\/h2>/);
  assert.match(globalNavigation, /aria-label="BuildSystem"/);
  assert.match(globalNavigation, />custom<\/span><\/span>\s*<b>1<\/b>/);
  assert.match(globalNavigation, />❔ custom<\/span>/);
  assert.equal((globalNavigation.match(/class="brand-icon/g) || []).length, 1);
  const logoURL = globalNavigation.match(/<img src="([^"]+)"/)[1];
  assert.match(logoURL, /^\/_astro\/gopher[.\w-]*\.svg$/);
  const iconResponse = await wire(logoURL);
  assert.equal(iconResponse.status, 200);
  assert.match(iconResponse.headers['content-type'], /image\/svg\+xml/);
  assert.match(listing, /href="https:\/\/build.example.org\/">BuildService<\/a>/);
  assert.match(listing, /href="https:\/\/github.com\/fixture\/repo\/tree\/stable%2F3">stable\/3<\/a>/);
  assert.match(listing, /href="https:\/\/github.com\/fixture\/repo\/commit\/abcdef0123456789abcdef0123456789abcdef01"[^>]*>abcdef<\/a>/);
  assert.equal((listing.match(/<table\b[^]*?<\/table>/)[0].match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 6);
  assert.equal((listing.match(/role="search"/g) || []).length, 1);
  const packageMenu = listing.match(/<div class="choice-rows"[^]*?<div class="results-toolbar"/)[0];
  const filterForms = html => [...html.matchAll(/<form[^>]*class="selection-form"[^]*?<\/form>/g)]
    .map(match => match[0]);
  const formURL = form => '/?' + new URLSearchParams([...form.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)"/g)]
    .map(([, name, value]) => [name, value.replaceAll('&amp;', '&')]));
  const findingForms = filterForms(packageMenu);
  assert.ok(findingForms.length > 0);
  for (const form of findingForms) {
    assert.match(form, /aria-pressed="false"/);
    const count = Number(form.match(/<b>([0-9]+)<\/b>/)[1]);
    assert.ok(count > 0);
    const id = form.match(/<button[^>]*id="([^"]+)"/)[1];
    const after = await read(formURL(form));
    assert.equal(rowKeys(after).size, count, 'facet count matches the submitted result');
    const selectedForm = filterForms(after).find(item => item.includes(`id="${id}"`));
    assert.ok(selectedForm, 'selected filter remains available');
    assert.match(selectedForm, /aria-pressed="true"/);
    const restored = await read(formURL(selectedForm));
    assert.deepEqual(rowKeys(restored), rowKeys(listing), 'removing the last selection restores all packages');
  }
  const activityRow = listing.match(/<tr data-key="success"[^]*?<\/tr>/)[0];
  for (const [kind, title, count] of [['pr', 'Open pull requests', 2], ['issue', 'Open issues', 1]]) {
    const counter = activityRow.match(new RegExp(`<a href="/packages/success#github-${kind}"[^]*?</a>`))[0];
    assert.ok(counter.includes(`aria-label="success: ${title}: ${count}"`));
    assert.match(counter, /class="document-icon"/);
    assert.ok(counter.endsWith(`</span>${count}</a>`));
    assert.match(counter, /class="value value-text /);
  }
  assert.doesNotMatch(listing.match(/<tr data-key="ahead"[^]*?<\/tr>/)[0], /#github-/);
  const subset = await read(filtered([[['maintenance', 'Outdated'], ['maintenance', 'Advisory']]]));
  const outdatedKeys = rowKeys(await read(filtered([[['maintenance', 'Outdated']]])));
  const advisoryKeys = rowKeys(await read(filtered([[['maintenance', 'Advisory']]])));
  assert.deepEqual(rowKeys(subset), outdatedKeys.union(advisoryKeys));
  const narrowed = await read(filtered([[['maintenance', 'Advisory'], ['build:rva23', 'failed']]]));
  const failedKeys = rowKeys(await read(filtered([[['build:rva23', 'failed']]])));
  assert.deepEqual(rowKeys(narrowed), advisoryKeys.intersection(failedKeys));
  const changedPage = await read(filtered([[['version_signal', 'requires']]]));
  await assertGlobalSearch(changedPage, 'advisory-current');
  console.log('PASS facets: unfiltered default, GET toggles, unions, intersections and clearing');
  const untrackedRow = listing.match(/<tr data-key="untracked"[^]*?<\/tr>/)[0];
  const untrackedCells = [...untrackedRow.matchAll(/<t[hd]\b[^>]*>([^]*?)<\/t[hd]>/g)].map(match => match[1]);
  assert.match(untrackedCells[1], />2\.0</);
  assert.doesNotMatch(untrackedCells[1], /value-tag/);
  assert.match(untrackedCells[2], /label%3AUntracked[^>]*>Untracked/);
  const failedRow = listing.match(/<tr data-key="check-error"[^]*?<\/tr>/)[0];
  const failedCells = [...failedRow.matchAll(/<t[hd]\b[^>]*>([^]*?)<\/t[hd]>/g)].map(match => match[1]);
  assert.doesNotMatch(failedCells[0] + failedCells[1], />CheckFailed</);
  assert.match(failedCells[2], /href="\/packages\/check-error#check-version"[^>]*>CheckFailed</);

  const errorPage = await read('/packages/check-error');
  for (const page of [listing, errorPage]) {
    const dataLinks = [...page.matchAll(/<a\b[^>]*href="([^"]+)"/g)]
      .map(([, href]) => href).filter(href => href.startsWith('/api/v2/'));
    assert.deepEqual(dataLinks, [], 'data endpoints belong in the API reference, not page chrome');
  }
  assert.match(errorPage, /id="check-version"/);
  assert.match(errorPage, /Fixture provider timeout/);
  assertCollapsedChecksLast(errorPage);
  assert.equal((packageMenu.match(/>LicenseDiff<\/span>/g) || []).length, 1);
  const terms = [['buildsystem', '_not_detected'], ['maintenance', 'LicenseDiff'], ['build:rva23', 'failed']];
  const scoped = await read(filtered([terms]));
  assert.match(scoped, /0 packages/);
  const zeroSelected = filterForms(scoped).find(form => form.includes('>LicenseDiff</span>'));
  assert.ok(zeroSelected, 'selected zero-count filters remain removable');
  assert.match(zeroSelected, /aria-pressed="true"/);
  assert.match(zeroSelected, /<b>0<\/b>/);
  assert.ok(rowKeys(await read(formURL(zeroSelected))).size > 0);
  await assertGlobalSearch(scoped, 'success');
  const paged = await read('/?per_page=3&page=2');
  const topPager = paged.match(/<nav[^>]*aria-label="Pages above results"[^]*?<\/nav>/)[0];
  const bottomPager = paged.match(/<nav[^>]*aria-label="Pages below results"[^]*?<\/nav>/)[0];
  const pagerLinks = html => [...html.matchAll(/href="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(pagerLinks(topPager), pagerLinks(bottomPager));
  assert.equal(pagerLinks(topPager).length, 2);
  assert.ok(paged.indexOf(topPager) < paged.indexOf('<table class="data-table'));
  assert.ok(paged.indexOf(bottomPager) > paged.indexOf('</table>'));
  for (const link of pagerLinks(topPager)) {
    assert.equal(new URL(link.replaceAll('&amp;', '&'), 'http://fixture').searchParams.get('per_page'), '3');
  }
  assert.match(listing, /class="results-toolbar"[^]*?class="result-count"[^]*?<form[^>]*role="search"/);
  assert.match(listing, /rel="icon" type="image\/svg\+xml" href="\/openruyi.svg"/);
  assert.match(listing, /<img src="\/openruyi.svg" width="38" height="28" alt=""/);
  assert.match(await read('/openruyi.svg'), /<svg/);
  const about = await read('/about');
  assert.deepEqual([...about.matchAll(/<h2[^>]*>(.*?)<\/h2>/g)].map(match => match[1]),
    ['Reading conventions', 'Data sources', 'License']);
  for (const href of ['/api/v2/status', '/api', '/LICENSE.txt',
    'https://osv.dev/', 'https://build.openruyi.cn/', 'https://endoflife.date/']) {
    assert.ok(about.includes(`href="${href}"`), `About link: ${href}`);
  }
  assert.match(about, /Octicons license/);
  assert.match(about, /Font Awesome notices/);
  console.log('PASS About: reading conventions, data sources and license notices');
  assert.match(about, /CC BY-SA 4.0/);
  assert.match(about, /Copyright .*2026 GitHub Inc/);
  assert.match(about, /permission notice shall be included/);
  assert.match(about, /Python Software Foundation/);
  assert.match(about, /Trademark policy/);
  assert.match(about, /Thomas Nagy/);
  assert.match(about, /CC BY-NC \(noncommercial; version unspecified\)/);
  const simple = await read('/?monitor=version');
  assert.match(simple, /<table class="data-table"/);
  assert.match(await read('/?monitor=build'), /<table class="data-table wide status-matrix"/);
  assert.match(listing, /<table class="data-table wide"/);
  const sourceCoverage = await read('/?monitor=source&section=coverage&per_page=2');
  const sourceTable = sourceCoverage.match(/<table\b[^]*?<\/table>/)[0];
  assert.match(sourceTable, />Check<\/th>/);
  assert.match(sourceTable, />Last checked<\/th>/);
  assert.match(sourceTable, /Checked/);
  assert.equal((sourceTable.match(/<col(?:\s[^>]*)?\s*\/?>/g) || []).length, 3);
  await assertGlobalSearch(sourceCoverage, 'advisory-current', '2');
  const focused = await read('/?monitor=yanked');
  assert.match(focused, /Release withdrawn/);
  assert.doesNotMatch(focused, /data-key="success"/);
  const coverage = await read(filtered([[['check:yanked', 'ok']]], {monitor: 'yanked', section: 'coverage'}));
  assert.match(coverage, />Version<\/th>/);
  assert.match(coverage, /Reset filters/);
  const unknownPort = await read('/packages/yanked');
  assert.match(unknownPort, /Release withdrawn/);
  assert.match(unknownPort, />Files<\/dt>/);
  assert.match(unknownPort, />3<\/span>/);
  assert.match(listing, /popovertarget="data-status"[^>]*>Data status · Degraded<\/button>/);
  const statusPanel = listing.slice(listing.indexOf('<div id="data-status"'));
  assert.match(statusPanel, /popover="auto" role="dialog" aria-label="Data status"/);
  assert.match(statusPanel, /intentional fixture error/);
  assert.doesNotMatch(listing.slice(listing.indexOf('<main'), listing.indexOf('</main>')), /intentional fixture error/);
  assert.match(statusPanel, /Collection checks across all packages/);
  const failedCheckURL = [...statusPanel.matchAll(/href="([^"]+)"/g)]
    .map(([, href]) => href.replaceAll('&amp;', '&'))
    .find(href => new URL(href, 'http://fixture').searchParams.getAll('select').includes('check:version=error'));
  assert.ok(failedCheckURL, 'Data status links to the observed error status');
  const failedCoverage = await read(failedCheckURL);
  const errorChoice = filterForms(failedCoverage).find(form => form.includes('id="select-Checks-CheckFailed"'));
  assert.ok(errorChoice, 'the same status is available in Coverage');
  assert.match(errorChoice, /aria-pressed="true"/);
  assert.deepEqual(rowKeys(failedCoverage), new Set(['check-error']));
  assert.doesNotMatch(failedCoverage, /aria-label="Remove Version: CheckFailed"/);
  assert.deepEqual(rowKeys(await read(formURL(errorChoice))), rowKeys(listing));
  const packageRows = name => listing.match(new RegExp(`<tr data-key="${name}"[^]*?</tbody>`))?.[0] ?? '';
  const row = name => listing.match(new RegExp(`<tr data-key="${name}"[^]*?</tr>`))?.[0] ?? '';
  assert.match(row('success'), /data-appearance="buildsystem%3Acustom"/);
  assert.doesNotMatch(row('success'), /#build/);
  const successfulBuilds = [...row('success').matchAll(/<td class="column-status">[^]*?<\/td>/g)];
  assert.equal(successfulBuilds.length, 3);
  for (const [build] of successfulBuilds) {
    assert.match(build, /class="visually-hidden">Succeeded<\/span>/);
    assert.equal((build.match(/class="document-icon"/g) || []).length, 1);
  }
  for (const version of ['1.9', '1.8', '1.7']) assert.ok(row('failed').includes(`>${version}</a>`));
  assert.doesNotMatch(listing, /9\.9-shared/);
  assert.match(row('failed'), /tone-negative/);
  assert.match(row('failed'), /href="\/packages\/failed#check-version"/);
  const presentationCSS = await read('/presentation.css');
  assert.match(presentationCSS, /\[data-appearance="buildsystem%3Acustom"\]\{--appearance-background:#123456;--appearance-foreground:#ffffff\}/);
  assert.doesNotMatch(presentationCSS, /\.value-tag|\.document-nav/, 'one palette authority serves every document role');
  appearancePalette = {custom: {background: '#654321', foreground: '#eeeeee'},
    'unsafe\"}body{color:red}': {background: '#123456;display:none', foreground: '#ffffff'}};
  const changedPaletteCSS = await read('/presentation.css');
  assert.match(changedPaletteCSS, /--appearance-background:#654321;--appearance-foreground:#ffffff/);
  assert.doesNotMatch(changedPaletteCSS, /unsafe|body|display|#123456/);
  for (const icon of ['rust', 'waf']) {
    appearancePalette = {custom: {background: '#123456', foreground: '#ffffff', icon}};
    const changedIcon = await read('/');
    assert.match(changedIcon, new RegExp(`<img src="/_astro/${icon}[.\\w-]*\\.svg"`));
  }
  for (const icon of ['not-in-catalog', '../gopher', 'https://untrusted.example/logo.svg']) {
    appearancePalette.custom.icon = icon;
    const fallback = (await read('/')).match(/<div[^>]*role="group" aria-label="BuildSystem"[^]*?<\/div>/)[0];
    assert.doesNotMatch(fallback, /<img|untrusted|not-in-catalog/);
    assert.match(fallback, />custom<\/span>/);
  }
  appearancePalette = {custom: {background: '#123456', foreground: '#ffffff', icon: 'gopher'}};
  assert.doesNotMatch(listing, / style=/);
  const sourceContext = await read('/packages/success');
  assert.match(sourceContext, /Fixture package/);
  assert.doesNotMatch(sourceContext, /<h2><\/h2>|href="#source"/);
  assert.match(sourceContext, /class="detail-aside"[^]*?<section[^>]*id="build"/);
  assert.match(sourceContext, /class="detail-lead"[^]*?<section[^>]*id="source"/);
  assert.ok(sourceContext.indexOf('class="document-sections"') > sourceContext.indexOf('class="detail-aside"'));
  assert.match(sourceContext, /class="document-sections"[^]*?id="source-description"[^]*?Fixture source description/);
  assert.ok(sourceContext.indexOf('class="package-heading"') < sourceContext.indexOf('class="detail-overview"'));
  assert.match(sourceContext, />MIT<\/span>/);
  assert.match(sourceContext, />\/SPECS\/success<\/a>/);
  assert.match(sourceContext, /href="https:\/\/example.org\/upstream"/);
  assert.match(sourceContext, /data-appearance="buildsystem%3Acustom"/);
  const history = sourceContext.match(/<section\b[^>]*id="changelog"[^]*?<\/section>/)[0];
  assert.match(history, /Fixture change/);
  assert.match(history, /class="entry-metadata"[^]*?Fixture author[^]*?abcdef012345[^]*?2026-09-19 09:00:00 UTC/);
  assert.doesNotMatch(history, /<dl\b|<th\b/);
  const sourceCheck = sourceContext.match(/<tr data-key="source"[^]*?<\/tr>/)[0];
  assert.match(sourceCheck, />Source</);
  assert.match(sourceCheck, /2026-09-19 11:10:00 UTC/);
  const detail = await read('/packages/failed');
  assert.match(detail, />\/SPECS\/failed<\/a>/);
  assert.match(detail, /Last successful version/);
  assert.match(detail, /2026-09-19 11:00:00 UTC/);
  const security = await read('/packages/security');
  assert.equal((security.match(/Local patches, bundled dependencies and binary artifacts are not evaluated/g) || []).length, 1);
  assert.match(security, /0\.396%/);
  assert.match(security, /Reported fixes/);
  assert.match(security, /href="https:\/\/example.org\/upstream"[^>]*>https:\/\/example.org\/upstream<\/a>/);
  assert.match(security, /unavailable/);
  assert.match(security, />No<\/span>/);
  assert.match(security, /<section\b[^>]*id="security"[^>]*>/, 'evidence remains expanded');
  assert.equal((security.match(/>Observed version<\/dt>/g) || []).length, 1);
  assert.equal((security.match(/>Reported fixes<\/dt>/g) || []).length, 8);
  const versionList = await read('/?monitor=version');
  const versionViews = versionList.match(/<div[^>]*role="group" aria-label="Version"[^]*?<\/div>/)[0];
  assert.match(versionViews, />Outdated<\/span>/);
  const versionChecks = versionList.match(/<div[^>]*role="group" aria-label="Checks"[^]*?<\/div>/)[0];
  for (const label of ['Checked', 'CheckFailed', 'Not configured']) assert.ok(versionChecks.includes(label));
  const updatesList = await read(filtered([[['maintenance', 'Outdated']]], {monitor: 'version'}));
  assert.ok(rowKeys(updatesList).size > 0);
  const selectedOutdated = filterForms(updatesList).find(form => form.includes('id="select-Version-Outdated"'));
  assert.match(selectedOutdated, /aria-pressed="true"/);
  assert.doesNotMatch(updatesList, /aria-label="Remove Outdated"/);
  assert.match(versionList, /<div[^>]*role="group" aria-label="Related"/);
  const versionRelated = await read(filtered([[['version_signal', 'security']]], {monitor: 'version', q: 'security'}));
  assert.match(versionRelated, /data-key="security"/);
  assert.doesNotMatch(versionRelated, /data-key="license-evidence"/);
  const versionTable = versionList.match(/<table\b[^]*?<\/table>/)[0];
  assert.match(versionTable, />Advisory 8<\/a>/);
  assert.ok([...versionTable.matchAll(/href="([^"]+)"/g)].some(([, href]) => href.includes('version_signal')));
  assert.doesNotMatch(versionTable, /CVE-2026-|Reported fixes|Exploit probability/);
  const securityDetail = await read('/packages/security');
  assert.match(securityDetail, /href="#security"[^>]*>Advisory 8<\/a>/);
  assert.equal((securityDetail.match(/<section\b[^>]*id="security"/g) || []).length, 1);
  const securityList = await read('/?monitor=security');
  for (let i = 1; i <= 8; i++) assert.match(securityList, new RegExp('>CVE-2026-100' + i + '</a>'));
  const securityTable = securityList.match(/<table\b[^]*?<\/table>/)[0];
  assert.doesNotMatch(securityTable, /<details|Reported fixes|Observed version/);
  const requirementPage = await read('/packages/requires-upgrade');
  const requirementSection = requirementPage.match(/<section\b[^>]*id="requires"[^]*?<\/section>/)[0];
  assert.match(requirementSection, />RuntimeDeps<\/h2>/);
  for (const name of ['Python', 'libwidget', 'libexample']) assert.ok(requirementSection.includes(name));
  assert.match(requirementSection, /≥ 1.80<[^]*?>✓<[^]*?≥ 1.82<[^]*?>✗</);
  assert.match(requirementSection, />Current requirement<[^]*?>Candidate requirement</);
  for (const mark of ['✓', '✗']) assert.ok(requirementSection.includes(`>${mark}<`));
  assert.match(requirementSection, /Observed dependency version does not satisfy the upstream declaration/);
  const optionalSection = requirementPage.match(/<section\b[^>]*id="requires-optional"[^]*?<\/section>/)[0];
  assert.match(optionalSection, /Optional library[^]*?condition not evaluated[^]*?>\?</);
  assert.doesNotMatch(optionalSection, /platform_python_implementation|sys_platform|speedups/);
  const conditions = requirementPage.match(/<details\b[^>]*id="requires-conditions"[^]*?<\/details>/)[0];
  assert.match(conditions, /platform_python_implementation[^]*?speedups/);
  assert.doesNotMatch(conditions.split('>')[0], /\bopen\b/);
  assert.doesNotMatch(requirementSection, /<details/);
  const requiresList = await read('/?monitor=requires');
  for (const name of ['Python', 'libwidget', 'Optional library', 'libexample']) assert.ok(requiresList.includes(name));
  assert.doesNotMatch(requiresList, /platform_python_implementation|sys_platform|speedups/);
  assert.match(requiresList, />RuntimeDeps<[^]*?>Optional RuntimeDeps</);
  const requiresRow = name => requiresList.match(new RegExp(`<tr data-key="${name}"[^]*?</tr>`))?.[0] ?? '';
  assert.match(requiresRow('requires-upgrade'), />2\.0<[^]*?→[^]*?>2\.1</);
  assert.match(requiresRow('requires-current'), /libcurrent/);
  assert.doesNotMatch(requiresRow('requires-current'), /→|Upgrade:|>2\.0</);
  assert.match(requiresRow('requires-target-only'), /libtarget<[^]*?Upgrade:[^]*?>✗</);
  const requirementNav = requiresList.match(/<div[^>]*role="group" aria-label="Dependencies"[^]*?<\/div>/)[0];
  for (const label of ['DepMismatch', 'DepChanges']) assert.ok(requirementNav.includes(label));
  const requirementChecks = requiresList.match(/<div[^>]*role="group" aria-label="Checks"[^]*?<\/div>/)[0];
  assert.match(requirementChecks, />Checked<\/span>/);
  const requiresFiltered = await read(filtered([[['maintenance', 'DepMismatch']]], {monitor: 'requires', per_page: '1'}));
  assert.match(requiresFiltered, /Reset filters/);
  const selectedMismatch = filterForms(requiresFiltered).find(form => form.includes('id="select-Dependencies-DepMismatch"'));
  assert.match(selectedMismatch, /aria-pressed="true"/);
  assert.doesNotMatch(requiresFiltered, /aria-label="Remove DepMismatch"/);
  const depChanges = await read(filtered([[['maintenance', 'DepChanges']]], {monitor: 'requires'}));
  const selectedChanges = filterForms(depChanges).find(form => form.includes('id="select-Dependencies-DepChanges"'));
  assert.match(selectedChanges, /aria-pressed="true"/);
  assert.doesNotMatch(depChanges, /aria-label="Remove DepChanges"/);
  await assertGlobalSearch(requiresFiltered, 'advisory-current', '1');
  const requiresCoverage = await read(filtered([[['check:requires', 'uncovered'], ['maintenance', 'DepMismatch']]], {monitor: 'requires', section: 'coverage'}));
  assert.match(requiresCoverage, />Version<\/th>/);
  assert.match(requiresCoverage, /0 packages/);
  const uncovered = await read(filtered([[['check:version', 'uncovered']]], {monitor: 'version', section: 'coverage'}));
  const uncoveredRow = uncovered.match(/<tr data-key="untracked"[^]*?<\/tr>/)[0];
  assert.match(uncoveredRow, />2\.0</);
  assert.doesNotMatch(uncoveredRow, /→/);
  const buildReason = await read('/packages/build-reason');
  assert.match(buildReason, /nothing provides &lt;fixture-dependency&gt;/);
  assert.doesNotMatch(buildReason, /worker:\/\/|nothing provides <fixture-dependency>/);
  const buildList = await read('/?monitor=build');
  assert.match(buildList, /status-matrix/);
  for (const target of targets) {
    assert.match(buildList, new RegExp(`aria-label="Filter ${target.label}"`));
  }
  const buildFiltered = await read(filtered([[['build:rva23', 'failed'], ['build:rva23', 'unresolvable']]], {monitor: 'build'}));
  const selectedTarget = buildFiltered.match(/<div[^>]*role="group" aria-label="rva23"[^]*?<\/div>/)[0];
  assert.equal((selectedTarget.match(/aria-pressed="true"/g) || []).length, 2);
  assert.match(buildFiltered, /class="navigation-menu-trigger has-selection"[^>]*aria-label="Filter rva23: Failed, Unresolvable"/);
  for (const label of ['rva23: Failed', 'rva23: Unresolvable']) {
    assert.ok(buildFiltered.includes(`aria-label="Remove ${label}"`));
  }
  assert.match(buildFiltered, /data-key="failed"/);
  assert.match(buildFiltered, /data-key="build-reason"/);
  assert.match(buildList, /nothing provides &lt;fixture-dependency&gt;/);
  assert.doesNotMatch(buildList, /worker:\/\//);
  const aggregateReason = packageRows('build-reason');
  assert.match(aggregateReason, /colspan="3" aria-hidden="true"/);
  assert.match(aggregateReason, /colspan="1"[^]*?headers="row-Packages-\d+ column-Packages-3"[^]*?nothing provides &lt;fixture-dependency&gt;/);
  const aggregateRuntime = packageRows('requires-upgrade');
  assert.match(aggregateRuntime, /RuntimeDeps:[^]*?libwidget/);
  assert.doesNotMatch(aggregateRuntime, /Python|Optional library|platform_python_implementation/);
  assert.match(listing, /MIT → Apache-2.0/);
  assert.match(listing, /CVE-2026-1001/);

  assert.doesNotMatch(listing.match(/<table\b[^]*?<\/table>/)[0], /<details/);
  assert.match(detail, /id="checks"/);
  const license = await read('/packages/license-evidence');
  assert.match(license, /MIT, Apache-2\.0/);
  assert.match(license, />Target<\/dt>/);
  assert.match(license, />No<\/span>/);
  assert.match(license, /<section\b[^>]*id="license"[^>]*>/, 'license evidence remains expanded');
  for (const pkg of packages) {
    const page = await read(pkg.detail_url);
    assert.match(page, /<section\b[^>]*id="changelog"[^>]*>/);
    assertCollapsedChecksLast(page);
  }
  assert.match(await read('/', 'theme=dark'), /data-theme="dark"/);
  assert.match(await read('/', 'theme=light'), /data-theme="light"/);
  // Unknown document data is escaped, including provenance URLs. No raw HTML or scripts.
  packages.push(makePackage('hostile', {spec: {source_path: 'SPECS/hostile', source_url: 'javascript:alert(1)',
    metadata: {summary: '<script>alert(1)</script>', url: '//evil.example'}, changelog: []}}));
  const hostile = await read('/packages/hostile');
  assert.match(hostile, /&lt;script&gt;/);
  assert.doesNotMatch(hostile, /href="(?:javascript:|\/\/evil)/);
  assertCollapsedChecksLast(hostile);
  packages.pop();
  console.log('PASS documents: arbitrary monitor, opaque navigation, immediate GET, visible tables/fields, safe links, theme and retained evidence');
  // Wire-level tests deliberately bypass fetch's automatic decompression/cache.
  const requestCount = forwardedRequests.length;
  const identity = await wire('/');
  assert.equal(forwardedRequests.length, requestCount + 1, 'SSR obtains its document once');
  assert.equal(identity.status,200);assert.equal(identity.headers['content-encoding'],undefined);
  assert.equal(identity.headers['cache-control'],'private, no-cache');
  assert.match(identity.headers['content-security-policy'], /script-src 'self'/);
  assert.doesNotMatch(identity.headers['content-security-policy'], /unsafe-inline|unsafe-eval/);
  const detailResponse = await wire('/packages/success');
  assert.match(detailResponse.headers['content-security-policy'], /script-src 'self'/);
  assert.doesNotMatch(detailResponse.headers['content-security-policy'], /unsafe-inline|unsafe-eval/);
  const parsedScripts = spawnSync(process.env.PYTHON || 'python3',
    [fileURLToPath(new URL('./html-scripts.py', import.meta.url))],
    {input: detailResponse.body, encoding: 'utf8', timeout: 5000});
  assert.equal(parsedScripts.status, 0, parsedScripts.stderr);
  const scripts = JSON.parse(parsedScripts.stdout);
  assert.equal(scripts.length, 1);
  const scriptPath = scripts[0].attributes.src;
  assert.match(scriptPath, /^\/_astro\/[^/]+\.js$/);
  assert.equal(scripts[0].text.trim(), '');
  assert.match(identity.body.toString(), new RegExp(`src="${scriptPath.replaceAll('.', '\\.')}"`));
  const script = await wire(scriptPath, {'Accept-Encoding': 'gzip'});
  assert.equal(script.status, 200);
  assert.match(script.headers['content-type'], /javascript/);
  assert.match(script.headers['cache-control'], /immutable/);
  console.log(`NAVIGATION gzip: ${script.body.length} bytes`);
  const fingerprint = response => response.body.toString().match(/data-page-fingerprint="([0-9a-f]{64})"/)[1];
  assert.match(identity.body.toString(), /hx-select="#page"/);
  assert.equal(fingerprint(identity), fingerprint(await wire('/')));
  assert.equal(fingerprint(identity), fingerprint(await wire('/', {'HX-Request': 'true'})));
  assert.match((await wire('/livez')).headers['content-security-policy'], /script-src 'none'/);
  assert.match(identity.headers.vary,/Cookie/);
  const etag=identity.headers.etag;assert.match(etag,/^W\/"[0-9a-f]{64}"$/);
  for (const coding of ['gzip','br']) {
    const compressed=await wire('/',{'Accept-Encoding':coding});
    assert.equal(compressed.headers['content-encoding'],coding);
    assert.match(compressed.headers.vary,/Accept-Encoding/);
    const decoded=(coding==='gzip'?gunzipSync:brotliDecompressSync)(compressed.body);
    assert.deepEqual(decoded,identity.body);
    assert.ok(compressed.body.length<identity.body.length/2);
    assert.equal(compressed.headers.etag,etag);
    console.log(`TRANSPORT ${coding}: ${identity.body.length} -> ${compressed.body.length} bytes`);
  }
  const prohibited=await wire('/',{'Accept-Encoding':'gzip;q=0, br;q=0, deflate;q=0, identity;q=1'});
  assert.equal(prohibited.headers['content-encoding'],undefined);assert.deepEqual(prohibited.body,identity.body);
  const validated=await wire('/',{'If-None-Match':etag,'Accept-Encoding':'gzip'});
  assert.equal(validated.status,304);assert.equal(validated.body.length,0);assert.equal(validated.headers.etag,etag);
  assert.equal(validated.headers['cache-control'],'private, no-cache');assert.match(validated.headers.vary,/Cookie/);
  assert.equal((await wire('/',{'If-None-Match':'"not-this", '+etag.slice(2)})).status,304);
  const themed=await wire('/',{'If-None-Match':etag,Cookie:'theme=dark'});assert.equal(themed.status,200);assert.notEqual(themed.headers.etag,etag);
  assert.notEqual(fingerprint(themed), fingerprint(identity));
  assert.equal((await wire('/?q=success',{'If-None-Match':etag})).status,200);
  packages[0].current='2.0.1';
  const changed=await wire('/',{'If-None-Match':etag});assert.equal(changed.status,200);assert.notEqual(changed.headers.etag,etag);
  assert.notEqual(fingerprint(changed), fingerprint(identity));
  packages[0].current='2.0';
  const cssPath=identity.body.toString().match(/href="(\/_astro\/[^"]+\.css)"/)[1];
  const css=await wire(cssPath,{'Accept-Encoding':'gzip'});assert.equal(css.status,200);assert.equal(css.headers['content-encoding'],'gzip');assert.match(css.headers['cache-control'],/immutable/);assert.ok(gunzipSync(css.body).length>css.body.length);
  const appCSS = gunzipSync(css.body).toString();
  assert.ok([...appCSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)].some(([, selectors, body]) => selectors.split(',').includes('.value-tag') && body.includes('var(--appearance-background')), 'tag colors use the declared palette');
  assert.match(appCSS, /\.value-tag\[data-size=small\]/);
  const range=await wire(cssPath,{'Accept-Encoding':'gzip',Range:'bytes=0-9'});assert.equal(range.status,206);assert.equal(range.headers['content-encoding'],undefined);assert.equal(range.body.length,10);
  assert.equal((await wire('/',{},'HEAD')).body.length,0);
  const json=await wire('/api/v2/packages',{'Accept-Encoding':'gzip'});assert.equal(json.status,200);assert.equal(json.headers['cache-control'],'no-store');assert.equal(json.headers['content-encoding'],'gzip');assert.ok(JSON.parse(gunzipSync(json.body)).items.length>0);
  const redirect=await wire('/theme?to=dark&from=%2F');assert.equal(redirect.status,303);assert.equal(redirect.headers['cache-control'],'no-store');assert.ok(redirect.headers['set-cookie']);assert.equal(redirect.headers.etag,undefined);
  for (const from of ['/\t/example.invalid', '/\n/example.invalid', '/\r/example.invalid',
      '//example.invalid', '/\\example.invalid', 'https://example.invalid']) {
    for (const to of ['dark', 'invalid']) {
      const response = await wire('/theme?' + new URLSearchParams({to, from}));
      assert.equal(response.status, 303);
      assert.equal(response.headers.location, '/', `Unsafe theme return: ${JSON.stringify(from)}`);
    }
  }
  const back = '/packages/a%2Bb?monitor=requires&q=one%20two#checks';
  const returned = await wire('/theme?' + new URLSearchParams({to: 'auto', from: back}));
  assert.equal(returned.status, 303);
  assert.equal(returned.headers.location, back);
  console.log('PASS theme redirect: control characters and external targets rejected; local path/query/fragment retained');
  unavailable=true;
  const failure=await wire('/');assert.equal(failure.status,503);assert.equal(failure.headers['cache-control'],'no-store');assert.equal(failure.headers.etag,undefined);
  unavailable=false;
  assert.equal((await wire('/')).status, 200, 'backend recovery is not hidden by a cached error');
  console.log('PASS transport: gzip/br, identity/q=0, decoded equality, ETag304, changed data/theme/query, immutable CSS GET, ranges, HEAD, JSON, errors, cookies and CSP');
  console.log('PASS SSR document renderer');
  }
} finally {
  child.kill('SIGTERM'); await once(child, 'exit'); mock.closeAllConnections(); await new Promise(resolve => mock.close(resolve));
}
