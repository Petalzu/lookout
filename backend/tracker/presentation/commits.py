"""Historical commit evidence displayed through the shared document primitives."""
from urllib.parse import quote, urlencode

from tracker.presentation.model import Cell, Entry, Section
from tracker.presentation.values import field, stamp, text

TITLES = {
    'original_reachable': 'Original commit in target history',
    'strict_patch_evidence': 'Strict equivalent-patch evidence',
    'backport_needs_review': 'Backport association; review required',
    'no_evidence_in_range': 'No evidence in the declared range',
    'unknown': 'Not determined',
}
REASONS = {
    'no_candidate_release': 'No comparable newer release',
    'target_unresolved': 'Target source identity is not established',
    'pending': 'Analysis pending',
    'shallow_history': 'Required history is incomplete',
    'association_only': 'Reference or patch match is not strict equivalence',
    'commit_limit': 'Target-history budget reached',
    'tag_drift': 'Release tag disagrees with the reviewed object',
    'unsupported_commit_shape': 'Outside the strict method support',
    'strict_manifest_limit': 'Strict verification manifest budget reached; equivalence not confirmed',
}


def cells(_pkg, result, _links):
    data = result['data']
    return [Cell(lines=[[text(f"{data['selected']} / {data['total']} candidates")],
                        [text({'ok': 'Checked', 'partial': 'Partial', 'error': 'Refresh failed'}.get(
                            result['check']['status'], result['check']['status'].replace('_', ' ').title()),
                            tone='notice' if result['check']['stale'] else 'muted')]])]


def _check_fields(check):
    fields = [field('Collection', text(check['status'].replace('_', ' ').title(),
                                      tone='notice' if check.get('stale') else 'muted')),
              field('Configuration', text('Configured' if check.get('configured') else 'Not configured'))]
    if check.get('error'):
        fields.append(field('Refresh error', text(check['error'], kind='code', tone='notice')))
    fields.extend(field('Analysis failure', text(failure, kind='code', tone='notice'))
                  for failure in check.get('failures', []))
    if check.get('checked_at'):
        fields.append(field('Observed', stamp(check['checked_at'])))
    if check.get('attempted_at'):
        fields.append(field('Last attempt', stamp(check['attempted_at'])))
    return fields


def _manifest_fields(manifest):
    changes = [manifest['candidate'], *manifest['matches']]
    fields = [field('Strict verification', text(f"Saved manifest v{manifest['version']}; "
        f"{sum(len(change['files']) for change in changes)} file records. "
        'Complete raw-path, mode and content-digest audit is available in the commit fact API.'))]
    for change in changes[:2]:
        fields.append(field('Verified change', text(change['parent_oid'] + ' → ' + change['oid'], kind='code')))
        for item in change['files'][:2]:
            states = [item['before'], item['after']]
            description = ' → '.join(value['mode'] + ': ' + (value['sha256'] or 'absent') for value in states)
            fields.append(field('Raw path (base64)', text(item['path_b64'], kind='code')))
            fields.append(field('File SHA-256', text(description, kind='code')))
    return fields


def sections(result, _links):
    data = result['data']
    entries = []
    for item in data['entries'][:20]:
        fields = [field('Rule', text(', '.join(sorted({r['rule'] for r in item['reasons']}))))]
        fields.extend(field('Matched ' + reason['field'], text(reason['fragment'], kind='code'))
                      for reason in item['reasons'][:8])
        if item.get('reasons_truncated') or len(item['reasons']) > 8:
            fields.append(field('Matches', text('Additional match reasons are available in the fact API.', tone='muted')))
        if item.get('reasons_truncated'):
            fields.append(field('Match coverage', text('Match reasons truncated.', tone='notice')))
        if item.get('stale'):
            fields.append(field('Validity', text('Saved input changed or evidence is stale; refresh required.',
                                               tone='notice')))
        for side in ('current', 'upgrade'):
            evidence = item['sides'][side]
            target = evidence.get('target')
            reason = evidence.get('reason')
            label = REASONS['no_candidate_release'] if reason == 'no_candidate_release' else TITLES[evidence['status']]
            fields.append(field('Current' if side == 'current' else 'Candidate release', text(label)))
            if reason and reason != 'no_candidate_release':
                fields.append(field('Explanation', text(REASONS.get(reason, reason.replace('_', ' ')), tone='muted')))
            if target:
                fields.append(field('Target', text(target.get('version') or '', kind='code'),
                                    text(' · ', tone='muted'), text(target['oid'], kind='code')))
            if target and target.get('source_url'):
                fields.append(field('Source identity', text(target['source_url'], href=target['source_url'])))
            if target and target.get('review_url'):
                fields.append(field('Reviewed mapping', text(target['review_url'], href=target['review_url'])))
            if evidence.get('matches'):
                base = item['url'].rsplit('/commit/', 1)[0]
                fields.append(field('Evidence commits',
                    *[text(value, kind='code', href=base + '/commit/' + value) for value in evidence['matches']]))
            if evidence.get('strict_manifest'):
                fields.extend(_manifest_fields(evidence['strict_manifest']))
            coverage = evidence.get('coverage', {})
            if coverage.get('start'):
                fields.append(field('Checked history', text(coverage['start'][:12] + ' … ' +
                    (coverage.get('target') or '')[:12], kind='code')))
            if evidence.get('checked_at'):
                fields.append(field('Observed', stamp(evidence['checked_at'])))
            if evidence.get('retained'):
                fields.append(field('Refresh', text('Retained evidence; refresh failed', tone='notice')))
            if evidence.get('reverts'):
                fields.append(field('Revert clues', text(', '.join(evidence['reverts']), kind='code')))
        entries.append(Entry(heading=[text(item['subject'], href=item['url'])],
                             metadata=[text(item['oid'][:12], kind='code'), stamp(item.get('committed_at'))], fields=fields))
    return [Section(id='commits', title=f"Upstream commits · {data['selected']} / {data['total']}",
                    fields=[*_check_fields(result['check']), field('Scope', text('Published candidate subset; not a functional fix assessment.')),
                            field('Coverage', text('Complete declared window' if data.get('coverage', {}).get('complete')
                                  else 'Partial or bounded evidence; inspect the fact API for exact ranges.', tone='muted'))],
                    entries=entries, more=data.get('history_url') if len(data['entries']) > 20 else None)]


def history_section(name, page, *, status=None, rule=None):
    rendered = sections({'check': page['check'], 'data': {'entries': page['items'], 'selected': len(page['items']),
                                 'total': page['total'], 'coverage': page.get('coverage', {})}}, None)[0]
    if page['next_cursor']:
        params = {'cursor': page['next_cursor']}
        if status:
            params['status'] = status
        if rule:
            params['rule'] = rule
        rendered.more = '/packages/' + quote(name, safe='') + '/commits?' + urlencode(params)
    return rendered
