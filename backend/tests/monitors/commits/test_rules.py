import inspect
import os
import signal
import sys
import time
import unicodedata
from unittest.mock import Mock

import pytest

from tracker.monitors.commits import rules as matcher
from tracker.monitors.commits.model import Rule
from tracker.monitors.commits.rules import RuleError, match_rules


def test_text_and_path_intersection_with_rule_union():
    rules = [Rule(id='both', keywords=['riscv'], paths=['target/riscv/**']),
             Rule(id='text', keywords=['RISC-V'])]
    assert not match_rules({'subject': 'riscv', 'paths': ['docs/x']}, rules)
    assert {m['rule'] for m in match_rules({'subject': 'RISCV', 'paths': ['target/riscv/x']}, rules)} == {'both'}
    assert match_rules({'body': 'RISC-V fix', 'paths': ['docs/x']}, rules)[0]['field'] == 'body'


def test_exclusion_and_case_sensitive_paths():
    rules = [Rule(id='path', paths=['target/riscv/**']), Rule(id='exclude', keywords=['docs'], exclude=True)]
    assert not match_rules({'subject': 'docs', 'paths': ['target/riscv/a']}, rules)
    assert not match_rules({'paths': ['Target/riscv/a']}, rules)
    assert match_rules({'paths': ['old', 'target/riscv/new']}, rules)
    assert not match_rules({'subject': 'RISCV'}, [Rule(id='case', keywords=['riscv'], case_sensitive=True)])


def test_regex_isolated_and_budget_is_not_empty_success():
    assert match_rules({'subject': 'RISC-V'}, [Rule(id='r', patterns=['risc.?v'])])
    with pytest.raises(RuleError, match='text_limit'):
        match_rules({'body': 'a' * 1025}, [Rule(id='r', keywords=['a'])], text_limit=1024)
    with pytest.raises(RuleError, match='regex_timeout'):
        match_rules({'body': 'a' * 20000 + '!'}, [Rule(id='r', patterns=['(a+)+$'])], timeout_seconds=0.1)



def _commit(subject="", body="", paths=None):
    return {"subject": subject, "body": body, "paths": [] if paths is None else paths}


def _reason(rule, field, fragment, path=None):
    return {"rule": rule, "field": field, "fragment": fragment, "path": path}


def _error(code, data, rules, **kwargs):
    with pytest.raises(RuleError) as caught:
        match_rules(data, rules, **kwargs)
    assert caught.value.code == code
    assert str(caught.value) == code


@pytest.fixture
def workers(monkeypatch):
    original = matcher.subprocess.Popen
    started = []

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        process.kill = Mock(wraps=process.kill)
        started.append(process)
        return process

    monkeypatch.setattr(matcher.subprocess, "Popen", record)
    yield started
    leaked = []
    for process in started:
        if process.returncode is None:
            leaked.append(process.pid)
            process.kill()
            process.wait()
    assert not leaked, f"unreaped matcher workers: {leaked}"


def _reaped(process):
    assert process.returncode is not None
    assert process.stdin.closed
    assert process.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


def test_public_budget_defaults_are_keyword_only():
    parameters = inspect.signature(match_rules).parameters
    assert parameters["timeout_seconds"].default == 1
    assert parameters["text_limit"].default == 524288
    assert parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["text_limit"].kind is inspect.Parameter.KEYWORD_ONLY


def test_union_preserves_subject_body_and_literal_keyword_fragments():
    rules = [Rule(id="Subject", keywords=["fix"]), Rule(id="Body", patterns=["repair"])]
    assert match_rules(_commit("FIX", "repair it"), rules) == [
        _reason("Subject", "subject", "FIX"), _reason("Body", "body", "repair"),
    ]
    literal = Rule(id="Literal", keywords=["a.b"], patterns=[r"repair\d+"])
    assert match_rules(_commit("axb", "repair42"), [literal]) == [_reason("Literal", "body", "repair42")]
    assert match_rules(_commit("prea.bpost"), [literal]) == [_reason("Literal", "subject", "a.b")]
    assert match_rules(_commit("axb"), [literal]) == []
    assert match_rules(_commit("FIX", "fix"), [rules[0]]) == [
        _reason("Subject", "subject", "FIX"), _reason("Subject", "body", "fix"),
    ]


