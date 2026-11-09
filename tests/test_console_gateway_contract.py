"""Opt-in contract check against a real Hermes gateway, not a permissive RPC mock.

Run with CMI_HERMES_SOURCE pointing at the installed Hermes source tree.
The subprocess uses a throwaway home and never touches live sessions/credentials.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


def test_console_rpcs_exist_in_native_gateway(tmp_path):
    source = os.environ.get("CMI_HERMES_SOURCE")
    if not source:
        pytest.skip("Set CMI_HERMES_SOURCE for the installed-gateway contract check")
    shell = Path(__file__).parents[1] / "console-ui/src/shell-hermes.js"
    methods = sorted(set(re.findall(r"host\.request\('([^']+)'", shell.read_text())))
    env = {**os.environ, "HERMES_HOME": str(tmp_path),
           "PYTHONPATH": os.pathsep.join([source, *(p for p in sys.path if p)])}
    script = '''
import json
# Use the already installed dependencies: a contract test must not run the updater.
from hermes_cli import venv_sync
venv_sync.prepare_launch = lambda *a, **kw: None
from pm import environments
environments.activate_dependencies = lambda *a, **kw: None
import tui_gateway.server as server
methods = json.loads(__import__('sys').argv[1])
missing = [m for m in methods if m not in server._methods]
# Prove that the original mistake is rejected by the actual dispatcher.
rejected = server.handle_request({'jsonrpc': '2.0', 'id': 1,
    'method': 'session.info', 'params': {'session_id': 'contract-test'}})
import threading
server._sessions['contract-test'] = {
    'agent': None, 'agent_ready': threading.Event(), 'agent_error': None,
    'history': [], 'history_lock': threading.RLock(), 'running': False,
    'cancel_requested': False, 'session_key': '', 'cwd': __import__('os').environ['HERMES_HOME'],
    'created_at': 1.0, 'last_active': 1.0,
}
activated = server.handle_request({'jsonrpc': '2.0', 'id': 2,
    'method': 'session.activate', 'params': {'session_id': 'contract-test', 'omit_messages': True}})
print('CMI_CONTRACT=' + json.dumps({'missing': missing,
    'event_as_method_code': rejected.get('error', {}).get('code'), 'activated': activated}))
'''
    result = subprocess.run([sys.executable, "-c", script, json.dumps(methods)],
                            cwd=source, env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[-3000:]
    payload = next(line.removeprefix("CMI_CONTRACT=") for line in (result.stdout + result.stderr).splitlines()
                   if line.startswith("CMI_CONTRACT="))
    contract = json.loads(payload)
    assert contract["event_as_method_code"] == -32601
    assert contract["missing"] == [], f"Unknown RPC methods in installed Hermes: {contract['missing']}"
    assert "error" not in contract["activated"], contract["activated"]
    live = contract["activated"]["result"]
    assert live["session_id"] == "contract-test"
    assert live["info"]["model"]
    assert live["running"] is False
    assert live["messages_omitted"] is True
