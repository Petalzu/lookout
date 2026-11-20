from tests.helpers.query import query_url, terms_in
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
import pytest

from tests.helpers.documents import client_for
from tracker import state
from tracker.monitors import model as monitor_model
from tracker.presentation import (
    build as presentation_build,
    evidence as presentation_evidence,
    navigation as presentation_navigation,
    pages as presentation_pages,
    values as presentation_values,
    version as presentation_version,
)
from tracker.presentation.model import Cell, Column, Row, Table
from tracker.readmodel import snapshot as view


def add_evidence(snapshot, name='binutils', mid='external_signature', *, status='ok'):
    snapshot.setdefault('monitor_catalog', {})[mid] = {'title': 'Artifact signatures'}
    snapshot.setdefault('monitors', {}).setdefault(name, {})[mid] = {
        'status': status, 'checked_at': state.utcnow(), 'subject': monitor_model.subject(snapshot, name),
        'findings': [monitor_model.finding('signature-1', 'Signature', 'Signing key changed', [
            monitor_model.evidence('Issuer', 'Example CA', 'Registry', 'https://example.org/signature')
        ], 'https://example.org/signature')],
    }


def test_new_monitor_uses_existing_document_primitives(snapshot, tmp_path):
    add_evidence(snapshot)
    client, db = client_for(snapshot, tmp_path)
    original = db.read_bytes()
    listing = client.get('/api/ui/packages?monitor=external_signature')
    assert listing.status_code == 200
    document = listing.json()
    assert document['title'] == 'Artifact signatures'
    assert document['total'] == 1
    assert [c['title'] for c in document['table']['columns']] == ['Package', 'Artifact signatures']
    assert document['table']['rows'][0]['cells'][1]['lines'][0][0]['text'] == 'Signing key changed'
    detail = client.get('/api/ui/packages/binutils').json()
    section = next(s for s in detail['sections'] if s['id'] == 'external_signature')
    entry = section['entries'][0]
    assert entry['heading'][0]['href'] == 'https://example.org/signature'
    assert entry['fields'][0]['values'][0]['text'] == 'Example CA'
    assert entry['fields'][0]['values'][1]['text'] == 'Registry'
    assert db.read_bytes() == original


def test_security_list_shows_every_identifier_once_without_full_evidence(snapshot, tmp_path):
    add_evidence(snapshot, mid='security')
    snapshot['monitor_catalog']['security']['title'] = 'Advisory'
    observed = snapshot['monitors']['binutils']['security']
    observed['findings'] = [monitor_model.finding(f'advisory-{i}', 'Advisory', f'CVE-2026-{1000 + i}',
        [monitor_model.evidence('Long provider explanation', 'detail only', 'OSV', 'https://example.org/')],
        f'https://example.org/{i}', tags=['KEV'] if i == 0 else []) for i in range(8)]
    client, _ = client_for(snapshot, tmp_path)
    document = client.get('/api/ui/packages?monitor=security').json()
    cell = document['table']['rows'][0]['cells'][1]
    assert len(cell['lines']) == 1  # generic wrapping values, not eight tall rows
    assert [v['text'] for v in cell['lines'][0]] == [
        f'CVE-2026-{1000 + i}' + (' · KEV' if i == 0 else '') for i in range(8)]
    assert [v['href'] for v in cell['lines'][0]] == [f'https://example.org/{i}' for i in range(8)]
    assert 'detail only' not in str(document) and 'advisories' not in str(document)
    overview = client.get('/api/ui/packages').json()
    assert all(f'CVE-2026-{1000 + i}' in str(overview['table']) for i in range(8))
    assert 'Security 8' not in str(overview['table'])
    assert 'detail only' not in str(overview)


def test_security_retained_identifiers_have_one_freshness_marker():
    entries = [dict(id=str(i), title=f'CVE-2026-{i}', evidence_url='https://example.org/',
                    stale=i == 1, tags=[]) for i in range(2)]
    result = dict(id='security', data=dict(entries=entries, finding_count=2))
    lines = presentation_evidence.evidence_cells({}, result, presentation_navigation.Links())[0].lines
    assert [value.text for value in lines[0]] == ['CVE-2026-0']
    assert [value.text for value in lines[1]] == ['Stale', 'CVE-2026-1']
    query = parse_qs(urlsplit(lines[1][0].href).query)
    assert query['monitor'] == ['security']
    assert any(dimension.startswith('retained:') and value == 'yes' for dimension, value in terms_in(lines[1][0].href))