def test_rule_intersection_and_path_alternatives():
    rule = Rule(id="Scoped", keywords=["fix"], paths=["docs/**", "src/**"])
    assert match_rules(_commit("fix", paths=["other/a.py"]), [rule]) == []
    assert match_rules(_commit("none", paths=["src/a.py"]), [rule]) == []
    assert match_rules(_commit("fix", paths=["src/nested/a.py"]), [rule]) == [
        _reason("Scoped", "subject", "fix"),
        _reason("Scoped", "path", "src/nested/a.py", "src/nested/a.py"),
    ]


def test_rename_input_preserves_old_and_new_path_evidence():
    rules = [Rule(id="Old", paths=["legacy/**"]), Rule(id="New", paths=["src/**"])]
    assert match_rules(_commit(paths=["legacy/old.py", "src/new.py"]), rules) == [
        _reason("Old", "path", "legacy/old.py", "legacy/old.py"),
        _reason("New", "path", "src/new.py", "src/new.py"),
    ]


@pytest.mark.parametrize(
    ("pattern", "path", "matched"),
    [
        ("foo/*", "foo/a", True), ("foo/*", "foo/a/b", False),
        ("foo/**", "foo/a", True), ("foo/**", "foo/a/b", True),
        ("foo/**", "foobar/a", False), ("foo/**/a.py", "foo/a.py", True),
        ("foo/**/a.py", "foo/deep/a.py", True), ("**/*.py", "a.py", True),
        ("*.py", "src/a.py", False), ("foo/?.py", "foo/a.py", True),
        ("foo/?.py", "foo/ab.py", False), ("foo/[ab].py", "foo/b.py", True),
        ("foo/**", "foo/.hidden/a", True),
    ],
)
def test_globs_respect_repository_path_segments(pattern, path, matched):
    assert bool(match_rules(_commit(paths=[path]), [Rule(id="Glob", paths=[pattern])])) is matched


@pytest.mark.parametrize("kind", ["keyword", "regex"])
@pytest.mark.parametrize("sensitive", [False, True])
def test_text_case_flag_and_case_sensitive_repository_paths(kind, sensitive):
    terms = {"keyword": {"keywords": ["fix"]}, "regex": {"patterns": ["fix"]}}
    rule = Rule(id="Case", case_sensitive=sensitive, **terms[kind])
    assert bool(match_rules(_commit("FIX"), [rule])) is (not sensitive)
    path_rule = Rule(id="Path", case_sensitive=sensitive, paths=["src/*"])
    assert match_rules(_commit(paths=["SRC/A"]), [path_rule]) == []


def test_unicode_casefold_preserves_original_matching_slice():
    assert match_rules(_commit("ß prefix STRAẞE suffix"), [Rule(id="Unicode", keywords=["strasse"])]) == [
        _reason("Unicode", "subject", "STRAẞE"),
    ]


def test_exclusion_requires_both_conditions_and_overrides_in_any_order():
    positive = Rule(id="Keep", keywords=["fix"])
    exclude = Rule(id="Drop", keywords=["skip"], paths=["src/**"], exclude=True)
    data = _commit("fix", "skip", ["src/a.py"])
    assert match_rules(data, [positive, exclude]) == []
    assert match_rules(data, [exclude, positive]) == []
    assert match_rules(_commit("fix", "skip", ["docs/a"]), [positive, exclude]) == [_reason("Keep", "subject", "fix")]
    assert match_rules(_commit("fix"), [exclude]) == []


def test_no_matches_empty_rules_and_no_cross_field_matching():
    assert match_rules(_commit("fix"), []) == []
    assert match_rules(_commit("unrelated"), [Rule(id="No", keywords=["fix"])]) == []
    assert match_rules(_commit("fi", "x"), [Rule(id="Split", keywords=["fix"], patterns=["fix"])]) == []
    assert match_rules(_commit("fix"), [Rule(id="Path", paths=["src/**"])]) == []


@pytest.mark.parametrize("data", [_commit("none"), _commit("fix", paths=["unrelated/x"])])
@pytest.mark.parametrize("exclude", [False, True])
def test_invalid_regex_is_not_hidden_by_path_text_or_exclusion(data, exclude):
    bad = Rule.model_construct(id="Bad", patterns=["("], paths=["src/**"], exclude=exclude)
    _error("invalid_regex", data, [Rule(id="Good", keywords=["fix"]), bad])
    _error("invalid_regex", _commit("fix"), [Rule(id="Drop", keywords=["fix"], exclude=True), bad])


