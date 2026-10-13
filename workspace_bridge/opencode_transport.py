"""Owned OpenCode v2 stdio server. Credentials never cross the WBRP boundary."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from .claude_host_adapter import AdapterFailure
from .login_path import runtime_env_with_login_path

# Native message snapshots can include four admitted 2 MiB image attachments.
MAX_NATIVE_BYTES = 16 * 1024 * 1024


class NativeTransport:
    def __init__(self, state: Path, *, binary: str | None = None,
                 environment: dict | None = None, plugin_options: dict | None = None):
        self.state = state
        self.env = dict(environment) if environment is not None else runtime_env_with_login_path()[0]
        self.binary = binary or shutil.which('opencode2', path=self.env.get('PATH')) or shutil.which(
            'opencode', path=self.env.get('PATH'))
        if not self.binary:
            raise AdapterFailure('OpenCode v2 executable was not found', 503, 'runtime_unavailable')
        # Keep native configuration/database/provider credentials in place. Bridge
        # and child-server credentials are not part of the native tool environment.
        for key in list(self.env):
            if key.startswith(('WB_', 'WORKSPACE_BRIDGE_')):
                self.env.pop(key)
        self.password = secrets.token_urlsafe(32)
        self.env['OPENCODE_PASSWORD'] = self.password
        self.env.pop('OPENCODE_SERVER_PASSWORD', None)
        try:
            overlay = json.loads(self.env.get('OPENCODE_CONFIG_CONTENT', '{}'))
            if not isinstance(overlay, dict):
                raise ValueError()
        except ValueError:
            raise AdapterFailure('OpenCode configuration overlay is invalid') from None
        plugins = overlay.get('plugins', [])
        if not isinstance(plugins, list):
            raise AdapterFailure('OpenCode plugin configuration is invalid')
        overlay.update({'update': 'disable', 'share': 'disabled', 'snapshots': False})
        overlay['plugins'] = [*plugins, {'package': Path(__file__).with_name(
            'opencode_permission_plugin').as_uri(), 'options': plugin_options or {}}]
        self.env['OPENCODE_CONFIG_CONTENT'] = json.dumps(overlay)
        self.process = None
        self.url = ''
        self.version = ''
        self.lock = threading.Lock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            try:
                self._start_owned()
            except BaseException as exc:
                self.close()
                if isinstance(exc, (AdapterFailure, KeyboardInterrupt, SystemExit)):
                    raise
                raise AdapterFailure('OpenCode v2 server could not start', 503,
                                     'runtime_unavailable') from None

    def _start_owned(self):
        self.process = subprocess.Popen([self.binary, 'serve', '--stdio', '--log-level', 'error', '--hostname',
            '127.0.0.1', '--port', '0'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=self.env, cwd=str(self.state))
        process = self.process
        ready = threading.Event()
        result = []
        def read_ready():
            try:
                line = process.stdout.readline(8193)
                if len(line) <= 8192:
                    result.append(json.loads(line))
            except (ValueError, OSError):
                pass
            finally:
                ready.set()
        threading.Thread(target=read_ready, daemon=True).start()
        if not ready.wait(40) or not result:
            self.close()
            raise AdapterFailure('OpenCode v2 server did not become ready', 503,
                                 'runtime_unavailable')
        parsed = urllib.parse.urlsplit(result[0].get('url', ''))
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            self.close()
            raise AdapterFailure('OpenCode v2 server returned an invalid address', 503,
                                 'runtime_unavailable')
        self.url = result[0]['url'].rstrip('/')
        info = self.request('GET', '/api/info')
        self.version = str(info.get('version') or info.get('data', {}).get('version') or '')[:80]
        if not self.version.lstrip('v').startswith('2.'):
            self.close()
            raise AdapterFailure('OpenCode v2 is required', 503, 'runtime_unavailable')
        secret_path = self.state / 'native-connection.json'
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump({'url': self.url, 'password': self.password, 'binary': self.binary,
                       'pid': self.process.pid}, f)

    def request(self, method, path, body=None, *, query=None, sse=False):
        if not self.url or not path.startswith('/api/'):
            raise AdapterFailure('OpenCode server is unavailable', 503, 'runtime_unavailable')
        url = self.url + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        if data and len(data) > 12 * 1024 * 1024:
            raise AdapterFailure('Native request is too large', 413, 'too_large')
        headers = {'Authorization': 'Basic ' + base64.b64encode(
            ('opencode:' + self.password).encode()).decode(),
            'Accept': 'text/event-stream' if sse else 'application/json'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with self._opener.open(urllib.request.Request(url, data=data, headers=headers,
                                                        method=method), timeout=30) as response:
                raw = response.read(MAX_NATIVE_BYTES + 1)
                if len(raw) > MAX_NATIVE_BYTES:
                    raise AdapterFailure('Native response is too large', 502, 'runtime_unavailable')
                if sse:
                    return [json.loads(line[5:].strip()) for line in raw.decode().splitlines()
                            if line.startswith('data:')]
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # Native errors can contain provider credentials, prompts and tool args.
            raise AdapterFailure('OpenCode rejected the operation',
                                 409 if exc.code == 409 else 404 if exc.code == 404 else 502,
                                 'native_rejected') from None
        except (OSError, ValueError, urllib.error.URLError):
            raise AdapterFailure('OpenCode request outcome is unavailable', 503,
                                 'runtime_unavailable') from None

    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        process = self.process
        if process is not None and process.poll() is None:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if process is not None:
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
        self.process = None
        self.url = ''
