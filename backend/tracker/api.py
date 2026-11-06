"""Read-only REST. HTTP processes never import or invoke the collector."""
import os
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, get_origin

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tracker import state
from tracker.monitors.model import RawFinding
from tracker.monitors.requires.model import RequirementAssessment
from tracker.presentation import activity as activity_presentation
from tracker.presentation import navigation as presentation_navigation
from tracker.presentation import pages as presentation_pages
from tracker.presentation.model import DetailDocument, DocumentTheme, ListingDocument, Section
from tracker.readmodel import activity, commits
from tracker.readmodel import monitors as monitor_views
from tracker.readmodel import snapshot as view
from tracker.readmodel.cache import ProjectionCache
from tracker.readmodel.query import MAX_QUERY_NODES, FilterQuery


# Fixed-shape payloads are typed so the response contract cannot silently drift.
# Raw provenance (source, upstream, per-flavor facts) stays open on purpose.
class Target(BaseModel):
    id: str
    label: str
    repository: str
    architecture: str

class LastSuccess(BaseModel):
    version: str | None
    time: str
    srcmd5: str | None

class Build(BaseModel):
    target: str
    label: str
    repository: str
    architecture: str
    raw_status: str
    text: str
    kind: str
    issue: bool
    log_url: str | None
    stale: bool
    matches_source: bool | None
    last_success: LastSuccess | None
    updated_at: str | None

class BuildFlavor(BaseModel):
    model_config = ConfigDict(extra='allow')  # raw OBS provenance remains available
    package: str
    raw_status: str
    text: str
    kind: str
    issue: bool
    log_url: str | None
    stale: bool
    updated_at: str | None
    matches_source: bool | None
    last_success: LastSuccess | None

class BuildDetail(Build):
    flavors: list[BuildFlavor]

class SourceRepository(BaseModel):
    url: str
    branch: str
    revision: str


class Collection(BaseModel):
    obs_updated_at: str | None
    upstream_updated_at: str | None
    last_attempt: str | None
    mode: str
    errors: list[str]
    generation: int
    packages: int
    tracked_packages: int
    projection_notice: str | None = None
    build_service_url: str | None = None
    source_repository: SourceRepository | None = None

class SpecMetadata(BaseModel):
    name: str | None
    version: str | None
    summary: str | None
    license: str | None
    url: str | None
    description: str | None
    buildsystem: str | None = None

class Appearance(BaseModel):
    background: str
    foreground: str
    icon: str | None = None

class Presentation(BaseModel):
    buildsystems: dict[str, Appearance] = {}

class MaintenanceLabel(BaseModel):
    label: str
    count: int
    stale: bool

class Finding(RawFinding):
    monitor: str
    stale: bool


class ChangelogEntry(BaseModel):
    commit: str
    date: str
    author: str
    subject: str
    signed_off_by: list[str]

class Spec(BaseModel):
    source_path: str
    source_url: str | None
    metadata: SpecMetadata | None
    changelog: list[ChangelogEntry]
    head: str | None
    error: str | None


