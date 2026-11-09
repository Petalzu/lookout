"""Indexed facts shared by result rows and facet counts."""
from collections import defaultdict
from types import MappingProxyType

from tracker.monitors import model as monitor_model
from tracker.monitors.build import status as build_status
from tracker.monitors.issues import Issue
from tracker.readmodel.query import Condition, Evaluation, FilterQuery

VIEWS = ('all', 'updates', 'problems', 'attention', 'untracked')


def _search_values(value):
    """Index observation values, never field names or internal facet dimensions."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _search_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _search_values(child)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value).casefold()


class PackageList:
    def __init__(self, rows, targets, query='', *, monitor='', previous=None):
        self.rows = tuple(rows)
        self.by_name = MappingProxyType({row['name']: row for row in self.rows})
        self.names = tuple(row['name'].casefold() for row in self.rows)
        # Only identical monitor objects may reuse search text: check timestamps
        # are searchable too, even when the corresponding facets did not change.
        prior = previous.by_name if previous else {}
        prior_text = dict(zip(previous.by_name, previous.observations)) if previous else {}
        self.observations = tuple({mid: prior_text[row['name']][mid]
            if result is prior.get(row['name'], {}).get('monitors', {}).get(mid)
            else '\n'.join(_search_values({
                'check': result.get('check', {}), 'data': result.get('data', {})}))
            for mid, result in row['monitors'].items()} for row in self.rows)
        self.all = frozenset(range(len(self.rows)))
        self.query = query
        self.monitor = monitor
        if (previous and tuple(self.by_name) == tuple(previous.by_name)
                and {f'build:{t["id"]}' for t in targets}
                    == {dimension for dimension in previous.index if dimension.startswith('build:')}
                and all(row['monitors'].keys() == old['monitors'].keys()
                        and all(module['dimensions'] == old['monitors'][mid]['dimensions']
                                for mid, module in row['monitors'].items())
                        for row, old in zip(self.rows, previous.rows))):
            self.index = previous.index
            return
        index = defaultdict(lambda: defaultdict(set))
        for key in ('view', 'buildsystem', 'maintenance', 'requires', 'version_signal', *(f'build:{t["id"]}' for t in targets)):
            index.setdefault(key, defaultdict(set))
        for number, row in enumerate(self.rows):
            index['view']['all'].add(number)
            for mid, module in row['monitors'].items():
                for family in ('check', 'retained', 'findings'):
                    dimension = family + ':' + mid
                    if dimension not in index:
                        index[dimension] = defaultdict(set)
                for dimension, values in module['dimensions'].items():
                    for value in values:
                        index[dimension][value].add(number)
        for dimension, options in index.items():
            if dimension.startswith('check:'):
                for group, statuses in monitor_model.CHECK_GROUPS.items():
                    options[group].update(set().union(*(options.get(status, ()) for status in statuses)))
        self.index = MappingProxyType({dimension: MappingProxyType({
            value: frozenset(members) for value, members in options.items()
        }) for dimension, options in index.items()})

    def monitor_coverage(self):
        """Package counts from the same indexed facts used by linked filters."""
        return {dimension.removeprefix('check:'): {
            status: len(members) for status, members in options.items()
            if status not in monitor_model.CHECK_GROUPS and members
        } for dimension, options in self.index.items() if dimension.startswith('check:')}

    def select(self, *, filters=FilterQuery(), page=1, per_page=100,
               query=None, monitor=None, findings_only=False, search='name'):
        query = (self.query if query is None else query).strip().casefold()
        monitor = self.monitor if monitor is None else monitor
        scope = {number for number, name in enumerate(self.names) if query in name} if query else self.all
        if query and search == 'observations':
            scope = scope | {number for number, observations in enumerate(self.observations)
                             if any(query in text for mid, text in observations.items()
                                    if not monitor or mid == monitor)}
        coverage = Evaluation(self.index, scope, filters)
        findings = self.index.get('findings:' + monitor, {}).get('yes', frozenset())
        selection = (Evaluation(self.index, scope & findings, filters)
                     if findings_only else coverage)
        selected = sorted(selection.matches)
        total = len(selected)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)

        def counts(dimension, required=(), *, evaluation=selection):
            values = self.index.get(dimension, {}).keys() | set(required)
            values |= {c.value for c in filters.conditions if c.dimension == dimension}
            return {value: evaluation.count(Condition(dimension=dimension, value=value))
                    for value in sorted(values)}

        statuses = {}
        for dimension in self.index:
            if dimension.startswith('build:'):
                options = counts(dimension)
                statuses[dimension.removeprefix('build:')] = [
                    {'value': value, 'label': build_status.label(value), 'count': options[value]}
                    for value in build_status.ordered(options)
                ]
        checks = counts('check:' + monitor, monitor_model.CHECK_GROUPS, evaluation=coverage) if monitor else {}
        return {
            'filters': filters,
            'items': [self.rows[number] for number in selected[(page - 1) * per_page:page * per_page]],
            'total': total, 'page': page, 'per_page': per_page, 'pages': pages,
            'counts': {**counts('view', required=VIEWS), 'all': total},
            'buildsystems': counts('buildsystem'),
            'maintenance_labels': counts('maintenance', required=(*Issue, 'EOL')),
            'version_signals': counts('version_signal', required=('requires',)),
            'requires_counts': {'all': total, **counts('requires', required=('unmet', 'changes'))},
            'commits_counts': {key: counts('commits:' + key) for key in ('status', 'repository', 'rule')},
            'build_statuses': statuses,
            'check_statuses': {k: v for k, v in checks.items() if k not in monitor_model.CHECK_GROUPS},
            'check_groups': {group: checks.get(group, 0) for group in monitor_model.CHECK_GROUPS},
            'result_count': len(coverage.matches & findings), 'coverage_count': len(coverage.matches),
            'retained_count': selection.count(Condition(dimension='retained:' + monitor, value='yes')) if monitor else 0,
        }
