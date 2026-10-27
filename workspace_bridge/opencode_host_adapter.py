"""OpenCode v2 host adapter, with shared native storage and exact message ownership.

The stdio server does not automatically resume suspended sessions. Admission is
journalled before native submission; ambiguous writes are only observed, never
repeated. The bundled plugin is an application permission boundary, not an OS
sandbox for user-installed native plugins or an explicitly approved shell.
"""
from __future__ import annotations

import base64
import fcntl
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from . import __version__
from . import security_profiles as profiles
from .run_roots import run_directory
from .claude_host_adapter import AdapterFailure, _clean, _id, _now
from .opencode_transport import NativeTransport

RUNTIME_ID = 'opencode'
_PLUGIN_ID = 'workspace-bridge.permissions.v1'


_READ = {'read', 'glob', 'grep', 'list', 'ls'}
_EDIT = {'write', 'edit', 'multiedit', 'apply_patch', 'patch'}
_SHELL = {'shell', 'bash'}
_WEB = {'webfetch', 'websearch', 'execute'}
_META = {'skill', 'subagent', 'task', 'question', 'form', 'todo', 'todowrite', 'todoread'}


def tool_decision(rules, cwd, tool, args, *, registered_extension=False):
    """Decide one OpenCode tool call against a run's profile rules."""
    if not isinstance(tool, str) or not isinstance(args, dict):
        return 'deny'
    category = ('read' if tool in _READ else 'edits' if tool in _EDIT else
                'shell' if tool in _SHELL else 'web' if tool in _WEB else
                'meta' if tool in _META else 'extensions' if registered_extension else None)
    if category is None:
        return 'deny'
    if category == 'meta':
        return 'allow'
    if category in ('web', 'extensions'):
        return rules[category]
    if category == 'shell':
        return profiles.command_mode(rules, args.get('command'))[0]
    write = category == 'edits'
    paths = [args[k] for k in ('path', 'filePath', 'file_path', 'directory') if k in args]
    patch = args.get('patchText', args.get('patch'))
    if tool in ('apply_patch', 'patch'):
        if not isinstance(patch, str):
            return 'deny'
        paths += re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$',
                            patch, flags=re.M)
    if tool in _EDIT | {'read', 'grep'} and not paths:
        return 'deny'
    if tool == 'glob':
        pattern = args.get('pattern')
        if not isinstance(pattern, str) or not pattern or '..' in pattern.split('/'):
            return 'deny'
        if pattern.startswith(('/', '~')):
            prefix = []
            for part in pattern.split('/'):
                if any(char in '*?[]{}' for char in part):
                    break
                prefix.append(part)
            paths.append('/'.join(prefix) or '/')
    if not paths:
        paths.append('.')
    effect = 'allow'
    for raw in paths:
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or '\0' in raw:
            return 'deny'
        resolved = Path(raw if os.path.isabs(raw) else os.path.join(cwd, raw)).resolve()
        # Native recursive grep includes hidden files and has no exclusion
        # input. Limit content searches to an explicitly admitted file.
        if tool == 'grep' and not resolved.is_file():
            return 'deny'
        if write and resolved.is_relative_to(Path(cwd)):
            parts = resolved.relative_to(cwd).parts
            if '.opencode' in parts or '.agents' in parts or any(
                    p in ('opencode.json', 'opencode.jsonc') for p in parts):
                return 'deny'
        mode = profiles.path_mode(rules, cwd, raw, write=write)[0]
        if mode == 'deny':
            return 'deny'
        effect = profiles.stricter(effect, mode)
    return effect


def permission_effect(rules, action):
    """Coarse answer for OpenCode's own permission check; ``/tool`` decides precisely."""
    if action in ('edit', 'write'):
        return min(rules['write_inside'], rules['write_outside'],
                   key=('allow', 'ask', 'deny').index)
    if action in ('shell', 'bash'):
        modes = [rules['shell']] + [rule['mode'] for rule in rules['commands']]
        return min(modes, key=('allow', 'ask', 'deny').index)
    if action in _WEB:
        return rules['web']
    if action in ('read', 'glob', 'grep', 'skill', 'subagent', 'question', 'todo'):
        return 'allow'
    return rules['extensions']


