"""Server-owned filter edits and links; the browser only renders/navigates."""

from tracker.monitors.build import status as build_status
from tracker.monitors.issues import VERSION_ISSUES, Issue
from tracker.presentation.commits import TITLES as COMMIT_TITLES
from tracker.presentation.labels import appearance, caption
from tracker.presentation.model import (
    Choice,
    Controls,
    Navigation,
    Parameter,
)
from tracker.presentation.registry import presenter
from tracker.presentation.values import CHECK_LABELS
from tracker.presentation.version import signal_title
from tracker.readmodel.query import Condition, FilterQuery, encode_parameters


class Links:
    def __init__(self, query=None):
        self.query = {key: value for key, value in (query or {}).items() if value or key == 'filters'}
        self.filters = FilterQuery.model_validate(self.query.get('filters', {}))
        self.query['filters'] = self.filters.model_dump()

    def to(self, **changes):
        query = {**self.query, 'page': None, **changes}
        if query.get('page') == 1:
            del query['page']
        filters = FilterQuery.model_validate(query.pop('filters', {}))
        parameters = [(key, value) for key, value in query.items() if value]
        return '/?' + encode_parameters(parameters + filters.parameters())

    def selected(self, dimension, value):
        return self.filters.selected(dimension, value)

    def toggle(self, dimension, value, **changes):
        try:
            filters = self.filters.toggle(dimension, value)
        except ValueError:
            return None
        return self.to(filters=filters.model_dump(), **changes)

    def condition(self, dimension, value, **changes):
        return self.to(filters=self.filters.select(dimension, (value,)).model_dump(), **changes)


def listing_query(query, catalog):
    # View selection changes columns, never silently drops an explicit predicate.
    focus = next((m for m in catalog if m['id'] == query.get('monitor')), None)
    if focus and not presenter(focus).has_results:
        return {**query, 'section': 'coverage'}
    return query


def global_navigation(payload, links):
    styles = payload.get('presentation', {}).get('buildsystems', {})
    return [Navigation(label=caption('Build system'), selection=True, choices=[
        Choice(label=caption('Custom') if value == '_not_detected' else value, count=count,
            selected=links.selected('buildsystem', value),
            appearance='buildsystem:' + value if value != '_not_detected' else None,
            icon=styles.get(value, {}).get('icon') if value != '_not_detected' else None,
            href=links.toggle('buildsystem', value))
        for value, count in sorted(payload['buildsystems'].items(), key=lambda item: item[0] == '_not_detected')])]


def _finding_names(payload, links):
    order = ('Outdated', 'DepMismatch', 'Advisory', 'Yanked', 'EOL', 'KEV', 'LicenseDiff', 'DepChanges')
    counts = payload['maintenance_labels']
    return [name for name in dict.fromkeys((*order, *counts))
        if name not in (Issue.CHECK_FAILED, Issue.UNTRACKED, 'PR', 'Issue')
        and (counts.get(name, 0) or links.selected('maintenance', name))]


def maintenance_navigation(payload, links):
    choices = [Choice(label=caption(name), count=payload['maintenance_labels'].get(name, 0), appearance=appearance(name),
        selected=links.selected('maintenance', name), href=links.toggle('maintenance', name))
        for name in _finding_names(payload, links)]
    return [Navigation(label='Findings', show_label=False, selection=True, choices=choices)] if choices else []


def build_navigation(payload, links):
    return {target['id']: Navigation(label=target['label'], selection=True, choices=[
        Choice(label=item['label'], count=item['count'], appearance=appearance(item['label']),
            selected=links.selected('build:' + target['id'], item['value']),
            href=links.toggle('build:' + target['id'], item['value']))
        for item in payload['build_statuses'][target['id']]]) for target in payload['targets']}