def test_no_evidence_and_failed_check_are_not_reported_as_success(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'new': {'title': 'New check'}}
    client, _ = client_for(snapshot, tmp_path)
    results = client.get('/api/ui/packages?monitor=new').json()
    assert client.get('/api/ui/packages?monitor=new&section=coverage').json()['total'] == 5
    coverage = client.get(query_url('/api/ui/packages', {'check:new': 'pending'}, monitor='new', section='coverage')).json()
    assert results['total'] == 0
    assert coverage['total'] == 5
    assert [c['title'] for c in coverage['table']['columns']] == ['Package', 'Version']
    versions = {row['key']: row['cells'][1]['lines'][0][0]['text'] for row in coverage['table']['rows']}
    assert versions == {name: source['version'] or '—' for name, source in snapshot['sources'].items()}


@pytest.mark.parametrize('monitor', ['version', 'external_signature'])
def test_filtered_checks_show_source_version_and_specific_error(snapshot, tmp_path, monitor):
    add_evidence(snapshot, mid='external_signature', status='error')
    snapshot['monitors']['binutils']['external_signature']['error'] = 'provider HTTP 503'
    snapshot['tracks']['binutils']['error'] = 'provider HTTP 503'
    client, _ = client_for(snapshot, tmp_path)
    document = client.get(query_url('/api/ui/packages', {'check:' + monitor: 'failed'}, monitor=monitor, section='coverage')).json()
    assert [c['title'] for c in document['table']['columns']] == ['Package', 'Version', 'Reason']
    row = next(row for row in document['table']['rows'] if row['key'] == 'binutils')
    assert [value['text'] for cell in row['cells'][1:] for line in cell['lines'] for value in line] == [
        snapshot['sources']['binutils']['version'], 'provider HTTP 503']
    assert 'Failed' not in str(document['table'])
    assert '3.10.0' not in str(row)  # a retained upstream target is not the current source


def test_uncovered_rows_do_not_repeat_filter_or_empty_timestamps(snapshot, tmp_path):
    client, _ = client_for(snapshot, tmp_path)
    document = client.get(query_url('/api/ui/packages', {'check:version': 'uncovered'}, monitor='version', section='coverage')).json()
    assert [c['title'] for c in document['table']['columns']] == ['Package', 'Version']
    versions = {r['key']: r['cells'][1]['lines'][0][0]['text'] for r in document['table']['rows']}
    assert versions == {'untracked': '1.0', 'unknown': '—'}
    assert 'Not configured' not in str(document['table'])


def test_build_semantics_are_projected_not_reinterpreted_in_website(snapshot):
    source = snapshot['sources']['binutils']
    builds = snapshot['builds']['binutils']
    for index, target in enumerate(builds.values()):
        target['last_success'] = {'version': source['version'], 'srcmd5': source['srcmd5'], 'time': state.utcnow()}
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['build']
    for build in result['data']['targets']:
        assert len(presentation_build.build_cell(build, source['version'], pkg['detail_url']).lines) == 1
    target = result['data']['targets'][1]
    target.update(raw_status='failed', kind='error', text='Failed', matches_source=False)
    target['last_success']['version'] = 'previous.rva20'
    rendered = presentation_build.build_cell(target, source['version'], pkg['detail_url'])
    assert rendered.lines[0][0].text == 'Failed'
    assert rendered.lines[0][0].tone == 'negative'
    assert rendered.lines[1][0].text == 'previous.rva20'
    assert rendered.lines[1][0].href.endswith('#build')


