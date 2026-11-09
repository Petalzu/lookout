"""Bounded, explainable commit rules; all pattern execution runs in one owned worker."""

from __future__ import annotations

import json
import re
import sys
import unicodedata

if __name__ != "__main__":
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from tracker.monitors.commits.model import Rule

_INPUT_LIMIT = 2 * 1024 * 1024
_OUTPUT_LIMIT = 1024 * 1024
_ERROR_CODES = frozenset({
    "invalid_budget", "invalid_commit", "invalid_rule", "invalid_regex", "invalid_path",
    "text_limit", "input_limit", "output_limit", "regex_timeout", "worker_error",
})


class RuleError(ValueError):
    """Only a stable, non-sensitive code is exposed in the exception message."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "worker_error"
        super().__init__(self.code)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_path(value):
    return (
        isinstance(value, str) and 0 < len(value) <= 4096
        and "\\" not in value
        and not any(part in ("", ".", "..") for part in value.split("/"))
        and not any(unicodedata.category(char).startswith("C") for char in value)
    )


def _commit_data(commit, text_limit):
    if not isinstance(commit, dict):
        raise RuleError("invalid_commit")
    text = {field: commit.get(field, "") for field in ("subject", "body")}
    paths = commit.get("paths", [])
    if any(not isinstance(value, str) for value in text.values()) or not isinstance(paths, list):
        raise RuleError("invalid_commit")
    if sum(len(value) for value in text.values()) > text_limit:
        raise RuleError("text_limit")
    try:
        size = sum(len(value.encode("utf-8")) for value in text.values())
    except UnicodeError:
        raise RuleError("invalid_commit") from None
    if size > text_limit:
        raise RuleError("text_limit")
    if len(paths) > 4096:
        raise RuleError("input_limit")
    for path in paths:
        if not isinstance(path, str):
            raise RuleError("invalid_commit")
        if not _safe_path(path):
            raise RuleError("invalid_path")
        size += len(path.encode("utf-8"))
        if size > _INPUT_LIMIT:
            raise RuleError("input_limit")
    return {**text, "paths": sorted(set(paths))}


def _rule_data(rules):
    from tracker.monitors.commits.model import Rule

    if not isinstance(rules, list):
        raise RuleError("invalid_rule")
    if len(rules) > 128:
        raise RuleError("input_limit")
    selected = []
    identities = set()
    for rule in rules:
        if not isinstance(rule, Rule):
            raise RuleError("invalid_rule")
        try:
            data = {name: getattr(rule, name) for name in ("id", "keywords", "patterns", "paths",
                                                         "case_sensitive", "exclude")}
        except AttributeError:
            raise RuleError("invalid_rule") from None
        identity = data["id"]
        if (not isinstance(identity, str) or len(identity) > 64
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", identity) or identity in identities
                or type(data["case_sensitive"]) is not bool or type(data["exclude"]) is not bool):
            raise RuleError("invalid_rule")
        for name, limit in (("keywords", 64), ("patterns", 32), ("paths", 64)):
            terms = data[name]
            if (not isinstance(terms, list) or len(terms) > limit
                    or any(not isinstance(term, str) or not 0 < len(term) <= 1024 for term in terms)):
                raise RuleError("invalid_rule")
            try:
                for term in terms:
                    term.encode("utf-8")
            except UnicodeError:
                raise RuleError("invalid_rule") from None
        if (not any(data[name] for name in ("keywords", "patterns", "paths"))
                or any(not _safe_path(pattern) for pattern in data["paths"])):
            raise RuleError("invalid_rule")
        identities.add(identity)
        selected.append(data)
    return selected


def _fragment(value):
    result = []
    for char in value:
        if not unicodedata.category(char).startswith("C"):
            result.append(char)
            if len(result) == 256:
                break
    return "".join(result)


def _keyword_fragment(value, keyword, sensitive, folded):
    if sensitive:
        position = value.find(keyword)
        return None if position < 0 else value[position:position + len(keyword)]
    wanted = keyword.casefold()
    position = folded.find(wanted)
    if position < 0:
        return None
    # Casefold can expand characters, so folded offsets are not original-text offsets.
    offset = 0
    start = None
    for index, char in enumerate(value):
        length = len(char.casefold())
        if start is None and offset + length > position:
            start = index
        offset += length
        if offset >= position + len(wanted):
            return value[start:index + 1]
    return None


def _evaluate(payload):
    compiled = []
    for rule in payload["rules"]:
        flags = 0 if rule["case_sensitive"] else re.IGNORECASE
        try:
            patterns = [re.compile(pattern, flags) for pattern in rule["patterns"]]
        except (re.PatternError, RecursionError, OverflowError):
            raise RuleError("invalid_regex") from None
        paths = []
        if rule["paths"]:
            import glob

            paths = [re.compile(glob.translate(pattern, recursive=True, include_hidden=True, seps="/"))
                     for pattern in rule["paths"]]
        compiled.append((rule, patterns, paths))

    result = []
    seen = set()
    output_size = len(b'{"matches":[]}')

    def append(rule, field, value, path=None):
        nonlocal output_size
        fragment = _fragment(value)
        key = (rule["id"], field, fragment, path)
        if key in seen:
            return
        item = {"rule": rule["id"], "field": field, "fragment": fragment, "path": path}
        output_size += len(_encoded(item)) + 1
        if output_size > _OUTPUT_LIMIT:
            raise RuleError("output_limit")
        seen.add(key)
        result.append(item)

    for rule, patterns, path_patterns in sorted(compiled, key=lambda entry: not entry[0]["exclude"]):
        matched_paths = [path for path in payload["commit"]["paths"]
                         if any(pattern.fullmatch(path) for pattern in path_patterns)]
        if path_patterns and not matched_paths:
            continue
        text_matches = []
        for field in ("subject", "body"):
            value = payload["commit"][field]
            folded = value.casefold() if rule["keywords"] and not rule["case_sensitive"] else value
            for keyword in rule["keywords"]:
                fragment = _keyword_fragment(value, keyword, rule["case_sensitive"], folded)
                if fragment is not None:
                    text_matches.append((field, fragment))
            for pattern in patterns:
                found = pattern.search(value)
                if found is not None:
                    text_matches.append((field, found.group(0)))
        if (rule["keywords"] or patterns) and not text_matches:
            continue
        if rule["exclude"]:
            return []
        for field, fragment in text_matches:
            append(rule, field, fragment)
        for path in matched_paths:
            append(rule, "path", path, path)
    return result


def _worker_main():
    try:
        raw = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
        if len(raw) > _INPUT_LIMIT:
            raise RuleError("input_limit")
        response = {"matches": _evaluate(json.loads(raw))}
        output = _encoded(response)
        if len(output) > _OUTPUT_LIMIT:
            raise RuleError("output_limit")
    except RuleError as error:
        output = _encoded({"error": error.code})
    except Exception:
        output = _encoded({"error": "worker_error"})
    sys.stdout.buffer.write(output)


def _exchange(process, raw: bytes, deadline: float) -> bytearray:
    import os
    import selectors
    import subprocess
    import time

    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        raise RuleError("worker_error")
    output = bytearray()
    offset = 0
    with selectors.DefaultSelector() as selector:
        for pipe, event in ((stdin, selectors.EVENT_WRITE), (stdout, selectors.EVENT_READ)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, event)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuleError("regex_timeout")
            events = selector.select(remaining)
            if not events:
                raise RuleError("regex_timeout")
            for key, _ in events:
                if key.fileobj is stdin:
                    try:
                        offset += os.write(stdin.fileno(), raw[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        offset = len(raw)
                    if offset == len(raw):
                        selector.unregister(stdin)
                        stdin.close()
                else:
                    try:
                        chunk = os.read(stdout.fileno(), min(65536, _OUTPUT_LIMIT + 1 - len(output)))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stdout)
                        stdout.close()
                    else:
                        output.extend(chunk)
                        if len(output) > _OUTPUT_LIMIT:
                            raise RuleError("output_limit")
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RuleError("regex_timeout") from None
    return output


def _run_worker(raw, timeout_seconds):
    import os
    import subprocess
    import time

    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", os.path.abspath(__file__)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, close_fds=True,
        )
    except (OSError, ValueError):
        raise RuleError("worker_error") from None
    try:
        output = _exchange(process, raw, deadline)
        if process.returncode:
            raise RuleError("worker_error")
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError):
            raise RuleError("worker_error") from None
        if not isinstance(response, dict):
            raise RuleError("worker_error")
        if set(response) == {"error"} and isinstance(response["error"], str):
            raise RuleError(response["error"])
        if set(response) != {"matches"} or not isinstance(response["matches"], list):
            raise RuleError("worker_error")
        return response["matches"]
    except OSError:
        raise RuleError("worker_error") from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()


def match_rules(commit: dict, rules: list[Rule], *, timeout_seconds=1, text_limit=524288) -> list[dict]:
    """Union positive rules; a matching exclusion overrides every positive rule.

    Text and paths intersect within a rule; keywords and regexes are alternatives.
    Text defaults to case-insensitive (Unicode casefold for keywords, re.IGNORECASE
    for regexes). Repository-relative globs are always case-sensitive; * and ? do
    not cross '/', ** matches recursive segments, including hidden paths.
    Text reasons retain subject/body with path=None; matched paths have field='path'.
    Missing commit fields default to empty values. Fragments retain original text,
    omit Unicode control/format characters, and contain at most 256 characters.

    timeout_seconds must be a finite number in (0, 10], excluding bool; it bounds
    the entire worker wall time, including startup and IPC. text_limit must be an
    integer in [1, 1048576], excluding bool; subject+body UTF-8 bytes are inclusive.
    At most 128 rules and 4096 paths (4096 characters each) are accepted. Total JSON
    input is capped at 2 MiB and output at 1 MiB; overflow raises rather than truncates.
    Every regex is compiled before any match-based short circuit, inside the worker.
    RuleError.code is one of _ERROR_CODES; the message never contains input data.
    """
    import math

    if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 10
            or not math.isfinite(timeout_seconds) or type(text_limit) is not int or not 1 <= text_limit <= 1048576):
        raise RuleError("invalid_budget")
    data = _commit_data(commit, text_limit)
    selected = _rule_data(rules)
    raw = _encoded({"commit": data, "rules": selected})
    if len(raw) > _INPUT_LIMIT:
        raise RuleError("input_limit")
    if not selected:
        return []
    matches = _run_worker(raw, timeout_seconds)
    identities = {rule["id"] for rule in selected if not rule["exclude"]}
    paths = set(data["paths"])
    for item in matches:
        if (not isinstance(item, dict) or set(item) != {"rule", "field", "fragment", "path"}
                or not isinstance(item["rule"], str) or item["rule"] not in identities
                or item["field"] not in ("subject", "body", "path")
                or not isinstance(item["fragment"], str) or len(item["fragment"]) > 256
                or any(unicodedata.category(char).startswith("C") for char in item["fragment"])
                or (item["field"] == "path" and (not isinstance(item["path"], str) or item["path"] not in paths))
                or (item["field"] != "path" and item["path"] is not None)):
            raise RuleError("worker_error")
    return matches


if __name__ == "__main__":
    _worker_main()