@pytest.mark.parametrize("value", [0, -1, True, False, "1", None, float("nan"), float("inf"), 11])
def test_invalid_timeout_values_are_errors(value):
    _error("invalid_budget", _commit("fix"), [Rule(id="One", keywords=["fix"])], timeout_seconds=value)


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1", None, 1048577])
def test_invalid_text_limit_values_are_errors(value):
    _error("invalid_budget", _commit("fix"), [Rule(id="One", keywords=["fix"])], text_limit=value)


def test_total_text_limit_is_inclusive_utf8_bytes():
    rule = Rule(id="Text", keywords=["é"])
    data = _commit("é", "中")
    assert match_rules(data, [rule], text_limit=5) == [_reason("Text", "subject", "é")]
    _error("text_limit", data, [rule], text_limit=4)
    assert match_rules(_commit("a", "b"), [Rule(id="A", keywords=["a"])], text_limit=2)
    _error("text_limit", _commit("a", "b"), [], text_limit=1)


def test_default_total_text_boundary():
    rule = Rule(id="Text", keywords=["x"])
    assert match_rules(_commit("x", "a" * 524287), [rule]) == [_reason("Text", "subject", "x")]
    _error("text_limit", _commit("x", "a" * 524288), [rule])


@pytest.mark.parametrize("data", [None, _commit(123), _commit(body=None), _commit(paths="src/a"), _commit(paths=[1]),
                                       _commit("\ud800")])
def test_invalid_commit_values_are_not_coerced(data):
    _error("invalid_commit", data, [Rule(id="One", keywords=["fix"])])


@pytest.mark.parametrize("path", ["/etc/x", "../x", "src/../x", "src/./x", "a//b", "a\\b", "x\ny", "x\x00y", ""])
def test_unsafe_paths_are_errors_not_silently_dropped(path):
    _error("invalid_path", _commit("fix", paths=[path]), [Rule(id="One", keywords=["fix"])])


@pytest.mark.parametrize("rule", [Rule.model_construct(id="Empty"), Rule.model_construct(id="Unsafe\n", keywords=["x"]),
                                  Rule.model_construct(id="BadPath", paths=["../*"]),
                                  Rule.model_construct(id="BadTerm", keywords=[""]),
                                  Rule.model_construct(id="BadFlag", keywords=["x"], exclude=1),
                                  {"id": "NotRule", "keywords": ["x"]}])
def test_invalid_rules_raise_safe_codes(rule):
    _error("invalid_rule", _commit("x"), [rule])


def test_fragment_bound_and_unicode_control_removal():
    text = "BEGIN\n\t\x1b\x00\u202e" + "a" * 400 + "END"
    fragment = match_rules(_commit(text), [Rule(id="Safe", patterns=["(?s)BEGIN.*END"])])[0]["fragment"]
    assert fragment.startswith("BEGIN")
    assert len(fragment) == 256
    assert all(not unicodedata.category(char).startswith("C") for char in fragment)


def test_quotes_in_paths_remain_structured_unmodified_values():
    path = 'src/a"}evil.py'
    assert match_rules(_commit(paths=[path]), [Rule(id="Quoted", paths=["src/*"])]) == [
        _reason("Quoted", "path", path, path),
    ]


def test_zero_width_regex_matches():
    assert match_rules(_commit("fix"), [Rule(id="Zero", patterns=["^x*"])])[0] == _reason("Zero", "subject", "")


def test_worker_isolated_and_reaped_on_success_and_invalid_regex(workers):
    assert match_rules(_commit("FIX"), [Rule(id="One", patterns=["fix"])])
    _error("invalid_regex", _commit(), [Rule.model_construct(id="Bad", patterns=["("])])
    assert len(workers) == 2
    for process in workers:
        assert process.pid != os.getpid()
        assert "-I" in process.args
        assert "-B" in process.args
        _reaped(process)