def test_partial_enrichment_and_alias_facts_remain_traceable(snapshot):
    add_evidence(snapshot)
    facts = snapshot['monitors']['binutils']['external_signature']['findings'][0]['facts']
    facts.extend([
        monitor_model.evidence('Changed wording', 0.004, 'FIRST', 'https://example.org/epss', code='epss_probability'),
        monitor_model.evidence('Model date', '2026-09-19', 'FIRST', 'https://example.org/epss'),
        monitor_model.evidence('KEV', None, 'CISA', 'https://example.org/kev', status='unavailable'),
        monitor_model.evidence('False value', False, 'Registry', 'https://example.org/')])
    pkg = view.project_monitors(snapshot)[0][0]
    before = deepcopy(pkg)
    result = presentation_pages.detail(pkg)
    entry = next(s for s in result.sections if s.id == 'external_signature').entries[0]
    assert next(f for f in entry.fields if f.label == 'Changed wording').values[0].text == '0.4%'
    assert any(f.label == 'Model date' and f.values[0].text == '2026-09-19' for f in entry.fields)
    assert any(f.label == 'KEV' and f.values[0].text == 'unavailable' for f in entry.fields)
    assert any(f.label == 'False value' and f.values[0].text == 'No' for f in entry.fields)
    assert pkg == before


def test_document_contract_rejects_ragged_tables_and_unknown_markup():
    with pytest.raises(ValidationError):
        Table(label='Bad', columns=[Column(title='A')], rows=[Row(key='a', cells=[])])
    with pytest.raises(ValidationError):
        Cell.model_validate({'lines': [], 'html': '<script>alert(1)</script>'})


@pytest.mark.parametrize('query', ['monitor=not-registered', 'page=-2', 'build=absent:failed'])
def test_display_and_data_routes_share_validation(snapshot, tmp_path, query):
    client, _ = client_for(snapshot, tmp_path)
    assert client.get('/api/ui/packages?' + query).status_code == 422
    assert client.get('/api/v2/packages?' + query).status_code == 422


def test_projection_retains_domain_api_and_theme_contract(snapshot, tmp_path):
    snapshot['presentation'] = {'buildsystems': {'unfamiliar': {'background': '#123456', 'foreground': '#ffffff'}}}
    client, _ = client_for(snapshot, tmp_path)
    old = client.get('/api/v2/packages/binutils').json()
    assert old['monitors']['build']['data']['kind'] == 'build'
    assert client.get('/api/ui/theme').json()['appearances']['buildsystem:unfamiliar']['background'] == '#123456'
    assert client.get('/api/ui/packages/absent').status_code == 404


def test_version_display_uses_decision_not_independent_comparison(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    version = pkg['monitors']['version']['data']
    build = pkg['monitors']['build']['data']
    version.update(current='source.version', latest='target.version', relation='outdated', track='reviewed')
    build['source_success'] = False
    values = presentation_values.version_value(pkg)
    assert [value.text for value in values] == ['source.version', '→', 'target.version']
    assert values[0].tone == 'negative' and values[2].tone == 'positive'
    for relation in ('ahead', 'current', 'unknown', 'not_applicable'):
        version['relation'] = relation
        assert [value.text for value in presentation_values.version_value(pkg)] == ['source.version']
    version.update(track=None, relation='untracked')
    pkg['monitors']['version']['dimensions']['view'] = ['untracked']
    pkg['monitors']['version']['dimensions']['maintenance'] = ['Untracked']
    assert presentation_values.version_value(pkg)[0].tone == 'negative'
    current, candidate, annotations = presentation_values.version_parts(pkg)
    assert [value.text for value in current] == ['source.version']
    assert candidate == []
    assert annotations[0].title == 'No upstream version track configured'


@pytest.mark.parametrize('matches', [False, None, True])
@pytest.mark.parametrize('status,kind', [('succeeded', 'ok'), ('failed', 'error'), ('building', 'pending')])
@pytest.mark.parametrize('version', ['3.9.0', '3.9.0.arch'])
def test_last_success_is_omitted_only_for_success_at_current_version(matches, status, kind, version):
    build = dict(raw_status=status, kind=kind, text=status, matches_source=matches,
                 last_success={'version': version, 'time': state.utcnow()})
    lines = presentation_build.build_cell(build, '3.9.0', '/packages/fixture').lines
    if status == 'succeeded' and version == '3.9.0':
        assert len(lines) == 1
    else:
        assert lines[1][0].text == version
    build['last_success'] = None
    assert len(presentation_build.build_cell(build, '3.9.0', '/packages/fixture').lines) == 1


def test_query_fact_does_not_turn_an_unrelated_monitor_into_security(snapshot):
    add_evidence(snapshot)
    finding = snapshot['monitors']['binutils']['external_signature']['findings'][0]
    finding['facts'].append(monitor_model.evidence('Lookup', 'artifact', 'Registry', 'https://example.org/', code='query'))
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation_pages.detail(pkg).sections if s.id == 'external_signature')
    assert section.notes == []
    assert section.fields[0].values[0].text == 'artifact'


