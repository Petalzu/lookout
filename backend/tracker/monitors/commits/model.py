"""Validated policy and immutable identities for commit collection."""
import base64
import binascii
import hashlib
import json
import re
import unicodedata
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from tracker.monitors.schedule import Schedule

STATUSES = ('original_reachable', 'strict_patch_evidence', 'backport_needs_review',
            'no_evidence_in_range', 'unknown')
METHOD_VERSION = 2
STRICT_MANIFEST_VERSION = 1
STRICT_FILE_LIMIT = 256
STRICT_PATH_BYTES = 4096
STRICT_MANIFEST_BYTES = 65536


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def complete_oid(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', value):
        raise ValueError('a complete lowercase Git object ID is required')
    return value


oid = complete_oid


def https_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        raise ValueError('URL must not contain whitespace or control characters')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or '%' in parsed.path or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise ValueError('a canonical public HTTPS URL is required')
    return value.rstrip('/')


def safe_path(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096 and "\\" not in value
            and not any(part in ('', '.', '..') for part in value.split('/'))
            and not any(unicodedata.category(char).startswith('C') for char in value))


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class StrictFileState(Policy):
    mode: Literal['000000', '100644', '100755']
    blob_oid: str | None
    sha256: str | None = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('blob_oid')
    @classmethod
    def valid_blob_oid(_cls, value):
        return complete_oid(value) if value is not None else None

    @model_validator(mode='after')
    def consistent_presence(self):
        missing = self.mode == '000000'
        if (self.blob_oid is None) != missing or (self.sha256 is None) != missing:
            raise ValueError('absent files require null object and content digest')
        return self


class StrictFile(Policy):
    path_b64: str = Field(min_length=4, max_length=4 * ((STRICT_PATH_BYTES + 2) // 3))
    before: StrictFileState
    after: StrictFileState

    @field_validator('path_b64')
    @classmethod
    def raw_path(_cls, value):
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError('path must be canonical base64') from error
        if (not raw or len(raw) > STRICT_PATH_BYTES or b'\x00' in raw
                or base64.b64encode(raw).decode('ascii') != value):
            raise ValueError('path must losslessly encode a bounded raw Git path')
        return value


class StrictChange(Policy):
    oid: str
    parent_oid: str
    files: list[StrictFile] = Field(min_length=1, max_length=STRICT_FILE_LIMIT)

    _oids = field_validator('oid', 'parent_oid')(complete_oid)


class StrictManifest(Policy):
    version: Literal[1] = STRICT_MANIFEST_VERSION
    candidate: StrictChange
    matches: list[StrictChange] = Field(min_length=1, max_length=STRICT_FILE_LIMIT)

    @model_validator(mode='after')
    def bounded_manifest(self):
        if (sum(len(change.files) for change in [self.candidate, *self.matches]) > STRICT_FILE_LIMIT
                or len(self.model_dump_json().encode()) > STRICT_MANIFEST_BYTES):
            raise ValueError('strict verification manifest budget exceeded')
        return self


class Rule(Policy):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    keywords: list[str] = Field(default_factory=list, max_length=64)
    patterns: list[str] = Field(default_factory=list, max_length=32)
    paths: list[str] = Field(default_factory=list, max_length=64)
    case_sensitive: bool = False
    exclude: bool = False

    @model_validator(mode='after')
    def validate_rule(self, info: ValidationInfo):
        if not (self.keywords or self.patterns or self.paths):
            raise ValueError('an empty rule matches nothing and must not be configured')
        for term in [*self.keywords, *self.patterns, *self.paths]:
            if not term or len(term) > 1024:
                raise ValueError('rule terms must contain 1..1024 characters')
            term.encode('utf-8')
        for pattern in self.patterns if not (info.context or {}).get('worker_compiles_patterns') else []:
            try:
                re.compile(pattern)
            except re.PatternError as error:
                raise ValueError('invalid rule regular expression') from error
        for path in self.paths:
            if not safe_path(path):
                raise ValueError('rule paths must be repository-relative POSIX patterns')
        return self


class RepositoryPolicy(Policy):
    url: str
    web_url: str
    ref: str = 'refs/heads/main'
    object_format: str = Field('sha1', pattern=r'^(sha1|sha256)$')
    window_start: str
    analysis_start: str | None = None
    fetch_depth: int = Field(2048, ge=1, le=100000)
    scan_limit: int = Field(2000, ge=1, le=100000)
    history_limit: int = Field(2000, ge=1, le=100000)
    timeout_seconds: int = Field(60, ge=1, le=600)
    output_limit: int = Field(8388608, ge=4096, le=67108864)
    disk_limit: int = Field(1073741824, ge=1048576, le=17179869184)
    record_limit_bytes: int = Field(16777216, ge=16384, le=268435456)

    _url = field_validator('url', 'web_url')(https_url)
    _start = field_validator('window_start')(oid)

    @model_validator(mode='after')
    def object_identity(self):
        expected = 64 if self.object_format == 'sha256' else 40
        if len(self.window_start) != expected or self.analysis_start and len(self.analysis_start) != expected:
            raise ValueError('range object IDs must match the configured repository object format')
        return self

    @field_validator('analysis_start')
    @classmethod
    def valid_analysis_start(_cls, value):
        return complete_oid(value) if value is not None else None

    @field_validator('ref')
    @classmethod
    def valid_ref(_cls, value):
        if (not re.fullmatch(r'refs/heads/[A-Za-z0-9_./-]+', value)
                or '..' in value or '@{' in value or value.endswith(('/', '.', '.lock'))):
            raise ValueError('monitor ref must be an explicit branch ref')
        return value


class Mapping(Policy):
    oid: str
    ref: str | None = None
    source_revision: str = Field(min_length=1, max_length=256)
    source_url: str
    review_url: str
    version: int = Field(1, ge=1)

    _oid = field_validator('oid')(complete_oid)
    _urls = field_validator('source_url', 'review_url')(https_url)

    @field_validator('ref')
    @classmethod
    def fixed_tag(_cls, value):
        if value is not None and (not re.fullmatch(r'refs/tags/[A-Za-z0-9_./-]+', value)
                                  or '..' in value or value.endswith(('/', '.', '.lock'))):
            raise ValueError('mapping ref must be an explicit release tag')
        return value


class PackagePolicy(Policy):
    repository: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    rules: list[Rule] = Field(min_length=1, max_length=128)
    mappings: dict[str, Mapping] = Field(default_factory=dict)

    @model_validator(mode='after')
    def unique_rules(self):
        if len({r.id for r in self.rules}) != len(self.rules) or not any(not r.exclude for r in self.rules):
            raise ValueError('rule IDs must be unique and a positive rule is required')
        return self


class Settings(Policy):
    repositories: dict[str, RepositoryPolicy] = Field(default_factory=dict)
    interval_seconds: int = Field(3600, ge=60, le=86400)
    stale_after_seconds: int = Field(86400, ge=120, le=604800)
    batch_size: int = Field(32, ge=1, le=1000)
    max_findings: int = Field(1000, ge=1, le=1000)
    regex_timeout_seconds: int = Field(1, ge=1, le=10)
    text_limit: int = Field(524288, ge=1024, le=1048576)
    max_batch_bytes: int = Field(4194304, ge=16384, le=16777216)

    @model_validator(mode='after')
    def valid_settings(self):
        if self.stale_after_seconds <= self.interval_seconds:
            raise ValueError('commits stale threshold must exceed polling interval')
        for key in self.repositories:
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key):
                raise ValueError('invalid repository identity')
        return self


def settings(config):
    return Settings.model_validate(config.get('commits', config.get('openruyi', {}).get('commits', {})))


def package_policies(config):
    result = {}
    for name, package in config.get('packages', {}).items():
        raw = package.get('monitors', {}).get('commits')
        if raw is not None and raw is not False:
            result[name] = PackagePolicy.model_validate(raw)
    configured = settings(config)
    if any(policy.repository not in configured.repositories for policy in result.values()):
        raise ValueError('commits package refers to an unconfigured repository')
    return result


def polling(config):
    return Schedule(settings(config).interval_seconds, 300, 3600)


def evidence_key(repository, candidate, target, rule_digest, analysis_range):
    return digest(dict(repository=repository, candidate=oid(candidate), target=target,
                       rules=rule_digest, method=METHOD_VERSION, analysis_range=analysis_range))


def source_binding(source):
    """Evidence dependencies, excluding polling and diagnostic timestamps."""
    return digest({key: source.get(key) for key in
                   ('origin', 'revision', 'version', 'metadata', 'native_query', 'error')})
