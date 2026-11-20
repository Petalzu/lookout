"""Pages for reading documents; no collection or persistence."""
from tracker.monitors.issues import Issue
from tracker.monitors.model import check_failed
from tracker.presentation.activity import open_links
from tracker.presentation.build import build_note
from tracker.presentation.evidence import evidence_labels
from tracker.presentation.labels import appearance, caption, palettes, priority, tag_palette
from tracker.presentation.model import (
    Choice,
    Column,
    DetailDocument,
    Field,
    ListingDocument,
    Navigation,
    Row,
    RowNote,
    Section,
    Table,
)
from tracker.presentation.navigation import (
    Links,
    build_navigation,
    global_navigation,
    listing_controls,
    listing_query,
)
from tracker.presentation.registry import PRESENTERS, presenter
from tracker.presentation.source import changelog_section, repository_link
from tracker.presentation.status import data_status
from tracker.presentation.values import (
    buildsystem,
    cell,
    check_diagnostics,
    check_value,
    field,
    module,
    stamp,
    text,
    version_parts,
)
from tracker.presentation.version import version_annotations


def collection_meta(collection):
    result = [Field(label='BuildService', href=collection.get('build_service_url'),
                    values=[stamp(collection['obs_updated_at'])])]
    source = collection.get('source_repository')
    if not source:
        return result
    branch_url = repository_link(source['url'], 'tree', source['branch'])
    commit_url = repository_link(source['url'], 'commit', source['revision'])
    result.append(Field(label=source['branch'], href=branch_url,
        values=[text(source['revision'][:6], kind='code', href=commit_url, title=source['revision'])]))
    return result


def identity_cell(pkg, links, *, labels=True):
    identity = [text(pkg['name'], href=pkg['detail_url'], kind='code')]
    signals = []
    if labels:
        signals.extend(check_failed_label(pkg, exclude_kind='version'))
        for result in pkg['monitors'].values():
            if result['data']['kind'] in ('evidence', 'requires'):
                signals.extend(evidence_labels(result, links, counts=True))
    source = module(pkg, 'source')
    if source and source['data'].get('buildsystem'):
        identity.append(buildsystem(source['data']['buildsystem'], links))
    identity.extend(sorted(signals, key=lambda value: priority(value.appearance)))
    activity = module(pkg, 'activity')
    if labels and activity:
        identity.extend(open_links(pkg, activity))
    return cell(identity)


def check_failed_label(pkg, *, exclude_kind=None):
    errors = [result for result in pkg['monitors'].values()
              if result['data']['kind'] != exclude_kind and check_failed(result['check'])]
    if not errors:
        return []
    anchor = 'check-' + errors[0]['id'] if len(errors) == 1 else 'checks'
    return [text(Issue.CHECK_FAILED, kind='tag', appearance=appearance(Issue.CHECK_FAILED),
                 title=', '.join(caption(result['title']) for result in errors),
                 href=pkg['detail_url'] + '#' + anchor)]


def listing(payload, query):
    query = listing_query(query, payload['monitors'])
    focus = next((m for m in payload['monitors'] if m['id'] == query.get('monitor')), None)
    payload = {**payload, 'section': query.get('section', 'results')}
    links = Links(query)
    title = caption(focus['title']) if focus else 'Packages'
    navigation = Navigation(label='Monitors', choices=[
        Choice(label='Packages', href=links.to(monitor='', section=''), selected=not focus),
        *[Choice(label=caption(m['title']), href=links.to(monitor=m['id'], section='results'),
                 selected=m == focus) for m in sorted(payload['monitors'], key=lambda m: m['id'] == 'eol')
          if presenter(m).has_results and m['id'] != 'yanked']])
    table = (_coverage_table(payload, focus, links, title)
             if focus is not None and payload['section'] == 'coverage'
             else _results_table(payload, focus, links, title))
    table.columns[0].navigation = global_navigation(payload, links)
    controls = listing_controls(payload, query, focus, links)
    pagination = []
    if payload['page'] > 1:
        pagination.append(Choice(label='Previous', href=links.to(page=payload['page'] - 1)))
    if payload['page'] < payload['pages']:
        pagination.append(Choice(label='Next', href=links.to(page=payload['page'] + 1)))
    meta = [] if focus else collection_meta(payload['collection'])
    notices = ['Test fixture — not live openRuyi data.'] if payload['collection']['mode'] == 'fixture' else []
    return ListingDocument(title=title, navigation=navigation, controls=controls,
        table=table, total=payload['total'], page=payload['page'],
        pages=payload['pages'], pagination=pagination, meta=meta,
        data_status=data_status(payload['collection'], payload.get('monitor_coverage', {}), payload['monitors']),
        notices=notices)


def _coverage_table(payload, focus, links, title):
    filtered = any(c.dimension == 'check:' + focus['id']
                   for c in links.filters.conditions)
    reasons = {pkg['name']: check_diagnostics(pkg['monitors'][focus['id']]['check'])
               for pkg in payload['items']} if filtered else {}
    show_reason = any(reasons.values())
    columns = [Column(title='Package', role='identity')]
    if filtered:
        columns.append(Column(title='Version'))
        if show_reason:
            columns.append(Column(title='Reason'))
    else:
        columns += [Column(title='Check'), Column(title='Last checked')]
    rows = []
    for pkg in payload['items']:
        cells = [identity_cell(pkg, links, labels=False)]
        if filtered:
            source = module(pkg, 'source')
            cells.append(cell([text(source['data'].get('version') if source else None, kind='code')]))
            if show_reason:
                cells.append(cell(*([text(message, tone='notice')] for message in reasons[pkg['name']])))
        else:
            check = pkg['monitors'][focus['id']]['check']
            cells += [cell([check_value(check)]), cell([stamp(check.get('checked_at'))])]
        rows.append(Row(key=pkg['name'], cells=cells))
    return Table(label=title, columns=columns, rows=rows)


