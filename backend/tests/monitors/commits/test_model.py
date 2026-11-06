import pytest
from pydantic import ValidationError

from tracker.monitors.commits.model import (
    Mapping,
    PackagePolicy,
    RepositoryPolicy,
    Rule,
    Settings,
    digest,
    evidence_key,
    package_policies,
)

OID = 'a' * 40


def test_repository_requires_fixed_window_and_https():
    with pytest.raises(ValidationError):
        RepositoryPolicy(url='https://example.org/a.git', web_url='https://example.org/a')
    for url in ('file:///etc', 'ssh://example.org/a', 'https://u:p@example.org/a', 'https://example.org/a?x=1', 'https://example.org/\npath'):
        with pytest.raises(ValidationError):
            RepositoryPolicy(url=url, web_url='https://example.org/a', window_start=OID)


def test_rule_is_explicit_and_bounded():
    for raw in ({'id': 'empty'}, {'id': 'x', 'paths': ['../etc']}, {'id': 'x', 'patterns': ['[']}):
        with pytest.raises((ValidationError, ValueError)):
            Rule(**raw)
    assert Rule(id='riscv', paths=['target/riscv/**']).paths == ['target/riscv/**']
    with pytest.raises(ValidationError):
        PackagePolicy(repository='qemu', rules=[Rule(id='a', keywords=['a']), Rule(id='a', keywords=['b'])])


def test_settings_and_mapping_fail_closed():
    with pytest.raises(ValidationError):
        Settings(interval_seconds=600, stale_after_seconds=600)
    with pytest.raises(ValueError):
        package_policies({'packages': {'p': {'monitors': {'commits': {
            'repository': 'missing', 'rules': [{'id': 'r', 'keywords': ['riscv']}]
        }}}}})
    with pytest.raises(ValidationError):
        Mapping(oid='abc', source_revision='r', source_url='https://example.org/src', review_url='https://example.org/review')


def test_evidence_identity_excludes_collection_membership():
    target = {'side': 'current', 'oid': 'b' * 40, 'revision': 'r1'}
    original = evidence_key('qemu', OID, target, 'rules', {'start': 'c' * 40})
    assert original == evidence_key('qemu', OID, dict(target), 'rules', {'start': 'c' * 40})
    assert original != evidence_key('qemu', OID, {**target, 'revision': 'r2'}, 'rules', {'start': 'c' * 40})
    assert digest({'a': 1, 'b': 2}) == digest({'b': 2, 'a': 1})


def test_real_opt_in_examples_validate_without_inventing_source_mappings():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    distribution = tomllib.loads((root / 'config/commits-distribution.example.toml').read_text())
    packages = tomllib.loads((root / 'config/commits-packages.example.toml').read_text())
    result = package_policies({**distribution, 'packages': packages})
    assert set(result) == {'qemu', 'llvm22'}
    assert all(not policy.mappings for policy in result.values())