class OpenCodeHostAdapter:
    def __init__(self, state: Path, projects_root: Path, *, binary=None,
                 transport=None, environment=None, worktrees_root: Path | None = None):
        self.state = Path(state).resolve()
        self.projects_root = Path(projects_root).resolve(strict=True)
        self.worktrees_root = worktrees_root
        if not self.projects_root.is_dir() or self.state.is_relative_to(self.projects_root) or self.projects_root.is_relative_to(self.state):
            raise AdapterFailure('Keep adapter state outside the projects root')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        self._lock_fd = os.open(self.state / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise AdapterFailure('OpenCode adapter state is already owned', 409, 'conflict') from None
        if any((self.state / name).is_symlink() for name in ('opencode-adapter.sqlite3', 'opencode-adapter.sqlite3-wal', 'opencode-adapter.sqlite3-shm')):
            os.close(self._lock_fd)
            raise AdapterFailure('Unsafe adapter state')
        self.db = sqlite3.connect(self.state / 'opencode-adapter.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS identity(id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,session TEXT UNIQUE,
            workspace TEXT,cwd TEXT,profile TEXT,revision TEXT,started INTEGER DEFAULT 0,
            created TEXT,owner TEXT DEFAULT 'bridge');
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,conversation TEXT,client_run TEXT,
            input_hash TEXT,security_binding TEXT,message TEXT UNIQUE,log_seq INTEGER DEFAULT 0,
            phase TEXT,active_state TEXT,outcome TEXT,result TEXT DEFAULT '',error TEXT DEFAULT '',
            usage TEXT,created TEXT,updated TEXT, UNIQUE(conversation,client_run));
          CREATE UNIQUE INDEX IF NOT EXISTS active_conversation ON runs(conversation)
            WHERE phase IN ('starting','active');
          CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,run TEXT,native_id TEXT,
            kind TEXT,status TEXT,summary TEXT,result TEXT,created TEXT,updated TEXT,
            UNIQUE(run,native_id));
          CREATE TABLE IF NOT EXISTS input_messages(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            run TEXT,request_id TEXT,mode TEXT,input_hash TEXT,status TEXT,message TEXT UNIQUE,
            UNIQUE(run,request_id));
        ''')
        if 'rules' not in {r[1] for r in self.db.execute('PRAGMA table_info(conversations)')}:
            with self.db:
                self.db.execute("ALTER TABLE conversations ADD COLUMN rules TEXT NOT NULL DEFAULT '{}'")
        os.chmod(self.state / 'opencode-adapter.sqlite3', 0o600)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.instance_id = _id('opencode_instance_')
        row = self.db.execute('SELECT id FROM identity').fetchone()
        self.owner_id = row['id'] if row else _id('owner_')
        if row is None:
            with self.db:
                self.db.execute('INSERT INTO identity VALUES(?)', (self.owner_id,))
        self.cursor = 0
        self.events_buffer = []
        self.pending = {}
        self.ready = set()
        self._workspaces = {}
        self.generation = secrets.token_hex(16)
        self._closed = False
        self._threads = []
        self._cancelled = set()
        self._policy_token = secrets.token_urlsafe(32)
        credential = self.state / 'policy-key'
        fd = os.open(credential, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(self._policy_token)
        adapter = self
        class PolicyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                if not secrets.compare_digest(self.headers.get('X-Policy-Token', ''), adapter._policy_token):
                    self.send_error(401)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 128 * 1024:
                        raise ValueError()
                    body = json.loads(self.rfile.read(size))
                    if body.get('generation') != adapter.generation:
                        raise ValueError()
                    result = adapter._policy(self.path, body)
                    raw = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (ValueError, TypeError, AdapterFailure, OSError):
                    self.send_error(403)
        self.policy_server = ThreadingHTTPServer(('127.0.0.1', 0), PolicyHandler)
        self.policy_server.daemon_threads = True
        threading.Thread(target=self.policy_server.serve_forever, daemon=True).start()
        options = {'policyUrl': f'http://127.0.0.1:{self.policy_server.server_port}',
                   'credentialFile': str(credential), 'generation': self.generation}
        try:
            self.native = transport or NativeTransport(self.state, binary=binary,
                    environment=environment, plugin_options=options)
            self.native.start()
            self._recover()
        except BaseException:
            self.close()
            raise

    def _scrub(self, value):
        """Remove private adapter credentials before storage or publication."""
        if isinstance(value, str):
            for secret in (self._policy_token, getattr(self.native, 'password', '')):
                if secret:
                    value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, dict):
            return {self._scrub(key): self._scrub(item) for key, item in value.items()}
        return value

    def _emit(self, event_type, conversation, run='', activity='', interaction=''):
        with self.condition:
            self.cursor += 1
            self.events_buffer.append({'instanceId': self.instance_id, 'cursor': self.cursor,
                'type': event_type, 'conversationId': conversation, 'runId': run,
                'activityId': activity, 'interactionId': interaction})
            self.events_buffer = self.events_buffer[-1000:]
            self.condition.notify_all()

    def _policy(self, path, body):
        cwd = self._cwd(body.get('directory'))
        if path == '/ready':
            with self.condition:
                self.ready.add(cwd)
                self.condition.notify_all()
            return {'ready': True}
        sessions = body.get('sessions')
        if not isinstance(sessions, list) or len(sessions) > 32:
            return {'effect': 'deny'}
        with self.lock:
            row = next((self.db.execute('SELECT * FROM conversations WHERE session=?',
                        (s,)).fetchone() for s in sessions if isinstance(s, str) and
                        self.db.execute('SELECT 1 FROM conversations WHERE session=?', (s,)).fetchone()), None)
        if row is None or row['cwd'] != cwd:
            return {'effect': 'deny'}
        try:
            # The rules of the conversation's latest run, which also govern a
            # session handed off to a human until the Bridge reclaims it.
            rules = profiles.validate(json.loads(row['rules']))
        except Exception:
            return {'effect': 'deny'}
        if path == '/permission':
            effect = permission_effect(rules, body.get('action', ''))
            # Bridge wrapper approvals happen at execute.before. Native project
            # rules can still ask independently; manual ownership uses native ask.
            return {'effect': 'allow' if effect == 'ask' and row['owner'] == 'bridge' else effect}
        if path != '/tool':
            return {'effect': 'deny'}
        effect = tool_decision(rules, cwd, body.get('tool'), body.get('input'), registered_extension=body.get('registeredExtension') is True)
        if effect != 'ask' or row['owner'] == 'human':
            return {'effect': 'allow' if effect == 'ask' else effect}
        with self.lock:
            run = self.db.execute("SELECT * FROM runs WHERE conversation=? AND phase IN ('starting','active')",
                                  (row['id'],)).fetchone()
            if run is None:
                return {'effect': 'deny'}
            ident = _id('int_')
            gate = threading.Event()
            self.pending[ident] = {'event': gate, 'effect': 'deny', 'local': True,
                'public': {'id': ident, 'runId': run['id'], 'kind': 'choice', 'state': 'pending',
                    'title': 'OpenCode requests ' + _clean(body.get('tool'), 100),
                    'resource': '', 'choices': [{'id': 'approve', 'label': 'Approve once',
                     'semantic': 'approve'}, {'id': 'deny', 'label': 'Deny', 'semantic': 'deny'}],
                    'fields': None, 'nativeRequestId': _clean(body.get('nativeId'), 200)}}
            with self.db:
                self.db.execute("UPDATE runs SET phase='active',active_state='waiting_interaction' WHERE id=?", (run['id'],))
            self._emit('interaction.pending', row['id'], run['id'], interaction=ident)
        gate.wait(3600)
        with self.lock, self.db:
            entry = self.pending.pop(ident, None)
            waiting = any(e['public']['runId'] == run['id'] for e in self.pending.values())
            self.db.execute("UPDATE runs SET active_state=? WHERE id=? AND phase='active'", ('waiting_interaction' if waiting else 'running', run['id']))
        return {'effect': entry['effect'] if entry else 'deny'}

    def _activate(self, cwd):
        self.native.request('GET', '/api/location', query={'location[directory]': cwd})
        # Agent discovery boots the location's plugin supervisor asynchronously.
        self.native.request('GET', '/api/agent', query={'location[directory]': cwd})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            plugins = self.native.request('GET', '/api/plugin', query={'location[directory]': cwd}).get('data', [])
            if cwd in self.ready and any(p.get('id') == _PLUGIN_ID and p.get('state', {}).get('status') == 'active' for p in plugins):
                return
            time.sleep(.05)
        raise AdapterFailure('Trusted OpenCode permission plugin is unavailable', 503,
                             'security_enforcement_unavailable')

    def descriptor(self):
        alive = getattr(self.native, 'is_alive', None)
        if alive is not None and not alive():
            raise AdapterFailure('OpenCode v2 server is unavailable', 503, 'runtime_unavailable')
        return {'protocol': {'major': 1, 'minor': 0}, 'runtime': {'id': RUNTIME_ID,
            'displayName': 'OpenCode', 'adapterVersion': __version__,
            'nativeVersion': self.native.version or 'unknown', 'instanceId': self.instance_id},
            'features': {k: 1 for k in ('models', 'conversations', 'runs', 'activities',
                'interactions', 'events', 'steering', 'queueing',
                'inputControls', 'imageInput')}}

    def models(self, workspace_id=None):
        result = self.native.request('GET', '/api/model', query={
            'location[directory]': self._workspaces.get(workspace_id, str(self.projects_root))})
        default = self.native.request('GET', '/api/model/default', query={
            'location[directory]': self._workspaces.get(workspace_id, str(self.projects_root))}).get('data') or {}
        models = []
        for item in result.get('data', [])[:200]:
            if not item.get('enabled', True):
                continue
            selector = item['providerID'] + '/' + item['id']
            models.append({'selector': selector, 'displayName': _clean(item.get('name') or selector, 120),
                'inputModalities': [m for m in item.get('capabilities', {}).get('input', ['text'])
                                    if m in ('text', 'image')],
                'reasoningOptions': [v['id'] for v in item.get('variants', []) if 'id' in v],
                'defaultReasoningEffort': None,
                'default': item['id'] == default.get('id') and item['providerID'] == default.get('providerID')})
        return {'models': models}

    def activities(self, run_id: str) -> dict:
        self._run(run_id)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE run=? ORDER BY created,id LIMIT 1000",
                (run_id,)).fetchall()
        return {"activities": [self._activity_public(dict(row)) for row in rows]}

    @staticmethod
    def _activity_public(row: dict) -> dict:
        return {"id": row["id"], "runId": row["run"], "nativeId": row["native_id"],
                "kind": row["kind"], "status": row["status"],
                "input": {"summary": row["summary"]},
                "result": json.loads(row["result"]),
                "createdAt": row["created"], "updatedAt": row["updated"]}

    def activity(self, activity_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM activities WHERE id=?",
                                  (activity_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Activity not found", 404, "not_found")
        return self._activity_public(dict(row))

    def events(self, after: int, wait_ms: int) -> dict:
        wait_seconds = min(max(wait_ms, 0), 25000) / 1000
        with self.condition:
            if after >= self.cursor and wait_seconds:
                self.condition.wait(wait_seconds)
            events = [event for event in self.events_buffer if event["cursor"] > after]
            return {"instanceId": self.instance_id, "cursor": self.cursor,
                    "events": events[:100]}

    def _cwd(self, requested):
        if not isinstance(requested, str) or not requested:
            raise AdapterFailure('Workspace directory is required')
        try:
            return run_directory(requested, self.projects_root, self.worktrees_root)
        except (OSError, ValueError, RuntimeError):
            raise AdapterFailure('Workspace directory is outside the configured projects root') from None

    def _conversation(self, ident):
        with self.lock:
            row = self.db.execute('SELECT * FROM conversations WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise AdapterFailure('Conversation not found', 404, 'not_found')
        return dict(row)

    @staticmethod
    def _security(body):
        """The profile a conversation or run carries; every run names one."""
        try:
            return profiles.from_wire(body.get('security'))
        except Exception as exc:
            raise AdapterFailure(_clean(str(exc), 200) or 'Security profile is invalid',
                                 400, 'invalid_profile') from None

    @staticmethod
    def _binding(row):
        return {'source': 'profile', 'profile': {'id': row['profile'], 'revision': row['revision']}}

    def _verify(self, row):
        native = self.native.request('GET', '/api/session/' + row['session']).get('data', {})
        if native.get('metadata', {}).get('workspaceBridgeOwner') != self.owner_id or (
                str(Path(native.get('location', {}).get('directory', '')).resolve()) != row['cwd']):
            raise AdapterFailure('Native session ownership changed', 409, 'ownership_changed')
        return native

    def _is_busy(self, ident):
        row = self._conversation(ident)
        with self.lock:
            bridge = self.db.execute("SELECT 1 FROM runs WHERE conversation=? AND phase IN ('starting','active')",
                                      (ident,)).fetchone()
        if bridge or row['session'] in self.native.request('GET', '/api/session/active').get('data', {}):
            return True
        if self.native.request('GET', '/api/session/' + row['session'] + '/inbox').get('data', []):
            return True
        # The active map belongs to our server. A different native process can
        # be using the shared database, so require an idle transcript boundary.
        history = self.native.request('GET', '/api/session/' + row['session'] + '/message',
                                      query={'order': 'desc', 'limit': 100})
        latest = next((m for m in history.get('data', []) if m.get('type') not in (
            'agent-switched', 'model-switched', 'location-switched')), None)
        if latest is None:
            return bool(history.get('cursor', {}).get('next'))
        return latest.get('type') != 'idle' or latest.get('outcome') not in (
            'succeeded', 'failed', 'interrupted')

    def create_conversation(self, body):
        workspace = body.get('workspaceId')
        if not isinstance(workspace, str) or not workspace or len(workspace) > 100:
            raise AdapterFailure('Invalid workspace id')
        cwd = self._cwd(body.get('directory'))
        profile_id, revision, rules = self._security(body)
        self._workspaces[workspace] = cwd
        self._activate(cwd)
        ident, session = _id('conv_'), _id('ses_')
        with self.lock, self.db:
            self.db.execute('INSERT INTO conversations(id,session,workspace,cwd,profile,revision,rules,created) VALUES(?,?,?,?,?,?,?,?)',
                (ident, session, workspace, cwd, profile_id, revision, profiles.canonical_json(rules), _now()))
        # Store identity before the native create. An ambiguous create is never
        # repeated and cannot accidentally attach an existing user's session.
        self.native.request('POST', '/api/session', {'id': session,
            'location': {'directory': cwd}, 'title': 'Workspace Bridge',
            'metadata': {'workspaceBridgeOwner': self.owner_id},
            'permissions': [{'action': '*', 'resource': '*', 'effect': 'allow'}]})
        return self.conversation(ident)

    def conversation(self, ident):
        row = self._conversation(ident)
        self._verify(row)
        return {'id': ident, 'runtime': RUNTIME_ID, 'nativeId': row['session'],
            'workspaceId': row['workspace'],
            'securityBinding': self._binding(row), 'status': 'active' if self._is_busy(ident) else 'idle',
            'ownership': row['owner']}

    def _run(self, ident):
        with self.lock:
            row = self.db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise AdapterFailure('Run not found', 404, 'not_found')
        return dict(row)

    def _run_public(self, row):
        value = {'id': row['id'], 'conversationId': row['conversation'], 'clientRunId': row['client_run'],
            'nativeId': row['message'], 'phase': row['phase'], 'activeState': row['active_state'],
            'outcome': row['outcome'], 'result': row['result'], 'error': row['error'],
            'createdAt': row['created'], 'updatedAt': row['updated'],
            'securityBinding': json.loads(row['security_binding'])}
        with self.lock:
            images = self.db.execute("SELECT result FROM activities WHERE run=? AND kind='image' ORDER BY native_id", (row['id'],)).fetchall()
        if images:
            value['inputImages'] = [json.loads(r['result']) for r in images]
        if row['usage']:
            value['usage'] = json.loads(row['usage'])
        with self.lock:
            value['inputMessages'] = [{'requestId': r['request_id'], 'mode': r['mode'], 'status': r['status']}
                for r in self.db.execute('SELECT * FROM input_messages WHERE run=? ORDER BY sequence', (row['id'],))]
        return self._scrub(value)

    @staticmethod
    def _input(items):
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise AdapterFailure('Run input is required')
        texts, files = [], []
        for item in items:
            if not isinstance(item, dict):
                raise AdapterFailure('Invalid run input')
            if item.get('type') == 'text' and isinstance(item.get('text'), str) and item['text'].strip():
                texts.append(item['text'])
            elif item.get('type') == 'image' and item.get('mimeType') in ('image/png', 'image/jpeg', 'image/webp'):
                try:
                    raw = base64.b64decode(item['data'], validate=True)
                    if not raw or len(raw) > 2 * 1024 * 1024 or len(files) >= 4:
                        raise ValueError()
                    files.append({'uri': 'data:' + item['mimeType'] + ';base64,' + item['data']})
                except (ValueError, KeyError, TypeError):
                    raise AdapterFailure('Invalid image input') from None
            else:
                raise AdapterFailure('Unsupported run input', 400, 'unsupported_input')
        if not texts or sum(map(len, texts)) > 60000:
            raise AdapterFailure('Text input is missing or too long')
        return '\n\n'.join(texts), files

    def _log(self, session, after=0):
        return self.native.request('GET', '/api/experimental/session/' + session + '/log',
                                   query={'after': after, 'follow': 'false'}, sse=True)

    def start_run(self, ident, body):
        text, files = self._input(body.get('input'))
        client = body.get('clientRunId')
        if not isinstance(client, str) or not client or len(client) > 200:
            raise AdapterFailure('Client run id is required')
        digest = sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        profile_id, revision, rules = self._security(body)
        with self.lock:
            replay = self.db.execute('SELECT 1 FROM runs WHERE conversation=? AND client_run=?', (ident, client)).fetchone()
        if replay is None:
            # The permission plugin reports ready through the policy server,
            # which needs this lock, so activation happens before taking it.
            self._activate(self._conversation(ident)['cwd'])
        with self.lock:
            old = self.db.execute('SELECT * FROM runs WHERE conversation=? AND client_run=?', (ident, client)).fetchone()
            if old:
                if old['input_hash'] != digest:
                    raise AdapterFailure('Client run id has different input', 409, 'idempotency_conflict')
                return self._run_public(dict(old))
            row = self._conversation(ident)
            if row['owner'] != 'bridge':
                raise AdapterFailure('Native session is handed off; reclaim it when idle', 409, 'ownership_changed')
            if self._is_busy(ident):
                raise AdapterFailure('Conversation is busy', 409, 'conversation_busy')
            self._verify(row)
            # The workspace map is memory only; after a restart the catalog
            # must still come from this conversation's activated location.
            self._workspaces[row['workspace']] = row['cwd']
            catalog = self.models(row['workspace'])['models']
            selector = body.get('model')
            model = next((m for m in catalog if m['selector'] == selector), None) if selector else next(
                (m for m in catalog if m['default']), None)
            if model is None:
                raise AdapterFailure('Model is unavailable', 400, 'unsupported_model')
            if files and 'image' not in model['inputModalities']:
                raise AdapterFailure('Model does not support images', 400, 'unsupported_input')
            reasoning = body.get('reasoning')
            if reasoning is not None and reasoning not in model['reasoningOptions']:
                raise AdapterFailure('Model reasoning option is unavailable', 400, 'unsupported_reasoning')
            provider, model_id = model['selector'].split('/', 1)
            ref = {'providerID': provider, 'id': model_id}
            if reasoning is not None:
                ref['variant'] = reasoning
            self.native.request('POST', '/api/session/' + row['session'] + '/model', {'model': ref})
            inbox = self.native.request('GET', '/api/session/' + row['session'] + '/inbox').get('data', [])
            if inbox:
                raise AdapterFailure('Native session has pending input; use native handoff', 409, 'conversation_busy')
            log = self._log(row['session'])
            seq = max((e.get('durable', {}).get('seq', e.get('seq', 0)) for e in log), default=0)
            run_id, message, now = _id('run_'), _id('msg_'), _now()
            row.update(profile=profile_id, revision=revision)
            with self.db:
                self.db.execute('UPDATE conversations SET profile=?,revision=?,rules=? WHERE id=?',
                                (profile_id, revision, profiles.canonical_json(rules), ident))
                self.db.execute('INSERT INTO runs(id,conversation,client_run,input_hash,security_binding,message,log_seq,phase,created,updated) VALUES(?,?,?,?,?,?,?,\'starting\',?,?)',
                    (run_id, ident, client, digest, json.dumps(self._binding(row)), message, seq, now, now))
        for index, item in enumerate(body['input']):
            if item.get('type') == 'image' and isinstance(item.get('source'), dict):
                self._upsert_activity(self._run(run_id), 'image:' + str(index), 'image', 'completed', 'Run image preview', item['source'])
        # Write exactly once. Even a timeout leaves an admission that find_run can
        # reconcile; no retry here or during restart.
        try:
            self.native.request('POST', '/api/session/' + row['session'] + '/prompt', {
                'id': message, 'text': text, 'files': files,
                'metadata': {'workspaceBridgeRun': run_id}, 'delivery': 'steer'})
        except AdapterFailure:
            pass
        with self.lock, self.db:
            self.db.execute("UPDATE runs SET phase='active',active_state=COALESCE(active_state,'running'),updated=? WHERE id=? AND phase IN ('starting','active')", (_now(), run_id))
        self._emit('run.updated', ident, run_id)
        self._watch(run_id)
        return self._run_public(self._run(run_id))

    def _watch(self, run_id):
        def drive():
            misses = 0
            while not self._closed:
                try:
                    row = self._run(run_id)
                    if row['phase'] == 'terminal':
                        return
                    self._refresh(row)
                    misses = 0
                except AdapterFailure:
                    misses += 1
                    if misses >= 3:
                        self._finish(run_id, 'orphaned', 'native_state_unavailable')
                        return
                time.sleep(.25)
        thread = threading.Thread(target=drive, daemon=True, name='opencode-run-watch')
        self._threads.append(thread)
        thread.start()

    def _refresh(self, run):
        owned = self._conversation(run['conversation'])
        self._verify(owned)
        records = self._log(owned['session'], run['log_seq'])
        terminal = None
        with self.lock:
            admitted = {run['message']} | {r['message'] for r in self.db.execute(
                'SELECT message FROM input_messages WHERE run=?', (run['id'],))}
        for event in records:
            data, kind = event.get('data', {}), event.get('type', '')
            if kind == 'session.inbox.enqueued':
                inbox_id = data.get('inboxID') or data.get('item', {}).get('id')
                if inbox_id and inbox_id not in admitted:
                    with self.lock, self.db:
                        self.db.execute("UPDATE conversations SET owner='human' WHERE id=?", (owned['id'],))
                    self._finish(run['id'], 'orphaned', 'native_session_taken_over')
                    return
            if kind == 'session.inbox.delivered':
                with self.lock, self.db:
                    self.db.execute("UPDATE input_messages SET status='delivered' WHERE run=? AND message=?", (run['id'], data.get('inboxID')))
            if kind.startswith('session.execution.') and kind != 'session.execution.started':
                terminal = kind.rsplit('.', 1)[-1]
        # Require a durable execution event or a projected idle marker after
        # the exact admitted message. Inactive alone never means success.
        observed, native_error = self._capture_messages(run, owned)
        if self._run(run["id"])["phase"] == "terminal":
            return
        terminal = observed or terminal
        self._native_interactions(run, owned)
        if terminal:
            # Idle markers delimit native turns. Queued follow-up turns can
            # still be executing inside the same owned drain.
            if owned['session'] in self.native.request('GET', '/api/session/active').get('data', {}):
                return
            self._discard_inbox(owned, run)
            self._finish(run['id'], 'cancelled' if run['id'] in self._cancelled else terminal,
                         '' if terminal == 'succeeded' else native_error or 'native_execution_' + terminal)
        elif not records and run['phase'] == 'active':
            active = self.native.request('GET', '/api/session/active').get('data', {})
            inbox = self.native.request('GET', '/api/session/' + owned['session'] + '/inbox').get('data', [])
            if owned['session'] not in active and not any(i.get('id') == run['message'] for i in inbox):
                try:
                    self.native.request('GET', '/api/session/' + owned['session'] + '/message/' + run['message'])
                except AdapterFailure:
                    self._finish(run['id'], 'interrupted', 'submission_not_observed')

    def _capture_messages(self, run, owned):
        response = self.native.request('GET', '/api/session/' + owned['session'] + '/message',
                                       query={'order': 'desc', 'limit': 100})
        rows = response.get('data', [])
        while not any(r.get('id') == run['message'] for r in rows) and response.get('cursor', {}).get('next'):
            if len(rows) >= 2000:
                self._finish(run['id'], 'orphaned', 'native_history_limit')
                return None, ''
            response = self.native.request('GET', '/api/session/' + owned['session'] + '/message',
                query={'cursor': response['cursor']['next'], 'limit': 100})
            rows.extend(response.get('data', []))
        rows = list(reversed(rows))
        start = next((i for i, row in enumerate(rows) if row['id'] == run['message']), None)
        if start is None:
            return None, ''
        with self.lock:
            admitted = {run['message']} | {r['message'] for r in self.db.execute('SELECT message FROM input_messages WHERE run=?', (run['id'],))}
        texts, usage, terminal, error = [], {}, None, ''
        for row in rows[start + 1:]:
            if row.get('type') == 'user':
                if row['id'] not in admitted:
                    with self.lock, self.db:
                        self.db.execute("UPDATE conversations SET owner='human' WHERE id=?", (owned['id'],))
                    self._finish(run['id'], 'orphaned', 'native_session_taken_over')
                    return None, ''
                with self.lock, self.db:
                    self.db.execute("UPDATE input_messages SET status='delivered' WHERE run=? AND message=?", (run['id'], row['id']))
            if row.get('type') == 'idle' and row.get('outcome') in ('succeeded', 'failed', 'interrupted'):
                terminal = row['outcome']
                # OpenCode's own explanation, such as a provider usage limit;
                # only its message, never the provider response body.
                native = row.get('error')
                message = native.get('message') if isinstance(native, dict) else None
                error = _clean(self._scrub(message), 300) if isinstance(message, str) else ''
            if row.get('type') != 'assistant':
                continue
            for part in row.get('content', []):
                if part.get('type') == 'text':
                    texts.append(_clean(self._scrub(part.get('text')), 12000))
                elif part.get('type') == 'tool':
                    state = part.get('state', {})
                    status = {'completed': 'completed', 'error': 'failed'}.get(state.get('status'), 'running')
                    self._upsert_activity(run, part['id'], 'tool', status, _clean(part.get('name'), 160), {})
            if row.get('tokens'):
                native = row['tokens']
                mapped = {'inputTokens': native.get('input'), 'outputTokens': native.get('output'),
                    'reasoningOutputTokens': native.get('reasoning'),
                    'cachedInputTokens': native.get('cache', {}).get('read'),
                    'cacheWriteInputTokens': native.get('cache', {}).get('write')}
                for key, val in mapped.items():
                    if isinstance(val, int) and not isinstance(val, bool) and 0 <= val < 2**53:
                        usage[key] = usage.get(key, 0) + val
        if usage:
            usage['totalTokens'] = sum(usage.values())
        result = '\n\n'.join(texts)[-24000:]
        serialized_usage = json.dumps(usage) if usage else None
        with self.lock, self.db:
            current = self._run(run['id'])
            self.db.execute('UPDATE runs SET result=?,usage=?,updated=? WHERE id=?',
                (result, serialized_usage, _now(), run['id']))
        if current['result'] != result or current['usage'] != serialized_usage:
            self._emit('run.updated', run['conversation'], run['id'])
        return terminal, error

    def _upsert_activity(self, run, native_id, kind, status, summary, result):
        with self.lock, self.db:
            old = self.db.execute('SELECT id,status FROM activities WHERE run=? AND native_id=?',
                                  (run['id'], native_id)).fetchone()
            ident, now = old['id'] if old else _id('act_'), _now()
            self.db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run,native_id) DO UPDATE SET status=excluded.status,updated=excluded.updated',
                (ident, run['id'], native_id, kind, status, summary, json.dumps(result), now, now))
            if old is None or old['status'] != status:
                self._emit('activity.updated', run['conversation'], run['id'], activity=ident)

    def _native_interactions(self, run, owned):
        native_ids = set()
        for kind in ('permission', 'form'):
            items = self.native.request('GET', '/api/session/' + owned['session'] + '/' + kind).get('data', [])
            for item in items:
                if item.get('state', {}).get('status', 'pending') != 'pending':
                    continue
                native_ids.add(item['id'])
                with self.lock:
                    if any(e.get('native_id') == item['id'] for e in self.pending.values()):
                        continue
                    ident = _id('int_')
                    public = {'id': ident, 'runId': run['id'], 'kind': 'choice' if kind == 'permission' else 'form',
                        'state': 'pending', 'title': _clean(item.get('title') or item.get('action'), 200),
                        'resource': _clean(', '.join(item.get('resources', [])), 1000), 'nativeRequestId': item['id']}
                    if kind == 'permission':
                        public['choices'] = [{'id': 'approve', 'label': 'Approve once', 'semantic': 'approve'},
                                              {'id': 'deny', 'label': 'Deny', 'semantic': 'deny'}]
                    else:
                        public['fields'] = [{'id': f['key'], 'question': (f.get('title') or f['key']) + (
                            ' (JSON array of option values)' if f['type'] == 'multiselect' else
                            ' (true or false)' if f['type'] == 'boolean' else ''),
                            'description': _clean(f.get('description'), 500), 'required': f.get('required', False),
                            'nativeType': f['type'], 'options': f.get('options', [])} for f in item['fields']]
                    self.pending[ident] = {'public': public, 'native_id': item['id'], 'kind': kind,
                                           'session': owned['session'], 'native': item}
                    self._emit('interaction.pending', run['conversation'], run['id'], interaction=ident)
        with self.lock, self.db:
            for ident, entry in list(self.pending.items()):
                if entry['public']['runId'] == run['id'] and not entry.get('local') and entry['native_id'] not in native_ids:
                    self.pending.pop(ident)
                    self._emit('interaction.resolved', run['conversation'], run['id'], interaction=ident)
            waiting = any(e['public']['runId'] == run['id'] for e in self.pending.values())
            self.db.execute("UPDATE runs SET active_state=? WHERE id=? AND phase='active'",
                ('waiting_interaction' if waiting else 'running', run['id']))

    def interactions(self, ident):
        row = self._run(ident)
        with self.lock:
            return {'interactions': [e['public'] for e in self.pending.values()
                                    if e['public']['runId'] == ident and row['phase'] == 'active']}

    def resolve(self, ident, body):
        with self.lock:
            entry = self.pending.get(ident)
            if entry is None or entry['public']['state'] != 'pending' or entry['public']['runId'] in self._cancelled or self._run(entry['public']['runId'])['phase'] != 'active':
                raise AdapterFailure('Interaction is stale', 409, 'interaction_stale')
            if entry.get('local'):
                if body.get('choiceId') not in ('approve', 'deny'):
                    raise AdapterFailure('Invalid choice')
                entry['effect'] = 'allow' if body['choiceId'] == 'approve' else 'deny'
                entry['public']['state'] = 'resolved'
                entry['event'].set()
            else:
                base = '/api/session/' + entry['session'] + '/' + entry['kind'] + '/' + entry['native_id']
                if entry['kind'] == 'permission':
                    if body.get('choiceId') not in ('approve', 'deny'):
                        raise AdapterFailure('Invalid choice')
                    self.native.request('POST', base + '/reply', {'decision': 'once' if body['choiceId'] == 'approve' else 'reject'})
                else:
                    answer = {}
                    incoming = body.get('answers')
                    if not isinstance(incoming, dict):
                        raise AdapterFailure('Form answers are required')
                    for field in entry['native']['fields']:
                        def condition_matches(condition):
                            key = condition['key']
                            if key not in answer:
                                return False
                            value = answer[key]
                            equal = condition['value'] in value if isinstance(value, list) else value == condition['value']
                            return equal if condition['op'] == 'eq' else not equal
                        if not all(condition_matches(c) for c in field.get('when', [])):
                            continue
                        raw = incoming.get(field['key'], {}).get('answers', [])
                        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
                            raise AdapterFailure('Invalid form answer')
                        if not raw:
                            if field.get('required'):
                                raise AdapterFailure('Required form answer is missing')
                            continue
                        typ = field['type']
                        try:
                            if typ == 'multiselect' and len(raw) == 1 and raw[0].startswith('['):
                                raw = json.loads(raw[0])
                                if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
                                    raise ValueError()
                            val = (raw if typ == 'multiselect' else int(raw[0]) if typ == 'integer'
                                else float(raw[0]) if typ == 'number' else {'true': True, 'false': False}[raw[0].lower()]
                                if typ == 'boolean' else raw[0] if typ == 'string' else None)
                        except (ValueError, KeyError):
                            raise AdapterFailure('Invalid form value') from None
                        if val is None:
                            raise AdapterFailure('External form requires native handoff', 409, 'unsupported_form')
                        if isinstance(val, float) and not math.isfinite(val):
                            raise AdapterFailure('Invalid form value')
                        answer[field['key']] = val
                    self.native.request('POST', base + '/reply', {'answer': answer})
                self.pending.pop(ident)
            self._emit('interaction.resolved', self._run(entry['public']['runId'])['conversation'],
                       entry['public']['runId'], interaction=ident)
            return {'id': ident, 'runId': entry['public']['runId'], 'state': 'resolved'}

    def _finish(self, ident, outcome, error=''):
        if outcome not in ('succeeded', 'failed', 'cancelled', 'interrupted', 'orphaned'):
            outcome = 'interrupted'
        with self.lock, self.db:
            run = self._run(ident)
            if run['phase'] == 'terminal':
                return
            for key, entry in list(self.pending.items()):
                if entry['public']['runId'] == ident:
                    if entry.get('local'):
                        entry['event'].set()
                    self.pending.pop(key)
            self.db.execute("UPDATE runs SET phase='terminal',active_state=NULL,outcome=?,error=?,updated=? WHERE id=?",
                            (outcome, error, _now(), ident))
            self.db.execute("UPDATE input_messages SET status='unknown' WHERE run=? AND status='queued'", (ident,))
        self._emit('run.updated', run['conversation'], ident)

    def run(self, ident):
        return self._run_public(self._run(ident))

    def find_run(self, conversation, client):
        with self.lock:
            row = self.db.execute('SELECT * FROM runs WHERE conversation=? AND client_run=?', (conversation, client)).fetchone()
        if row is None:
            raise AdapterFailure('Run not found', 404, 'not_found')
        return self._run_public(dict(row))

    def cancel(self, ident):
        run = self._run(ident)
        if run['phase'] != 'terminal':
            owned = self._conversation(run['conversation'])
            self._verify(owned)
            if owned['owner'] != 'bridge':
                raise AdapterFailure('Session ownership changed', 409, 'ownership_changed')
            self._cancelled.add(ident)
            with self.lock:
                for entry in self.pending.values():
                    if entry['public']['runId'] == ident and entry.get('local'):
                        entry['effect'] = 'deny'
                        entry['event'].set()
            self._discard_inbox(owned, run)
            self.native.request('POST', '/api/session/' + owned['session'] + '/interrupt', {}, query={'resume': 'false'})
            self._finish(ident, 'cancelled')
        return self.run(ident)

    def _discard_inbox(self, owned, run):
        with self.lock:
            ids = {run['message']} | {r['message'] for r in self.db.execute('SELECT message FROM input_messages WHERE run=?', (run['id'],))}
        for item in self.native.request('GET', '/api/session/' + owned['session'] + '/inbox').get('data', []):
            if item['id'] in ids:
                self.native.request('DELETE', '/api/session/' + owned['session'] + '/inbox/' + item['id'])
        with self.lock, self.db:
            self.db.execute("UPDATE input_messages SET status='discarded' WHERE run=? AND status IN ('queued','unknown')", (run['id'],))

    def _control(self, ident, body, mode):
        if body.get('expectedRunId') != ident or not isinstance(body.get('requestId'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', body['requestId']):
            raise AdapterFailure('Invalid run input binding')
        text, files = self._input(body.get('input'))
        if files:
            raise AdapterFailure('Steering and queueing accept text only')
        digest = sha256(text.encode()).hexdigest()
        with self.lock:
            run = self._run(ident)
            old = self.db.execute('SELECT * FROM input_messages WHERE run=? AND request_id=?', (ident, body['requestId'])).fetchone()
            if old:
                if old['input_hash'] != digest or old['mode'] != mode:
                    raise AdapterFailure('Input request id has different content', 409, 'idempotency_conflict')
                return self.run(ident)
            if run['phase'] != 'active':
                raise AdapterFailure('Run is no longer active', 409, 'run_not_active')
            if self.db.execute('SELECT COUNT(*) FROM input_messages WHERE run=?', (ident,)).fetchone()[0] >= 50 or self.db.execute("SELECT COUNT(*) FROM input_messages WHERE run=? AND status='queued'", (ident,)).fetchone()[0] >= 20:
                raise AdapterFailure('Run input limit reached', 409, 'conflict')
            owned = self._conversation(run['conversation'])
            self._verify(owned)
            if owned['owner'] != 'bridge':
                raise AdapterFailure('Native session ownership changed', 409, 'ownership_changed')
            message = _id('msg_')
            with self.db:
                self.db.execute('INSERT INTO input_messages(run,request_id,mode,input_hash,status,message) VALUES(?,?,?,?,\'queued\',?)', (ident, body['requestId'], mode, digest, message))
        try:
            self.native.request('POST', '/api/session/' + owned['session'] + '/prompt',
                {'id': message, 'text': text, 'delivery': mode,
                 'metadata': {'workspaceBridgeRun': ident}, 'resume': True})
        except AdapterFailure:
            with self.lock, self.db:
                self.db.execute("UPDATE input_messages SET status='unknown' WHERE message=?", (message,))
        self._emit('run.updated', run['conversation'], ident)
        return self.run(ident)

    def steer(self, ident, body):
        return self._control(ident, body, 'steer')

    def queue(self, ident, body):
        return self._control(ident, body, 'queue')

    def _recover(self):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE phase IN ('starting','active')")]
        for run in rows:
            try:
                owned = self._conversation(run['conversation'])
                self._verify(owned)
                # Native durable logs can prove completion even if another native
                # service recovered it. No prompt or permission is replayed.
                self._refresh(run)
                if self._run(run['id'])['phase'] != 'terminal':
                    self._discard_inbox(owned, run)
                    self._finish(run['id'], 'interrupted', 'adapter_restarted')
            except AdapterFailure:
                self._finish(run['id'], 'orphaned', 'native_recovery_unconfirmed')

    def reclaim(self, ident):
        """Take a handed-off session back for Bridge runs at an idle boundary."""
        row = self._conversation(ident)
        if self._is_busy(ident):
            raise AdapterFailure('Conversation is busy', 409, 'conversation_busy')
        self._verify(row)
        self._activate(row['cwd'])
        with self.lock, self.db:
            self.db.execute("UPDATE conversations SET owner='bridge' WHERE id=?", (ident,))
        return self.conversation(ident)

    def handoff(self, ident):
        row = self._conversation(ident)
        self._verify(row)
        if self._is_busy(ident):
            raise AdapterFailure('Native handoff requires an idle session', 409, 'conversation_busy')
        with self.lock, self.db:
            self.db.execute("UPDATE conversations SET owner='human' WHERE id=?", (ident,))
        return {'nativeId': row['session'], 'directory': row['cwd']}

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self.lock:
            for entry in self.pending.values():
                if entry.get('local'):
                    entry['event'].set()
        if hasattr(self, 'native'):
            self.native.close()
        for thread in self._threads:
            thread.join(timeout=2)
        self.policy_server.shutdown()
        self.policy_server.server_close()
        self.db.close()
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