def test_build_reasons_keep_their_target_and_flavor_without_expanding_homepage(snapshot):
    snapshot['builds']['binutils']['rva23'].update(raw_status='unresolvable', details='nothing provides ebtables')
    snapshot['builds']['binutils']['rva20'].update(raw_status='building', details='worker://private-host')
    snapshot['builds']['foo3:tools']['rva20'].update(raw_status='blocked', details='waiting for toolkit')
    packages = {p['name']: p for p in view.project_monitors(snapshot)[0]}
    build = packages['binutils']['monitors']['build']
    section = presentation_build.build_sections(build, presentation_navigation.Links())[0]
    assert section.table.rows[0].cells[1].lines[1][0].text == 'nothing provides ebtables'
    assert len(section.table.rows[1].cells[1].lines) == 1
    assert 'worker://' not in section.model_dump_json()
    assert 'nothing provides' not in str(presentation_build.build_cells(packages['binutils'], build, presentation_navigation.Links()))
    multiple = presentation_build.build_sections(packages['foo3']['monitors']['build'], presentation_navigation.Links())[0]
    assert [r.key for r in multiple.table.rows if 'waiting for toolkit' in r.model_dump_json()] == ['rva20:foo3:tools']


def test_references_share_one_field_but_keep_distinct_fix_links():
    urls = ['https://example.org/commit/abcdef0123456789', 'https://example.org/commit/123456abcdef']
    facts = [monitor_model.evidence('FIX', url, 'OSV', url, code='reference') for url in urls]
    result = presentation_evidence.fact_fields([*facts, facts[0]])
    assert len(result) == 1 and result[0].label == 'References'
    assert [v.href for v in result[0].values] == urls
    assert [v.text for v in result[0].values] == ['Fix · abcdef012345', 'Fix · 123456abcdef']


