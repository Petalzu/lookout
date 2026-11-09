import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def fixed_git_clock(monkeypatch):
    for name in ('GIT_AUTHOR_DATE', 'GIT_COMMITTER_DATE'):
        monkeypatch.setenv(name, '2026-11-06T10:00:00+00:00')


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location('commit_case_checker', ROOT / 'scripts/check-commit-cases.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cases(checker):
    data = json.loads((ROOT / 'backend/tests/fixtures/commits-real-cases.json').read_text())
    return {**data, 'cases': checker.load_cases(data)}