class Watch(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str
    version: str | None = None
    error: str | None = None
    fetched_at: str | None = None
    stale: bool


class BuildStatusOption(BaseModel):
    value: str
    label: str
    count: int


# Facts and display documents are separate projections of the same snapshot.
class ObservationCheck(BaseModel):
    status: str
    stale: bool
    checked_at: str | None = None
    attempted_at: str | None = None
    error: str | None = None
    note: str | None = None
    changed_at: str | None = None
    evidence_revision: str | None = None
    failures: list[str] = Field(default_factory=list, description='Failed collection subchecks; partial evidence remains usable.')


class SourceSummary(BaseModel):
    kind: Literal['source']
    version: str | None
    buildsystem: str | None
    buildsystem_status: Literal['declared', 'not_declared', 'unknown']


class SourceObservation(SourceSummary, Spec):
    revision: str | None
    obs: dict


class VersionAnnotation(BaseModel):
    monitor: str = Field(description='Stable owning monitor ID; also the version signal filter value.')
    label: str
    count: int = Field(ge=1, description='Distinct related findings, or changed dependency declarations.')
    scope: Literal['current', 'upgrade']
    target_version: str | None
    stale: bool
    finding_ids: list[str] = Field(description='IDs in the owning monitor observation; evidence is not duplicated.')


class SourceRelease(BaseModel):
    ecosystem: str
    name: str
    version: str
    url: str


class RevisionComparison(BaseModel):
    repository: str
    branch: str
    current: str
    packaged_date: str | None
    latest: str | None
    latest_committed_at: str | None
    links: dict[str, str | None]


class VersionSummary(BaseModel):
    kind: Literal['version']
    current: str | None
    source_release: SourceRelease | None = None
    revision: RevisionComparison | None = None
    latest: str | None
    relation: Literal['current', 'outdated', 'changed', 'ahead', 'unknown', 'untracked', 'not_applicable']
    track: str | None
    track_label: str
    stale: bool
    error: str | None
    last_known_relation: str
    updated_at: str | None
    annotations: list[VersionAnnotation] = []


class VersionObservation(VersionSummary):
    upstream: dict
    watch: list[Watch]


class BuildData[TargetBuild: Build](BaseModel):
    kind: Literal['build']
    targets: list[TargetBuild]
    source_version: str | None
    source_success: bool | None
    last_successful_version: str | None


class BuildSummary(BuildData[Build]):
    pass


class BuildObservation(BuildData[BuildDetail]):
    pass


class EvidenceEntry(BaseModel):
    id: str
    title: str
    evidence_url: str
    stale: bool
    scope: Literal['current', 'upgrade']
    target_version: str | None = None
    tags: list[str] = []


class EvidenceSummary(BaseModel):
    kind: Literal['evidence']
    labels: list[MaintenanceLabel]
    finding_count: int
    entries: list[EvidenceEntry] = []


class EvidenceObservation(EvidenceSummary):
    findings: list[Finding]


class RequiresSummary(BaseModel):
    kind: Literal['requires']
    labels: list[MaintenanceLabel]
    finding_count: int
    current_version: str | None
    target_version: str | None
    requirements: list[RequirementAssessment]


class RequiresObservation(RequiresSummary):
    findings: list[Finding]


class ActivityAssociation(BaseModel):
    kind: str
    value: str


class ActivityItem(BaseModel):
    author: str | None
    id: str
    repository: str
    number: int
    kind: Literal['pr', 'issue']
    title: str
    url: str
    status: Literal['open', 'closed', 'draft', 'merged', 'completed', 'not_planned']
    updated_at: str
    available: bool
    paths_complete: bool
    association: list[ActivityAssociation]


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    total: int
    next_cursor: str | None


class ActivitySummary(BaseModel):
    kind: Literal['activity']
    labels: list[MaintenanceLabel]


class CommitTarget(BaseModel):
    oid: str
    version: str | None
    revision: str
    mapping_version: int
    ref: str | None = None
    source_url: str | None
    review_url: str | None
    side: Literal['current', 'upgrade']
    source_kind: Literal['upstream']


class CommitReason(BaseModel):
    rule: str
    field: str
    fragment: str
    path: str | None


class CommitSide(BaseModel):
    status: Literal['original_reachable', 'strict_patch_evidence', 'backport_needs_review',
                    'no_evidence_in_range', 'unknown']
    reason: str | None
    matches: list[str]
    reverts: list[str]
    coverage: dict[str, Any]
    checked_at: str | None
    attempted_at: str | None = None
    retained: bool = False
    error: str | None = None
    target: CommitTarget | None
    key: str


class CommitEntry(BaseModel):
    oid: str
    subject: str
    committed_at: str | None
    url: str
    reasons: list[CommitReason]
    reasons_truncated: bool = False
    sides: dict[Literal['current', 'upgrade'], CommitSide]
    stale: bool = False


class CommitSummary(BaseModel):
    kind: Literal['commits']
    history_url: str | None = None
    entries: list[CommitEntry]
    selected: int
    total: int
    batch_id: str | None
    repository: str | None
    coverage: dict[str, Any]
    labels: list[MaintenanceLabel]


class CommitPage(BaseModel):
    coverage: dict[str, Any] = Field(default_factory=dict)
    items: list[CommitEntry]
    total: int
    next_cursor: str | None
    batch_id: str | None


class MonitorDescription(BaseModel):
    id: str
    title: str
    kind: Literal['source', 'version', 'build', 'evidence', 'requires', 'activity', 'commits']


class MonitorSummary(BaseModel):
    id: str
    title: str
    check: ObservationCheck
    data: Annotated[SourceSummary | VersionSummary | BuildSummary | EvidenceSummary | RequiresSummary | ActivitySummary | CommitSummary, Field(discriminator='kind')]


class MonitorObservation(BaseModel):
    id: str
    title: str
    check: ObservationCheck
    data: Annotated[SourceObservation | VersionObservation | BuildObservation | EvidenceObservation | RequiresObservation | ActivitySummary | CommitSummary, Field(discriminator='kind')]


class PackageData[Observation: MonitorSummary | MonitorObservation](BaseModel):
    name: str
    detail_url: str
    monitors: dict[str, Observation]


class MonitoredPackage(PackageData[MonitorSummary]):
    pass


class MonitoredObservation(PackageData[MonitorObservation]):
    pass


class MonitoredDetail(MonitoredObservation):
    presentation: Presentation


class MonitoredList(BaseModel):
    items: list[MonitoredPackage | MonitoredObservation]
    monitors: list[MonitorDescription]
    total: int
    page: int
    per_page: int
    pages: int
    filters: FilterQuery
    query_node_limit: int = MAX_QUERY_NODES
    counts: dict[str, int]
    targets: list[Target]
    collection: Collection
    presentation: Presentation
    buildsystems: dict[str, int]
    maintenance_labels: dict[str, int]
    requires_counts: dict[str, int]
    commits_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    version_signals: dict[str, int]
    build_statuses: dict[str, list[BuildStatusOption]]
    check_statuses: dict[str, int]
    check_groups: dict[str, int]
    section: Literal['results', 'coverage']
    result_count: int
    coverage_count: int
    retained_count: int


class MonitorExport(BaseModel):
    schema_version: Literal[2] = Field(2, alias='schema')
    collection: Collection
    targets: list[Target]
    packages: list[MonitoredObservation]


class PackageBatch(BaseModel):
    items: list[MonitoredObservation]
    collection: Collection


class ListingQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    q: str = Field('', max_length=100)
    page: int = Field(1, ge=1, le=1000000)
    per_page: int = Field(100, ge=1, le=200)
    filters: FilterQuery = Field(default_factory=FilterQuery)
    monitor: str = Field('', max_length=64)
    section: Literal['results', 'coverage'] = 'results'

    @classmethod
    def from_parameters(cls, parameters):
        query, remaining = FilterQuery.extract(parameters)
        values: dict[str, Any] = {'filters': query}
        arrays = {name for name, field in cls.model_fields.items() if get_origin(field.annotation) is list}
        for name, value in remaining:
            if name == 'filters':
                raise ValueError('Use repeated select=FACET=VALUE parameters')
            if name in arrays:
                values.setdefault(name, []).append(value)
            else:
                values[name] = value
        parsed = cls.model_validate(values)
        return parsed


FULL_PAGE_LIMIT = 20
MonitorID = Annotated[str, Field(min_length=1, max_length=64)]
PackageName = Annotated[str, Field(min_length=1, max_length=512)]


class MonitorSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    include: list[MonitorID] = Field(default_factory=list, max_length=32,
        description='Repeated monitor IDs to return. Omitted means all; does not change filtering.')


class PackageQuery(ListingQuery, MonitorSelection):
    search: Literal['name', 'observations'] = Field('name',
        description='Case-insensitive substring search. observations searches values in the focused monitor, or all monitors when unfocused; package names always match.')
    detail: Literal['summary', 'full'] = Field('summary',
        description=f'Full observations include evidence and histories; require per_page <= {FULL_PAGE_LIMIT}.')


class BatchQuery(MonitorSelection):
    names: list[PackageName] = Field(min_length=1, max_length=FULL_PAGE_LIMIT,
        description=f'Repeated exact package names, in response order; 1–{FULL_PAGE_LIMIT} unique names. Any missing name fails the whole request.')


def read_query(model, request):
    # Keep repeated facet values; converting to a dict would discard alternatives.
    try:
        return model.from_parameters(request.query_params.multi_items())
    except ValidationError as error:
        errors = [{**item, 'loc': ('query', *item['loc'])} for item in error.errors()]
        raise RequestValidationError(errors) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def listing_parameters(request: Request):
    return read_query(ListingQuery, request)


def package_parameters(request: Request):
    return read_query(PackageQuery, request)


def query_schema(model):
    """Keep HTTP documentation derived from the same validated field definitions."""
    schema = model.model_json_schema()
    parameters = [{'name': name, 'in': 'query', 'schema': field,
        'required': name in schema.get('required', ()),
        **({'description': field['description']} if 'description' in field else {})}
        for name, field in schema['properties'].items() if name != 'filters']
    parameters.append({'name': 'select', 'in': 'query', 'required': False,
        'schema': {'type': 'array', 'items': {'type': 'string'}}, 'style': 'form', 'explode': True,
        'description': f'Repeat select=FACET=VALUE. Values in one facet are OR; facets are AND. '
            f'Omitted facets are unrestricted. Empty values are invalid. Maximum {MAX_QUERY_NODES} selections, including repeats. '
            'Counts apply every other facet and the search text. Example: select=maintenance=Advisory&select=build:rva23=failed.'})
    return {'parameters': parameters, 'responses': {422: {'description': 'Invalid query'}}}


def selected_monitors(requested, catalog):
    known = {module['id'] for module in catalog}
    if set(requested) - known:
        raise HTTPException(422, 'Unknown included monitor')
    return set(requested) if requested else known


def package_response(row, included, *, full, focus=''):
    selected = {**row, 'monitors': {mid: result for mid, result in row['monitors'].items() if mid in included}}
    if full:
        return MonitoredObservation.model_validate(selected)
    return MonitoredPackage.model_validate(view.monitor_summary(selected, focus))


def create_app(db=None):
    db = Path(db or os.environ.get('TRACKER_DB', 'state/tracker.sqlite3'))
    cache = ProjectionCache(db)

    @asynccontextmanager
    async def lifespan(app):
        worker = threading.Thread(target=cache.run, name='snapshot-projection', daemon=True)
        worker.start()
        try:
            yield
        finally:
            cache.stop()
            worker.join(timeout=15)

    app = FastAPI(title='openRuyi Lookout', version='0.1.0', docs_url=None, redoc_url=None,
                  lifespan=lifespan,
                  description='Read-only collected facts. succeeded is not a release or revision verification claim.')
    app.state.projection = cache

    def data():
        try:
            return cache.read()
        except ValueError:
            raise HTTPException(503, 'No prepared snapshot yet') from None
    @app.get('/healthz')
    def health():
        return {'status': 'ok'}
    @app.get('/readyz')
    def ready():
        snap, index, collection = data()
        return {'status': 'degraded' if collection['errors'] else 'ready', 'packages': len(index.rows),
                'generation': snap['generation'], 'last_attempt': snap['last_attempt'],
                'projection_notice': collection.get('projection_notice')}
    def find_package(name, index):
        found = index.by_name.get(name)
        if found is None:
            raise HTTPException(404, 'Package not found')
        return found
    def select_monitored(filters, *, document=False, search='name'):
        snap, index, collection = data()
        catalog = [module.describe() for module in monitor_views.registry(snap)]
        if filters.monitor and filters.monitor not in {m['id'] for m in catalog}:
            raise HTTPException(422, 'Unknown monitor')
        monitor = filters.monitor
        section = filters.section
        focused = next((m for m in catalog if m['id'] == monitor), None)
        if document and focused and not presentation_navigation.presenter(focused).has_results:
            section = 'coverage'
        try:
            result = index.select(query=filters.q, monitor=monitor, filters=filters.filters,
                page=filters.page, per_page=filters.per_page,
                search=search,
                findings_only=bool(focused and focused['kind'] in ('evidence', 'requires', 'commits') and section == 'results'))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if document:
            result['monitor_coverage'] = index.monitor_coverage()
            result['query'] = filters.model_copy(update={'section': section, 'page': result['page']}).model_dump(exclude_defaults=True)
            result['query']['filters'] = filters.filters.model_dump()
        return {**result,
                'section': section,
                'monitors': catalog, 'targets': snap.get('targets', []), 'collection': collection,
                'presentation': snap.get('presentation', {})}

    @app.get('/api/v2/packages', response_model=MonitoredList, openapi_extra=query_schema(PackageQuery))
    def monitor_packages(filters: Annotated[PackageQuery, Depends(package_parameters)]):
        if filters.detail == 'full' and filters.per_page > FULL_PAGE_LIMIT:
            raise HTTPException(422, f'Full observations require per_page <= {FULL_PAGE_LIMIT}')
        result = select_monitored(filters, search=filters.search)
        included = selected_monitors(filters.include, result['monitors'])
        return {**result, 'items': [package_response(row, included,
            full=filters.detail == 'full', focus=filters.monitor) for row in result['items']]}

    @app.get('/api/ui/packages', response_model=ListingDocument, openapi_extra=query_schema(ListingQuery))
    def listing_document(filters: Annotated[ListingQuery, Depends(listing_parameters)]):
        result = select_monitored(filters, document=True)
        return presentation_pages.listing(result, result['query'])

    @app.get('/api/v2/packages:batchGet', response_model=PackageBatch)
    def batch_packages(filters: Annotated[BatchQuery, Query()]):
        if len(set(filters.names)) != len(filters.names):
            raise HTTPException(422, 'Batch package names must be unique')
        snap, index, collection = data()
        included = selected_monitors(filters.include, [module.describe() for module in monitor_views.registry(snap)])
        rows = [find_package(name, index) for name in filters.names]
        return {'items': [package_response(row, included, full=True) for row in rows], 'collection': collection}

    @app.get('/api/v2/packages/{name}', response_model=MonitoredDetail)
    def monitor_package(name: str, filters: Annotated[MonitorSelection, Query()]):
        snap, index, _ = data()
        included = selected_monitors(filters.include, [module.describe() for module in monitor_views.registry(snap)])
        package = package_response(find_package(name, index), included, full=True)
        # Keep validated submodels; only the HTTP boundary serializes them.
        return MonitoredDetail(**dict(package), presentation=snap.get('presentation', {}))

    @app.get('/api/ui/packages/{name}', response_model=DetailDocument)
    def package_document(name: str):
        snap, index, collection = data()
        document = presentation_pages.detail(find_package(name, index), collection)
        for kind in ('pr', 'issue'):
            history = activity.page(snap, name, kind)
            if history['total']:
                document.sections.insert(len(document.sections) - 1, activity_presentation.section(name, kind, history))
        document.data_status = presentation_pages.data_status(collection, index.monitor_coverage(),
            [module.describe() for module in monitor_views.registry(snap)])
        return document

    def activity_history(name, kind, cursor, per_page):
        snap, index, _ = data()
        find_package(name, index)
        try:
            return activity.page(snap, name, kind, cursor, per_page)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get('/api/v2/packages/{name}/activity', response_model=ActivityPage)
    def package_activity(name: str, kind: Literal['pr', 'issue'], cursor: str | None = None,
                         per_page: Annotated[int, Query(ge=1, le=50)] = 20):
        return activity_history(name, kind, cursor, per_page)

    @app.get('/api/ui/packages/{name}/activity', response_model=Section)
    def activity_document(name: str, kind: Literal['pr', 'issue'], cursor: str | None = None):
        return activity_presentation.section(name, kind, activity_history(name, kind, cursor, 20))

    @app.get('/api/v2/packages/{name}/commits', response_model=CommitPage)
    def package_commits(name: str, cursor: str | None = None,
                        per_page: Annotated[int, Query(ge=1, le=50)] = 20,
                        status: Literal['original_reachable', 'strict_patch_evidence', 'backport_needs_review',
                                        'no_evidence_in_range', 'unknown'] | None = None,
                        rule: Annotated[str | None, Query(min_length=1, max_length=64)] = None):
        snap, index, _ = data()
        find_package(name, index)
        try:
            return commits.page(snap, name, cursor=cursor, per_page=per_page, status=status, rule=rule)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get('/api/ui/packages/{name}/commits', response_model=Section)
    def commits_document(name: str, cursor: str | None = None,
                         status: Literal['original_reachable', 'strict_patch_evidence', 'backport_needs_review',
                                         'no_evidence_in_range', 'unknown'] | None = None,
                         rule: Annotated[str | None, Query(min_length=1, max_length=64)] = None):
        from tracker.presentation.commits import history_section
        snap, index, _ = data()
        find_package(name, index)
        try:
            history = commits.page(snap, name, cursor=cursor, status=status, rule=rule)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return history_section(name, history, status=status, rule=rule)

    @app.get('/api/ui/theme', response_model=DocumentTheme)
    def document_theme():
        snap, _, _ = data()
        return presentation_pages.theme(snap.get('presentation', {}))

    @app.get('/api/v2/tracks/{track_id}')
    def track(track_id: str):
        snap, _, _ = data()
        if track_id not in snap['tracks']:
            raise HTTPException(404, 'Track not found')
        fact = snap['tracks'][track_id]
        return dict(id=track_id, **fact, stale=state.stale(fact, datetime.now(UTC), snap.get('stale_after_seconds', 86400)))
    @app.get('/api/v2/targets', response_model=list[Target])
    def targets():
        snap, _, _ = data()
        return snap.get('targets', [])
    @app.get('/api/v2/status')
    def status():
        try:
            return cache.read_status()
        except ValueError:
            raise HTTPException(503, 'No prepared snapshot yet') from None
    @app.get('/api/v2/export', response_model=MonitorExport)
    def export(response: Response):
        snap, index, collection = data()
        response.headers['Content-Disposition'] = 'attachment; filename="openruyi-packages.json"'
        return {'schema': 2, 'collection': collection, 'targets': snap.get('targets', []), 'packages': index.rows}
    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return app

app = create_app()