def _condition_label(condition: Condition, payload) -> str:
    dimension, value = condition.identity
    if dimension == 'maintenance':
        return caption(value)
    if dimension == 'buildsystem':
        return caption('Build system') + ': ' + (caption('Custom') if value == '_not_detected' else value)
    if dimension == 'view':
        return caption(VERSION_ISSUES.get(value, value))
    if dimension == 'requires':
        return {'unmet': 'DepMismatch', 'changes': 'DepChanges'}.get(value, value)
    titles = {m['id']: caption(m['title']) for m in payload['monitors']}
    if dimension == 'version_signal':
        return titles.get(value, value)
    kind, _, identifier = dimension.partition(':')
    if kind == 'build':
        target = next((t['label'] for t in payload['targets'] if t['id'] == identifier), identifier)
        return target + ': ' + build_status.label(value)
    if kind in ('check', 'retained'):
        return titles.get(identifier, identifier) + ': ' + (
            'Retained' if kind == 'retained' else
            'Failed checks' if value == 'failed' else CHECK_LABELS.get(value, value.title()))
    return dimension + ': ' + value


def listing_controls(payload, query, focus, links):
    rows, navigation, visible = [], [], set()

    def choice(dimension, value, label, count, **changes):
        visible.add((dimension, value))
        return Choice(label=caption(label), count=count, appearance=appearance(label),
            selected=links.selected(dimension, value), href=links.toggle(dimension, value, **changes))

    if not focus:
        rows.extend(maintenance_navigation(payload, links))
        visible.update(('maintenance', name) for name in _finding_names(payload, links))
    else:
        views = []
        if presenter(focus).has_results:
            views.append(Choice(label='Results',
                count=payload['result_count'] if focus['kind'] in ('evidence', 'requires', 'commits') else payload['coverage_count'],
                selected=payload['section'] == 'results', href=links.to(section='results')))
        views.append(Choice(label='Coverage', count=payload['coverage_count'],
            selected=payload['section'] == 'coverage', href=links.to(section='coverage')))
        navigation.append(Navigation(label=caption(focus['title']), choices=views))
        if payload['section'] == 'results':
            if focus['kind'] == 'version':
                rows.append(Navigation(label='Version', selection=True, choices=[
                    choice('maintenance', label, label, payload['maintenance_labels'].get(label, 0))
                    for label in VERSION_ISSUES.values()]))
                related = [choice('version_signal', m['id'], signal_title(m), payload['version_signals'][m['id']])
                    for m in payload['monitors'] if m['id'] in payload['version_signals']]
                if related:
                    rows.append(Navigation(label='Related', selection=True, choices=related))
            elif focus['kind'] == 'commits':
                for dimension, title in (('status', 'Inclusion'), ('repository', 'Upstream'), ('rule', 'Rule')):
                    choices = [choice('commits:' + dimension, value,
                                      COMMIT_TITLES.get(value, value) if dimension == 'status' else value, count)
                               for value, count in payload.get('commits_counts', {}).get(dimension, {}).items()
                               if count or links.selected('commits:' + dimension, value)]
                    if choices:
                        rows.append(Navigation(label=title, selection=True, choices=choices))
            elif focus['kind'] == 'requires':
                rows.append(Navigation(label='Dependencies', selection=True, choices=[
                    choice('maintenance', label, label, payload['maintenance_labels'].get(label, 0))
                    for label in (Issue.DEP_MISMATCH, 'DepChanges')]))
        checks = [choice('check:' + focus['id'], status, CHECK_LABELS.get(status, status.title()),
            count, section='coverage') for status, count in payload['check_statuses'].items()
            if count or links.selected('check:' + focus['id'], status)]
        if checks:
            rows.append(Navigation(label='Checks', selection=True, choices=checks))
        if payload.get('retained_count') or links.selected('retained:' + focus['id'], 'yes'):
            rows.append(Navigation(label='Observations', selection=True, choices=[
                choice('retained:' + focus['id'], 'yes', 'Retained', payload['retained_count'])]))
    hidden = [Parameter(name=key, value=str(query[key])) for key in ('per_page',) if query.get(key)]
    active = [Choice(label='Search: ' + query['q'], href=links.to(q=''))] if query.get('q') else []
    active.extend(Choice(label=_condition_label(c, payload), href=links.toggle(*c.identity))
        for c in links.filters.conditions if c.identity not in visible)
    return Controls(query=query.get('q', ''), hidden=hidden, choice_rows=rows, navigation=navigation,
        active=active, reset=links.to(filters={}))