def test_catastrophic_regex_is_killed_reaped_and_subsequent_matching_works(workers):
    started = time.monotonic()
    _error("regex_timeout", _commit("a" * 10000 + "!"), [Rule(id="Slow", patterns=["(a+)+$"])], timeout_seconds=0.2)
    assert time.monotonic() - started < 3
    assert len(workers) == 1
    assert workers[0].kill.called
    assert workers[0].returncode == -signal.SIGKILL
    _reaped(workers[0])
    assert match_rules(_commit("fix"), [Rule(id="After", patterns=["fix"])]) == [_reason("After", "subject", "fix")]
    _reaped(workers[1])


def test_ipc_input_limit_is_checked_before_starting_worker(workers):
    paths = [f"{index:04}/" + "p" * 1020 for index in range(4096)]
    _error("input_limit", _commit(paths=paths), [Rule(id="Paths", paths=["**"])])
    assert workers == []


def test_worker_reason_output_limit_is_an_error_not_an_empty_result(workers):
    paths = [f"src/{index:04}-" + "p" * 600 for index in range(1500)]
    _error("output_limit", _commit(paths=paths), [Rule(id="Paths", paths=["src/**"])])
    _reaped(workers[0])


def test_parent_caps_stdout_and_kills_noisy_worker(monkeypatch, workers):
    recorded = matcher.subprocess.Popen

    def noisy_worker(args, **kwargs):
        script = f"import os, time; os.write(1, b'x' * {matcher._OUTPUT_LIMIT + 1}); time.sleep(5)"
        return recorded([sys.executable, "-I", "-B", "-c", script], **kwargs)

    monkeypatch.setattr(matcher.subprocess, "Popen", noisy_worker)
    _error("output_limit", _commit("fix"), [Rule(id="One", patterns=["fix"])])
    assert workers[0].kill.called
    _reaped(workers[0])


def test_worker_failure_is_safe_and_reaped(monkeypatch, workers):
    recorded = matcher.subprocess.Popen

    def broken_worker(args, **kwargs):
        return recorded([sys.executable, "-I", "-B", "-c", "raise SystemExit(2)"], **kwargs)

    monkeypatch.setattr(matcher.subprocess, "Popen", broken_worker)
    _error("worker_error", _commit("fix"), [Rule(id="One", keywords=["fix"])])
    _reaped(workers[0])



def test_legal_budget_upper_boundaries_and_missing_optional_commit_fields():
    rule = Rule(id="One", keywords=["fix"])
    assert match_rules({"subject": "fix"}, [rule], timeout_seconds=10, text_limit=1048576) == [
        _reason("One", "subject", "fix"),
    ]
    assert match_rules({}, [rule], text_limit=1) == []


def test_escaped_json_ipc_size_is_bounded_before_worker_start(workers):
    _error("input_limit", _commit('"' * 1048576), [Rule(id="One", keywords=["fix"])], text_limit=1048576)
    assert workers == []


def test_duplicate_ids_and_container_type_are_invalid():
    rule = Rule(id="Same", keywords=["fix"])
    _error("invalid_rule", _commit("fix"), [rule, rule])
    _error("invalid_rule", _commit("fix"), (rule,))


@pytest.mark.parametrize("stdout", ["not json", '{"matches":[{"rule":"injected"}]}', '{"error":"private details"}'])
def test_malformed_worker_protocol_never_returns_untrusted_reasons(monkeypatch, workers, stdout):
    recorded = matcher.subprocess.Popen

    def malformed_worker(args, **kwargs):
        return recorded([sys.executable, "-I", "-B", "-c", f"print({stdout!r})"], **kwargs)

    monkeypatch.setattr(matcher.subprocess, "Popen", malformed_worker)
    _error("worker_error", _commit("fix"), [Rule(id="One", keywords=["fix"])])
    _reaped(workers[0])


def test_worker_start_failure_uses_safe_code(monkeypatch):
    def cannot_start(*args, **kwargs):
        raise OSError("private interpreter location")

    monkeypatch.setattr(matcher.subprocess, "Popen", cannot_start)
    _error("worker_error", _commit("fix"), [Rule(id="One", keywords=["fix"])])



def test_matching_exclusion_precedes_positive_reason_output_budget():
    paths = [f"src/{index:04}-" + "p" * 600 for index in range(1500)]
    rules = [Rule(id="Many", paths=["src/**"]), Rule(id="Drop", keywords=["skip"], exclude=True)]
    assert match_rules(_commit("skip", paths=paths), rules) == []
