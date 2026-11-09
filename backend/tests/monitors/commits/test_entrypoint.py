import importlib.util
import threading
from contextlib import nullcontext
from pathlib import Path

from tracker.monitors.commits.model import polling


def test_opt_in_commits_has_its_own_scheduled_phase(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location('commits_entrypoint', root / 'deploy/container-entrypoint.py')
    assert spec is not None and spec.loader is not None
    entrypoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entrypoint)
    jobs = []

    class Thread:
        def __init__(self, *, target, args, daemon, name):
            jobs.append((target, args, name))

        def start(self):
            pass

        def join(self, timeout):
            pass

    config = {'collector': {'obs_interval_seconds': 60, 'build_interval_seconds': 15,
                            'nvchecker_interval_seconds': 3600}, 'spec': {'repo': None},
              'commits': {'repositories': {'example': {'url': 'https://example.org/repo.git',
                 'web_url': 'https://example.org/repo', 'window_start': 'a' * 40}}},
              'packages': {'package': {'monitors': {'commits': {'repository': 'example',
                          'rules': [{'id': 'r', 'keywords': ['riscv']}]}}}}}
    stopped = threading.Event()
    stopped.set()
    monkeypatch.setattr(entrypoint, '_stop', stopped)
    monkeypatch.setattr(entrypoint, 'runtime_session', lambda *args: nullcontext(config))
    monkeypatch.setattr(entrypoint.threading, 'Thread', Thread)
    monkeypatch.setattr(entrypoint.os, 'makedirs', lambda *args, **kwargs: None)
    monkeypatch.setattr(entrypoint.signal, 'signal', lambda *args: None)
    entrypoint.main()
    assert (entrypoint.periodic, ('commits', polling(config)), 'commits') in jobs
    assert not any(name == 'monitors' for _, _, name in jobs)
