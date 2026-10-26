#!/usr/bin/env python3
"""Generated hosted cold-start A/B diagnostics; do not edit the checkout.

Run exactly one mode per fresh hosted VM after the unchanged general test gate.
Both modes execute the unchanged three theme tests once, in their original order.
The only candidate intervention is dbus-run-session inside run_browser's minimal
child environment; wrapping pytest outside that boundary would lose its address.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Observe:
    def __init__(self, root, output, mode, observer, session):
        self.root, self.output, self.mode = root, output, mode
        self.observer, self.session = observer, session
        self.records = []
        self.original_run = subprocess.run
        self.module = None
        self.original_module_subprocess = None

    def pytest_collection_modifyitems(self, items):
        expected = [
            'test_first_install_keeps_explicit_theme_and_control_offline_without_http_cache',
            'test_workspace_keyboard_focus_uses_accessible_theme_color[light]',
            'test_workspace_keyboard_focus_uses_accessible_theme_color[dark]',
        ]
        actual = [item.name for item in items]
        assert actual == expected, actual
        self.module = items[0].module
        self.original_module_subprocess = self.module.subprocess
        # Replace only this module's subprocess reference, never the global module.
        self.module.subprocess = SimpleNamespace(run=self.run)

    def run(self, argv, **kwargs):
        assert Path(argv[1]) == self.root / 'tests/js/test_ui_theme_browser.cjs'
        scenario = argv[-1]
        directory = self.output / scenario
        directory.mkdir(mode=0o700)
        observation = directory / 'startup.json'
        environment = dict(kwargs['env'])
        assert 'DBUS_SESSION_BUS_ADDRESS' not in environment
        assert 'NODE_OPTIONS' not in environment
        environment['FN_STARTUP_OBSERVATION'] = str(observation)
        command = [argv[0], '--require', str(self.observer), *argv[1:]]
        if self.mode == 'private-session':
            assert sys.platform == 'linux', 'candidate is Linux-only'
            command = [self.session, '--', *command]
        started = time.monotonic()
        result = self.original_run(command, **{**kwargs, 'env': environment})
        profile = Path(argv[-2])
        stderr_path = Path(str(profile) + '.stderr.log')
        if stderr_path.exists():
            shutil.copyfile(stderr_path, directory / 'chrome.stderr.log')
            (directory / 'chrome.stderr.log').chmod(0o600)
        (directory / 'stdout.log').write_text(result.stdout)
        (directory / 'stderr.log').write_text(result.stderr)
        self.records.append({'scenario': scenario, 'argv': command, 'cwd': str(kwargs['cwd']),
                             'environmentKeys': sorted(environment), 'returncode': result.returncode,
                             'elapsedSeconds': time.monotonic() - started,
                             'observationExists': observation.exists()})
        return result

    def restore(self):
        if self.module:
            self.module.subprocess = self.original_module_subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['baseline', 'private-session'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    observer = Path(__file__).with_name('startup-observer.cjs').resolve()
    source_paths = ['tests/js/test_ui_theme_browser.cjs', 'tests/test_ui_theme_browser.py',
                    'tests/test_ui_theme_browser_startup.py', '.github/workflows/ci.yml',
                    'tests/test_ci_browser.py']
    source = {name: {'sha256': digest(root / name), 'mode': oct((root / name).stat().st_mode & 0o777)}
              for name in source_paths}
    session = shutil.which('dbus-run-session')
    if args.mode == 'private-session':
        assert session and shutil.which('dbus-daemon'), 'use existing runner tools; do not install anything'
    identity = {'mode': args.mode, 'source': source, 'probeSha256': digest(Path(__file__)),
                'observerSha256': digest(observer), 'platform': sys.platform,
                'kernel': os.uname().release, 'python': sys.version, 'sessionTool': session}
    policy = Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')
    identity['apparmorRestriction'] = policy.read_text().strip() if policy.exists() else None
    for label, command in [('gitHead', ['git', 'rev-parse', 'HEAD']),
                           ('gitTree', ['git', 'rev-parse', 'HEAD^{tree}'])]:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
        identity[label] = result.stdout.strip()
    tools = ['dbus-run-session', 'dbus-daemon', 'dbus-send', 'strace', 'gdb']
    identity['toolPaths'] = {name: shutil.which(name) for name in tools}
    (output / 'identity.json').write_text(json.dumps(identity, indent=2) + '\n')
    sys.path.insert(0, str(root))
    os.chdir(root)
    import pytest
    plugin = Observe(root, output, args.mode, observer, session)
    try:
        status = int(pytest.main(['-q', '-rA', '-s', 'tests/test_ui_theme_browser.py',
                                 '--basetemp=' + str(output / 'generated'),
                                 '--junitxml=' + str(output / 'junit.xml')], plugins=[plugin]))
    finally:
        plugin.restore()
        unchanged = all(digest(root / name) == entry['sha256'] for name, entry in source.items())
        (output / 'commands.json').write_text(json.dumps(plugin.records, indent=2) + '\n')
        (output / 'source-unchanged.json').write_text(json.dumps({'unchanged': unchanged}) + '\n')
    assert unchanged, 'probe must not modify source'
    assert len(plugin.records) == 3, plugin.records
    assert all(record['observationExists'] for record in plugin.records), plugin.records
    return status


if __name__ == '__main__':
    raise SystemExit(main())