def _results_table(payload, focus, links, title):
    descriptors = [focus] if focus else [m for m in payload['monitors'] if m['kind'] in ('version', 'build')]
    columns = [Column(title='Package', role='identity')]
    for descriptor in descriptors:
        if not focus and descriptor['kind'] == 'version':
            columns.extend([Column(title='Current'), Column(title='Candidate')])
            continue
        columns_for = presenter(descriptor).columns
        assert columns_for is not None
        added = columns_for(caption(descriptor['title']), payload['targets'])
        if descriptor['kind'] == 'build':
            menus = build_navigation(payload, links)
            for target, column in zip(payload['targets'], added, strict=True):
                column.navigation = [menus[target['id']]]
        columns.extend(added)
    return Table(label=title, columns=columns,
                 rows=[_result_row(pkg, descriptors, focus is None, links) for pkg in payload['items']])


def _result_row(pkg, descriptors, aggregate, links):
    cells = [identity_cell(pkg, links, labels=aggregate)]
    notes = []
    for descriptor in descriptors:
        result = pkg['monitors'][descriptor['id']]
        column = len(cells)
        if aggregate and descriptor['kind'] == 'version':
            current, candidate, annotations = version_parts(pkg, links=links)
            cells.extend([cell(current), cell(candidate + annotations)])
            for related in sorted(pkg['monitors'].values(), key=lambda m: m['id'] == 'eol'):
                preview = presenter({'kind': related['data']['kind']}).preview
                if preview:
                    notes.extend(RowNote(column=0, span=column + 2, values=line)
                                 for line in preview(pkg, related, links))
        else:
            cells_for = presenter(descriptor).cells
            assert cells_for is not None
            cells.extend(cells_for(pkg, result, links))
        if aggregate and descriptor['kind'] == 'build':
            notes.extend(build_note(pkg, result, column))
    return Row(key=pkg['name'], cells=cells, notes=notes)


def detail(pkg, collection=None):
    links = Links()
    source = module(pkg, 'source')
    data = source['data'] if source else {}
    meta = data.get('metadata') or {}
    identity = [buildsystem(data['buildsystem'], links)] if data.get('buildsystem') else []
    signals = version_annotations(pkg) + check_failed_label(pkg, exclude_kind='version')
    requirements = module(pkg, 'requires')
    if requirements:
        signals.extend(evidence_labels(requirements, links, counts=True))
    identity.extend(sorted(signals, key=lambda value: priority(value.appearance)))
    shortcuts = [text('/' + data['source_path'], href=data.get('source_url'))] if data.get('source_path') else []
    if meta.get('url'):
        shortcuts.append(text(meta['url'], href=meta['url']))
    # Composition order is a reader concern; collectors never encode it.
    results = sorted(pkg['monitors'].values(), key=lambda m: (m['id'] == 'eol', {'build': 0, 'evidence': 1, 'requires': 1, 'version': 2, 'source': 3, 'activity': 4, 'commits': 1}[m['data']['kind']]))
    sections, context = [], []
    for result in results:
        kind = result['data']['kind']
        adapter = PRESENTERS[kind]
        destination = sections if adapter.has_results else context
        rendered = adapter.sections(result, links)
        if kind == 'version':
            current, candidate, annotations = version_parts(pkg)
            fields = [field('Current', *current)]
            if candidate or annotations:
                fields.append(field('Candidate', *candidate, *annotations))
            if not rendered:
                rendered = [Section(id=result['id'], title='')]
            rendered[0].placement = 'lead'
            rendered[0].title = ''
            rendered[0].fields = fields + rendered[0].fields
        destination.extend(rendered)
    checks = []
    for result in pkg['monitors'].values():
        check = result['check']
        status_lines = [[check_value(check)]]
        diagnostics = check_diagnostics(check)
        status_lines.extend([text(message, tone='notice' if check_failed(check) else 'muted')]
                            for message in diagnostics if message)
        timestamps = [[stamp(check.get('checked_at'))]]
        if check.get('attempted_at') and check['attempted_at'] != check.get('checked_at'):
            timestamps.append([text('Last attempted', tone='muted'), stamp(check['attempted_at'])])
        checks.append(Row(key=result['id'], id='check-' + result['id'], cells=[cell([text(caption(result['title']),
            href=links.condition('check:' + result['id'], check['status'], monitor=result['id'], section='coverage'))]),
            cell(*status_lines), cell(*timestamps)]))
    sections.extend(context)
    if source:
        sections.extend(changelog_section(source, (collection or {}).get('source_repository')))
    context = []
    sections.append(Section(id='checks', title='Checks', collapsible=True, table=Table(label='Collection checks', columns=[
        Column(title='Monitor'), Column(title='Observation'), Column(title='Last checked')], rows=checks)))
    return DetailDocument(title=pkg['name'], subtitle=meta.get('summary'), identity=identity,
                          links=shortcuts, context=context, sections=sections)


def theme(presentation):
    return {'appearances': {**palettes(), **{'buildsystem:' + key: colors
            for key, value in presentation.get('buildsystems', {}).items() if (colors := tag_palette(value['background'])) is not None}}}