def test_upgrade_context_does_not_require_a_named_frontend_monitor(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['external_signature']
    result['data']['findings'][0].update(scope='upgrade', target_version='3.10.0')
    rendered = presentation_evidence.evidence_cells(pkg, result, presentation_navigation.Links())[0]
    assert rendered.lines[0][0].text == pkg['monitors']['version']['data']['current']
    section = presentation_evidence.evidence_section(result, presentation_navigation.Links())[0]
    assert next(f for f in section.fields if f.label == 'Target').values[0].text == '3.10.0'
    assert all(f.label != 'Target' for entry in section.entries for f in entry.fields)


def test_watch_failure_does_not_replace_primary_version(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['version']
    result['data']['watch'] = [{'id': 'other@preview', 'version': '9.0rc1', 'error': 'Timeout', 'stale': True}]
    primary = presentation_values.version_value(pkg)
    section = presentation_version.version_sections(result, presentation_navigation.Links())[0]
    assert section.entries[0].heading[0].href == '/api/v2/tracks/other%40preview'
    assert section.entries[0].fields[0].label == 'Last observed'
    assert section.entries[0].fields[1].values[0].text == 'Timeout'
    assert all(value.text != '9.0rc1' for value in primary)


def test_check_notes_and_attempts_are_retained(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    pkg['monitors']['external_signature']['check'].update(
        note='Identity is missing', status='input_unavailable',
        checked_at='2026-09-18T01:00:00Z', attempted_at='2026-09-19T01:00:00Z')
    checks = next(s for s in presentation_pages.detail(pkg).sections if s.id == 'checks')
    row = next(r for r in checks.table.rows if r.key == 'external_signature')
    assert row.cells[1].lines[0][0].text == 'Input unavailable'
    assert row.cells[1].lines[1][0].text == 'Identity is missing'
    assert row.cells[2].lines[0][0].datetime == '2026-09-18T01:00:00Z'
    assert row.cells[2].lines[1][1].datetime == '2026-09-19T01:00:00Z'


def test_ordinary_version_does_not_repeat_same_name_track(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['version']
    result['data'].update(track=pkg['name'], relation='current', watch=[])
    assert presentation_version.version_sections(result, presentation_navigation.Links()) == []
    result['data']['relation'] = 'unknown'
    section = presentation_version.version_sections(result, presentation_navigation.Links())[0]
    assert [f.label for f in section.fields] == ['Comparison']
    assert section.fields[0].values[0].text == 'Cannot compare current observations'


def test_evidence_is_not_duplicated_as_summary_and_keeps_all_sources(snapshot):
    add_evidence(snapshot)
    finding = snapshot['monitors']['binutils']['external_signature']['findings'][0]
    finding['facts'] = [monitor_model.evidence(
        'Provider fixed events', ['2.4', '3.1'], 'Registry', url, code='fixed_events')
        for url in ['https://example.org/one', 'https://example.org/two', 'https://example.org/one']]
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation_pages.detail(pkg).sections if s.id == 'external_signature')
    entry = section.entries[0]
    assert len(entry.fields) == 1
    assert entry.fields[0].values[0].text == '2.4, 3.1'
    assert [v.href for v in entry.fields[0].values[1:]] == ['https://example.org/one', 'https://example.org/two']
    assert entry.metadata == []


def test_shared_query_context_appears_once_and_distinct_inputs_are_not_hidden(snapshot):
    add_evidence(snapshot)
    findings = snapshot['monitors']['binutils']['external_signature']['findings']
    common = monitor_model.evidence('Query package', 'reviewed identity', 'Registry', 'https://example.org/query', code='query')
    findings[0]['facts'].append(common)
    findings.append({**deepcopy(findings[0]), 'id': 'signature-2', 'title': 'Another key'})
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation_pages.detail(pkg).sections if s.id == 'external_signature')
    assert [f.label for f in section.fields] == ['Query package']
    assert not any(f.label == 'Query package' for e in section.entries for f in e.fields)
    findings[1]['facts'][-1]['value'] = 'another identity'
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation_pages.detail(pkg).sections if s.id == 'external_signature')
    assert section.fields == []
    assert sum(f.label == 'Query package' for e in section.entries for f in e.fields) == 2


def test_package_context_and_changelog_remain_visible_without_empty_sections(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    source = pkg['monitors']['source']['data']
    source['metadata'] = {'license': 'MIT', 'summary': 'Short summary', 'description': 'Useful description'}
    source['changelog'] = [{'subject': 'Fix build', 'commit': 'a' * 40, 'author': 'Packager',
                           'date': '2026-09-19T01:00:00Z', 'signed_off_by': ['Packager']}]
    document = presentation_pages.detail(pkg)
    context = next(s for s in document.sections if s.id == 'source')
    assert context.notes == []
    assert next(s for s in document.sections if s.id == 'source-description').notes == ['Useful description']
    assert context.fields[0].values[0].text == 'MIT'
    changelog = next(s for s in document.sections if s.title == 'Changelog')
    assert changelog.entries[0].metadata[0].text == 'Packager'
    assert source['changelog'][0]['signed_off_by'] == ['Packager']
    assert document.sections[-1].id == 'checks'
    assert [s.id for s in document.sections + document.context if s.collapsible] == ['checks']


def test_compact_labels_do_not_hide_different_cve_subjects(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['external_signature']
    finding = result['data']['findings'][0]
    finding.update(title='CVE-2026-1000', stale=True, facts=[
        monitor_model.evidence('KEV · CVE-2026-1000', False, 'CISA', 'https://example.org/kev'),
        monitor_model.evidence('KEV · CVE-2026-1001', None, 'CISA', 'https://example.org/kev', status='unavailable')])
    section = presentation_evidence.evidence_section(result, presentation_navigation.Links())[0]
    assert [f.label for f in section.entries[0].fields] == ['KEV', 'KEV · CVE-2026-1001']
    assert [v.text for v in section.entries[0].heading] == ['CVE-2026-1000']
    assert section.fields == []
    assert section.title.endswith(' · Stale')


def test_meaningful_release_line_is_not_lost_with_redundant_track(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['version']
    result['data'].update(relation='current', watch=[], track_label='3.x')
    section = presentation_version.version_sections(result, presentation_navigation.Links())[0]
    assert [(f.label, f.values[0].text) for f in section.fields] == [('Release line', '3.x')]


def test_equal_formatted_values_do_not_merge_distinct_provider_assertions():
    facts = [monitor_model.evidence('Probability', value, 'FIRST', 'https://example.org/' + str(i), code='epss_probability')
             for i, value in enumerate([0.001231, 0.001232])]
    fields = presentation_evidence.fact_fields(facts)
    assert len(fields) == 2
    assert fields[0].values[0].text == fields[1].values[0].text
    assert fields[0].values[1].href != fields[1].values[1].href


def test_page_size_survives_navigation_and_filter_forms(snapshot, tmp_path):
    client, _ = client_for(snapshot, tmp_path)
    document = client.get('/api/ui/packages?per_page=2').json()
    assert len(document['table']['rows']) == 2
    assert {'name': 'per_page', 'value': '2'} in document['controls']['hidden']
    links = presentation_navigation.Links({'per_page': 2, 'page': 1, 'q': 'foo'})
    for link in (links.to(page=2), links.to(monitor='build'), links.to(q='')):
        assert parse_qs(urlsplit(link).query)['per_page'] == ['2']
    assert client.get(links.to(page=2).replace('/?', '/api/ui/packages?')).status_code == 200


def test_monitor_data_contract_is_discriminated_by_kind(snapshot):
    from tracker.api import MonitorObservation
    from tracker.api import MonitorSummary
    row = view.project_monitors(snapshot)[0][0]
    source = row['monitors']['source']
    for model in (MonitorObservation, MonitorSummary):
        parsed = model.model_validate(source)
        assert parsed.data.kind == 'source'
        broken = deepcopy(source)
        broken['data']['kind'] = 'version'
        with pytest.raises(ValidationError):
            model.model_validate(broken)
        schema = model.model_json_schema()['properties']['data']
        assert schema['discriminator']['propertyName'] == 'kind'
        assert set(schema['discriminator']['mapping']) == {
            'source', 'version', 'build', 'evidence', 'requires', 'activity', 'commits',
        }
        commits = {**source, 'id': 'commits', 'title': 'Commits', 'data': {
            'kind': 'commits', 'entries': [], 'selected': 0, 'total': 0,
            'batch_id': None, 'repository': None, 'coverage': {}, 'labels': [],
        }}
        assert model.model_validate(commits).data.kind == 'commits'
        commits['data']['kind'] = 'source'
        with pytest.raises(ValidationError):
            model.model_validate(commits)


def test_spec_link_uses_the_path_as_its_only_label(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    source = pkg['monitors']['source']['data']
    source.update(source_path='SPECS/aardvark-dns',
                  source_url='https://github.com/openRuyi-Project/openRuyi/tree/main/SPECS/aardvark-dns')
    document = presentation_pages.detail(pkg)
    link = next(value for value in document.links if value.href == source['source_url'])
    assert link.text == '/SPECS/aardvark-dns'


def test_upstream_link_shows_the_address_without_an_indirect_label(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    url = 'https://example.invalid/upstream/project'
    pkg['monitors']['source']['data']['metadata'] = {'url': url}
    document = presentation_pages.detail(pkg)
    link, = [value for value in document.links if value.href == url]
    assert link.text == url


def test_detail_overview_placement_does_not_hide_monitor_evidence(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    pkg['monitors']['source']['data']['metadata'] = {'license': 'MIT', 'description': 'Observed description'}
    sections = presentation_pages.detail(pkg).sections
    placements = {section.id: section.placement for section in sections}
    assert placements['build'] == 'aside'
    assert placements['source'] == 'lead'
    assert next(section for section in sections if section.id == 'source').title == ''
    assert placements['external_signature'] == 'flow'
    assert placements['checks'] == 'flow'
    assert all(not section.collapsible for section in sections if section.id != 'checks')


def test_repeated_reference_keeps_first_caption_without_rebuilding(monkeypatch):
    url = 'https://example.org/advisory/1'
    first = monitor_model.evidence('FIX', url, 'OSV', url, code='reference')
    later = {**first, 'key': 'Other caption'}
    expected = presentation_evidence.fact_fields([first])
    original = presentation_evidence.text
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(presentation_evidence, 'text', counted)
    assert presentation_evidence.fact_fields([first, *([later] * 100)]) == expected
    assert len(calls) == 1
