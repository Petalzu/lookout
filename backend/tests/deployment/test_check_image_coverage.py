# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'deploy/check-image.sh'


@pytest.mark.parametrize('collect,fail', [(False, False), (True, False), (True, True)])
def test_report_export_preserves_directory_permissions(tmp_path, collect, fail):
    engine = tmp_path / 'docker'
    engine.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ENGINE_ARGS"\n'
                      'printf "<coverage/>\\n"\nexit "$ENGINE_EXIT"\n')
    engine.chmod(0o700)
    output = tmp_path / 'reports'
    output.mkdir(mode=0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               CONTAINER_ENGINE='docker', ENGINE_ARGS=str(tmp_path / 'args'),
               ENGINE_EXIT='7' if fail else '0', COVERAGE_OUTPUT_DIR=str(output) if collect else '')
    result = subprocess.run(['sh', str(SCRIPT), 'runtime', 'check'], env=env, capture_output=True)
    assert result.returncode == (7 if fail else 0)
    assert output.stat().st_mode & 0o777 == 0o700
    assert not list(output.glob('.coverage-*'))
    report = output / 'backend-coverage.xml'
    assert report.exists() == (collect and not fail)
    if report.exists():
        assert report.read_text() == '<coverage/>\n'
        assert report.stat().st_mode & 0o777 == 0o600
    args = (tmp_path / 'args').read_text().splitlines()
    assert not any(arg.endswith(':rw,Z') for arg in args)
    assert f'COLLECT_COVERAGE={"1" if collect else ""}' in args


def test_fork_coverage_capability_does_not_gate_native_tests_or_artifacts():
    root = SCRIPT.parents[1]
    workflow = (root / '.github/workflows/ci.yml').read_text()
    native = workflow.split('  native:', 1)[1].split('  upload-coverage-python:', 1)[0]
    upload = workflow.split('  upload-coverage-python:', 1)[1].split('  result:', 1)[0]
    result = workflow.split('  result:', 1)[1]
    assert 'LOOKOUT_CODE_QUALITY_ENABLED' not in native
    assert 'COVERAGE_OUTPUT_DIR: ${{ runner.temp }}/coverage' in native
    assert 'if-no-files-found: error' in native
    assert "!github.event.repository.fork || vars.LOOKOUT_CODE_QUALITY_ENABLED == 'true'" in upload
    assert "needs.native.result == 'success'" in upload
    assert 'continue-on-error' not in upload
    assert 'test "$NATIVE" = success' in result
