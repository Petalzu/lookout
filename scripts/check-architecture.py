#!/usr/bin/env python3
# Guards this project's own invariants, in the spirit of openRuyi's pre-commit
# pygrep hooks. Pure standard library so it runs anywhere the repo is checked out.
#
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Fail if a maintainability invariant drifts.

1. Read/write separation: the read path (api, view) must never import the
   write/network path (collector, obs, nv, native_spec).
2. Thin config: package policies must hold no label-only binding whose label
   equals the value derived from the package name. Redundant bindings belong in
   the naming rule, not in the file.
3. No pinned versions: the native nvchecker config must not carry manual/hardcoded
   versions. Observations come from external sources, never from a checked-in value.
4. Preserve Anitya ordering: do not feed its already-sorted stable history back
   through nvchecker's different generic version comparator.
"""
import ast
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from tracker.monitors.version.rules import load
from tracker import catalog
# Side-effect-free monitor types and comparisons can be read without collecting.
PURE_MONITORS = {
    'tracker.monitors.github.model',
    'tracker.monitors.commits.model',
    'tracker.monitors.contract', 'tracker.monitors.model', 'tracker.monitors.schedule',
    'tracker.monitors.issues', 'tracker.monitors.observations',
    'tracker.monitors.build.status', 'tracker.monitors.version.compare',
    'tracker.monitors.version.rules', 'tracker.monitors.source.release',
    'tracker.monitors.requires.model', 'tracker.monitors.requires.compare',
    'tracker.monitors.requires.markers',
}
WRITE_PATH = {'tracker.collector', 'tracker.package', 'tracker.config_change', 'tracker.runtime_checks'}
READ_PREFIXES = ('tracker.readmodel', 'tracker.presentation')
EXTERNAL_IO = {'httpx', 'requests', 'urllib.request', 'subprocess', 'nvchecker', 'nvchecker_source'}


def _runtime_imports(tree):
    """TYPE_CHECKING dependencies do not execute when a read module is imported."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name) and node.test.id == 'TYPE_CHECKING'
            or isinstance(node.test, ast.Attribute) and node.test.attr == 'TYPE_CHECKING'
        ):
            for child in node.orelse:
                yield from _runtime_imports(ast.Module(body=[child], type_ignores=[]))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        else:
            yield from _runtime_imports(node)


def _imported_submodules(node, module, packages, available):
    """Resolve complete module names, including executed package initializers."""
    dependencies = set()
    if isinstance(node, ast.Import):
        dependencies.update(alias.name for alias in node.names)
    else:
        base = node.module or ''
        if node.level:
            package = module if module in packages else module.rpartition('.')[0]
            prefix = package.split('.')[:len(package.split('.')) - node.level + 1]
            base = '.'.join([*prefix, base]).rstrip('.')
        dependencies.add(base)
        dependencies.update(base + '.' + alias.name for alias in node.names
                            if base + '.' + alias.name in available)
    loaded = set()
    for dependency in dependencies:
        if any(dependency == name or dependency.startswith(name + '.') for name in EXTERNAL_IO):
            loaded.add(dependency)
        parts = dependency.split('.')
        loaded.update('.'.join(parts[:length]) for length in range(1, len(parts) + 1)
                      if '.'.join(parts[:length]) in available)
    return loaded


def check_read_write_separation():
    root = ROOT / 'backend/tracker'
    sources, packages = {}, set()
    for path in sorted(root.rglob('*.py')):
        parts = path.relative_to(root).with_suffix('').parts
        if parts[-1] == '__init__':
            name = '.'.join(('tracker', *parts[:-1]))
            packages.add(name)
        else:
            name = '.'.join(('tracker', *parts))
        sources[name] = ast.parse(path.read_text(), str(path))
    graph = {
        name: set().union(*(_imported_submodules(node, name, packages, sources)
                           for node in _runtime_imports(tree)))
        for name, tree in sources.items()
    }

    def collects(name):
        if any(name == boundary or name.startswith(boundary + '.') for boundary in EXTERNAL_IO):
            return True
        if name in packages:
            return False  # Its initializer is traversed, not assumed harmless.
        if name in WRITE_PATH:
            return True
        if name.startswith('tracker.providers.'):
            return name != 'tracker.providers.model'
        return name.startswith('tracker.monitors.') and name not in PURE_MONITORS

    errors = []
    starts = [name for name in graph if name == 'tracker.api'
              or any(name == prefix or name.startswith(prefix + '.') for prefix in READ_PREFIXES)]
    for start in sorted(starts):
        todo, visited = [(start, [start])], set()
        while todo:
            name, chain = todo.pop()
            if name in visited:
                continue
            visited.add(name)
            for dependency in sorted(graph.get(name, ())):
                if collects(dependency):
                    errors.append('read path reaches write module: ' + ' -> '.join([*chain, dependency]))
                elif dependency in graph:
                    todo.append((dependency, [*chain, dependency]))
    return errors


def check_thin_config():
    errors = []
    path = ROOT / 'config/tracker.toml'
    cfg = tomllib.loads(catalog.read_input(path).decode())
    catalog.load(cfg, path, {})
    rule = re.compile(r'-(\d+(?:\.\d+)*)$')
    for name, binding in cfg.get('packages', {}).items():
        if set(binding) != {'track_label'}:
            continue
        m = rule.search(name)
        derived = f'{m.group(1)}.x' if m else 'stable'
        if binding['track_label'] == derived:
            errors.append(f'{cfg["packages_path"]} [{name}] label "{binding["track_label"]}" is derivable; drop it')
    return errors


def check_no_pinned_versions():
    errors = []
    native = load((ROOT / 'config/versions/nvchecker.toml')).entries
    for name, entry in native.items():
        if name == '__config__':
            continue
        if isinstance(entry, dict) and entry.get('source') == 'manual':
            errors.append(f'["{name}"] uses source="manual"; observations must come from an external source')
    return errors


def check_provider_order():
    errors = []
    native = load((ROOT / 'config/versions/nvchecker.toml')).entries
    for name, entry in native.items():
        if (entry.get('source') == 'jq'
                and entry.get('url', '').startswith('https://release-monitoring.org/api/v2/versions/')
                and re.fullmatch(r'\s*\.stable_versions\s*\[\s*\]\s*', entry.get('filter', ''))):
            errors.append(f'["{name}"] re-sorts Anitya history; select the first eligible provider-ordered version in jq')
    return errors


def main():
    errors = (check_read_write_separation() + check_thin_config()
              + check_no_pinned_versions() + check_provider_order())
    for e in errors:
        print(f'architecture: {e}', file=sys.stderr)
    if errors:
        print(f'{len(errors)} invariant violation(s)', file=sys.stderr)
        return 1
    print('architecture invariants: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
