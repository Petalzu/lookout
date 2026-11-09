"""Render-test bridge: use the real Python presenter, not a JS copy of its rules."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.presentation import pages as presentation_pages
from tracker.monitors.requires.model import RequirementAssessment
from tracker.api import ListingQuery
from tracker.readmodel.packages import PackageList
from tracker.readmodel.monitors import retained_dimensions
from tracker.monitors.model import check_failed

request = json.load(sys.stdin)
operation = request['operation']
packages = ([request['payload']] if operation == 'detail' else
            request['payload'].get('items', []) if operation == 'list' else [])
for package in packages:
    for monitor in package['monitors'].values():
        if monitor['data']['kind'] == 'requires':
            # SSR fixtures must supply the actual current/target assessment contract.
            monitor['data']['requirements'] = [RequirementAssessment.model_validate(item).model_dump()
                                               for item in monitor['data']['requirements']]
if operation == 'detail':
    result = presentation_pages.detail(request['payload']).model_dump()
elif operation == 'list':
    payload = request['payload']
    for package in packages:
        for mid, monitor in package['monitors'].items():
            data = monitor['data']
            dimensions = monitor.setdefault('dimensions', {})
            dimensions['check:' + mid] = [monitor['check']['status']]
            if check_failed(monitor['check']):
                dimensions.setdefault('maintenance', []).append('CheckFailed')
            if data['kind'] == 'source':
                dimensions['buildsystem'] = [data['buildsystem'] or '_not_detected']
            elif data['kind'] == 'build':
                dimensions.update({'build:' + t['target']: [t['raw_status']] for t in data['targets']})
            elif data['kind'] == 'evidence':
                dimensions.update({'findings:' + mid: ['yes'] if data['findings'] else [],
                    'maintenance': [label['label'] for label in data['labels']]})
            elif data['kind'] == 'activity':
                dimensions['maintenance'] = [label['label'] for label in data['labels']]
            elif data['kind'] == 'requires':
                dims = []
                if any(r['satisfaction'] == 'unsatisfied' or r['target_satisfaction'] == 'unsatisfied' for r in data['requirements']):
                    dims.append('unmet')
                if any(r['changed'] for r in data['requirements']):
                    dims.append('changes')
                dimensions.update({'requires': dims, 'findings:' + mid: ['yes'] if data['requirements'] else [],
                                   'maintenance': (['DepMismatch'] if 'unmet' in dims else []) +
                                       (['DepChanges'] if 'changes' in dims else [])})
            elif data['kind'] == 'version':
                dimensions['version_signal'] = [a['monitor'] for a in data['annotations']]
                if not data['track']:
                    monitor['check']['status'] = 'not_configured'
                    dimensions['check:' + mid] = ['not_configured']
        retained_dimensions(package['monitors'])
    filters = ListingQuery.from_parameters(request['query'])
    focus = next((m for m in payload['monitors'] if m['id'] == filters.monitor), None)
    section = 'coverage' if focus and focus['kind'] == 'source' else filters.section
    selection = PackageList(packages, payload['targets']).select(query=filters.q,
        filters=filters.filters, page=filters.page, per_page=filters.per_page,
        monitor=filters.monitor, findings_only=bool(focus and focus['kind'] in ('evidence', 'requires') and section == 'results'))
    result = presentation_pages.listing({**payload, **selection, 'section': section,
        'monitor_coverage': PackageList(packages, payload['targets']).monitor_coverage()},
        filters.model_copy(update={'section': section, 'page': selection['page']}).model_dump(exclude_defaults=True)).model_dump()
elif operation == 'activity':
    from tracker.presentation.activity import section
    result = section(request['payload']['name'], request['payload']['kind'], request['payload']['page']).model_dump()
elif operation == 'commits':
    from tracker.presentation.commits import history_section
    result = history_section(request['payload']['name'], request['payload']['page'],
                             status=request['payload'].get('status'), rule=request['payload'].get('rule')).model_dump()
elif operation == 'theme':
    result = presentation_pages.theme(request['payload'])
else:
    raise ValueError(operation)
json.dump(result, sys.stdout)
