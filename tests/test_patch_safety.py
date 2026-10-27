"""apply_patch safety at the file level: paths, policy, staging and races."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from workspace_bridge.api import DESTRUCTIVE_TOOLS, TOOLS, make_mcp
from workspace_bridge.embedded_skill import read_project_lead_skill
from mcp_helpers import McpTransport, LEGACY, MODERN, PREFIX
from workspace_bridge.security import BridgeError, HANDOFF, MAX_WRITE, digest
from workspace_bridge.service import Service

NOTE = 'notes/plan.md'


@pytest.fixture(autouse=True)
def _edit_scope(request):
    if "env" in request.fixturenames:
        env = request.getfixturevalue("env")
        env["service"].manage_workspace(env["id"], "set_write_scope", write_scope="workspace")


def call(env, tool, **args):
    return env['service'].call(env['id'], env['token'], tool, args)


def patch(env, *changes, ws=None):
    return env['service'].call(ws or env['id'], env['token'], 'apply_patch',
                               {'changes': list(changes), 'message': 'Update the plan note.'})


def write(env, content='first\n', path=NOTE, ws=None):
    """Create a file and return its new hash."""
    return patch(env, {'op': 'create', 'path': path, 'content': content}, ws=ws)['files'][0]['sha256']


def edit(env, sha, old='first', new='second', path=NOTE, ws=None):
    return patch(env, {'op': 'edit', 'path': path, 'sha256': sha, 'old': old, 'new': new},
                 ws=ws)['files'][0]['sha256']


def read(env, path=NOTE):
    return call(env, 'read_file', path=path, start_line=1, max_lines=200, expected_sha256=None)


def staged(env):
    return list(env['root'].rglob('.wb-write-*'))


def test_create_edit_read_no_other_changes(env):
    original = (env['root']/'src/main.py').read_bytes()
    created = write(env)
    assert read(env)['sha256'] == created == digest(b'first\n')
    edited = edit(env, created)
    assert (env['root']/NOTE).read_bytes() == b'second\n' and read(env)['sha256'] == edited
    assert (env['root']/'src/main.py').read_bytes() == original
    assert not staged(env)


@pytest.mark.parametrize('op', ['create', 'edit'])
@pytest.mark.parametrize('path', ['../outside.txt', '/tmp/outside.txt', 'notes/../../outside.txt',
    'notes//plan.md', 'notes/./plan.md', 'bad\\name.md', 'C:\\Projects\\note.md', 'bad\x00name.md'])
def test_denied_paths_leave_source_untouched(env, op, path):
    original = (env['root']/'src/main.py').read_bytes()
    with pytest.raises(BridgeError):
        if op == 'create':
            write(env, path=path)
        else:
            edit(env, '0'*64, path=path)
    assert (env['root']/'src/main.py').read_bytes() == original
    assert not (env['tmp']/'outside.txt').exists()


@pytest.mark.parametrize('path', ['.env', 'notes/secret.key', '.git/config', 'notes/.git/config',
                                  'data.db', 'notes/secrets.json'])
def test_secret_and_internal_paths_denied(env, path):
    with pytest.raises(BridgeError):
        write(env, path=path)
    assert not (env['root']/path).exists()


@pytest.mark.parametrize('rule', ['*.md', 'notes/**', 'notes', '*.MD'])
def test_admin_exclusions_apply_to_read_write_list_search(env, rule):
    sha = write(env)
    env['service'].manage_workspace(env['id'], 'set_excludes', [rule])
    with pytest.raises(BridgeError):
        edit(env, sha)
    with pytest.raises(BridgeError):
        read(env)
    with pytest.raises(BridgeError):
        write(env, path='notes/other.md')
    try:
        assert not call(env, 'glob', path='notes', pattern='**/*')['entries']
    except BridgeError:
        pass
    assert (env['root']/NOTE).read_bytes() == b'first\n'


@pytest.mark.parametrize('where', ['parent', 'file'])
def test_symlinks_cannot_redirect_create_or_edit(env, where):
    outside = env['tmp']/'outside'
    outside.mkdir()
    target = outside/'plan.md'
    target.write_text('keep\n')
    if where == 'parent':
        (env['root']/'notes').symlink_to(outside, target_is_directory=True)
    else:
        (env['root']/'notes').mkdir()
        (env['root']/NOTE).symlink_to(target)
    with pytest.raises(BridgeError):
        write(env, path='notes/new.md' if where == 'parent' else NOTE)
    with pytest.raises(BridgeError):
        edit(env, digest(b'keep\n'), old='keep')
    assert target.read_text() == 'keep\n'
    assert list(outside.iterdir()) == [target]


@pytest.mark.parametrize('kind', ['hardlink', 'fifo', 'directory'])
def test_nonregular_or_multilink_files_cannot_be_modified(env, kind):
    p = env['root']/NOTE
    p.parent.mkdir(parents=True)
    target = env['root']/'src/main.py'
    before = target.read_bytes()
    if kind == 'hardlink':
        os.link(target, p)
    elif kind == 'fifo':
        os.mkfifo(p)
    else:
        p.mkdir()
    with pytest.raises(BridgeError):
        edit(env, digest(before), old='return')
    assert target.read_bytes() == before


def test_stale_hash_does_not_overwrite_local_changes(env):
    first = write(env)
    (env['root']/NOTE).write_text('local first update\n')
    with pytest.raises(BridgeError) as exc:
        edit(env, first)
    assert exc.value.code == 'stale_evidence'
    assert (env['root']/NOTE).read_text() == 'local first update\n'


@pytest.mark.parametrize('content,old,error', [('same same', 'same', 'ambiguous_match'),
    ('aaa', 'aa', 'ambiguous_match'), ('first', 'absent', 'match_not_found'),
    ('first', '', 'invalid_arguments')])
def test_exact_edit_rejects_ambiguity_and_missing_text(env, content, old, error):
    first = write(env, content)
    with pytest.raises(BridgeError) as exc:
        edit(env, first, old, 'x')
    assert exc.value.code == error
    assert (env['root']/NOTE).read_text() == content


def test_exact_edit_preserves_crlf_final_newline_and_unicode(env):
    first = write(env, '標題\r\nhello\r\nlast')
    edit(env, first, 'hello', '再見')
    assert (env['root']/NOTE).read_bytes() == '標題\r\n再見\r\nlast'.encode()


@pytest.mark.parametrize('content,error', [('a\x00b', 'binary_file'), ('a\x1bb', 'binary_file'),
    ('\ud800', 'invalid_arguments'), ('sk-'+'A'*30, 'secret_content')])
def test_bad_new_content_rejected_before_creating_folders(env, content, error):
    with pytest.raises(BridgeError) as exc:
        write(env, content)
    assert exc.value.code == error
    assert not (env['root']/'notes').exists()


@pytest.mark.parametrize('raw', [b'\xff\xfe', b'a\x00b'])
def test_existing_binary_content_not_edited(env, raw):
    p = env['root']/NOTE
    p.parent.mkdir(parents=True)
    p.write_bytes(raw)
    with pytest.raises(BridgeError) as exc:
        edit(env, digest(raw), old='a')
    assert exc.value.code == 'binary_file'
    assert p.read_bytes() == raw


def test_explicit_general_browsing_no_default_source_scan_of_handoff(env):
    note = env['root']/HANDOFF/'notes/plan.md'
    note.parent.mkdir(parents=True)
    note.write_text('unique handoff phrase\n')
    path = HANDOFF + '/notes/plan.md'
    assert all(not e['path'].startswith(HANDOFF) for e in call(env, 'list_dir', depth=4)['entries'])
    assert not call(env, 'grep', pattern='unique handoff phrase')['matches']
    assert call(env, 'glob', path=HANDOFF, pattern='**/*.md')['entries'][0]['path'] == path
    assert call(env, 'grep', path=HANDOFF, pattern='unique handoff phrase')['matches'][0]['path'] == path


def test_published_handoff_lives_in_bridge_state_not_the_workspace(env, payload):
    env['service'].manage_workspace(env['id'], 'set_write_scope', write_scope='none')
    job = call(env, 'save_handoff', **payload)
    assert job['stored'] == 'bridge' and 'path' not in job
    assert not (env['root']/HANDOFF).exists()
    task = call(env, 'list_handoffs', job_id=job['id'], document='TASK.md', start_line=1, max_lines=200)
    assert task['matches_published'] and 'Improve arithmetic behavior' in task['content']
    assert task['sha256'] == job['documents']['TASK.md']['sha256']
    prompt = call(env, 'list_handoffs', job_id=job['id'], document='PROMPT.md', start_line=1, max_lines=200)
    assert '===== ACCEPTANCE.md =====' in prompt['content'] and prompt['content'] in job['copy_prompt']
    assert call(env, 'list_handoffs', offset=0, limit=20)['handoffs'][0]['state'] == 'prepared'


def test_two_workspaces_same_relative_path_isolated(env):
    other = env['parent']/'beta'
    other.mkdir()
    svc = env['service']
    ws = svc.add_workspace('Beta', str(other), [])['workspace']['id']
    svc.manage_workspace(ws, 'enable')
    svc.manage_workspace(ws, 'set_write_scope', write_scope='workspace')
    one = write(env, 'alpha first\n')
    two = write(env, 'beta first\n', ws=ws)
    assert (env['root']/NOTE).read_text() == 'alpha first\n' and (other/NOTE).read_text() == 'beta first\n'
    with pytest.raises(BridgeError):
        edit(env, one, ws=ws)
    assert two != one and (other/NOTE).read_text() == 'beta first\n'


@pytest.mark.parametrize('action', ['workspace_disable', 'bridge_disable', 'rotate', 'wrong_token'])
def test_auth_revocation_prevents_write(env, action):
    svc = env['service']
    if action == 'workspace_disable':
        svc.manage_workspace(env['id'], 'disable')
    elif action == 'bridge_disable':
        svc.manage_bridge('disable')
    elif action == 'rotate':
        svc.manage_bridge('rotate_token')
    else:
        env['token'] = 'invalid'
    with pytest.raises(BridgeError):
        write(env)
    assert not (env['root']/'notes').exists()


def test_competing_edits_only_one_stale_base_wins(env):
    first = write(env)

    def attempt(value):
        try:
            return edit(env, first, 'first', value)
        except BridgeError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['one', 'two']))
    assert sum(r == 'stale_evidence' for r in results) == 1
    assert (env['root']/NOTE).read_text() in {'one\n', 'two\n'}


def test_failed_switch_keeps_old_file_and_removes_temp(env, monkeypatch):
    first = write(env)

    def failed(*args, **kw):
        raise OSError('disk error')
    monkeypatch.setattr(os, 'replace', failed)
    with pytest.raises(BridgeError):
        edit(env, first)
    assert (env['root']/NOTE).read_bytes() == b'first\n'
    assert not staged(env)


def test_local_change_during_staging_rejected(env, monkeypatch):
    first = write(env)
    original, once = os.fsync, False

    def fsync(fd):
        nonlocal once
        if not once:
            once = True
            (env['root']/NOTE).write_bytes(b'local edit\n')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(BridgeError) as exc:
        edit(env, first)
    assert exc.value.code == 'stale_evidence'
    assert (env['root']/NOTE).read_bytes() == b'local edit\n'
    assert not staged(env)


def test_create_race_does_not_clobber_existing_file(env, monkeypatch):
    link = os.link

    def race(src, dst, **kwargs):
        (env['root']/NOTE).write_text('other writer\n')
        return link(src, dst, **kwargs)
    monkeypatch.setattr(os, 'link', race)
    with pytest.raises(BridgeError) as exc:
        write(env)
    assert exc.value.code == 'conflict'
    assert (env['root']/NOTE).read_text() == 'other writer\n'
    assert not staged(env)


def test_parent_replaced_during_write_is_detected(env, monkeypatch):
    first = write(env)
    original, once = os.fsync, False

    def fsync(fd):
        nonlocal once
        if not once:
            once = True
            parent = (env['root']/NOTE).parent
            parent.rename(parent.with_name('moved-notes'))
            parent.mkdir()
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(BridgeError) as exc:
        edit(env, first)
    assert exc.value.code == 'stale_evidence'
    assert not (env['root']/NOTE).exists()
    assert (env['root']/'moved-notes/plan.md').read_bytes() == b'first\n'
    assert not list((env['root']/'notes').glob('.wb-write-*'))


def test_replaced_root_at_the_same_path_stays_usable(env):
    # No historical root-identity pin: a fresh directory at the configured
    # path takes the patch.
    root = env['root']
    root.rename(root.with_name('moved'))
    root.mkdir()
    write(env)
    assert (root/NOTE).read_bytes() == b'first\n'


def test_logs_do_not_contain_document_content(env):
    write(env, 'my-planning-sentinel')
    text = json.dumps([dict(row) for row in env['service'].db.execute('SELECT * FROM events')])
    assert 'apply_patch' in text and 'my-planning-sentinel' not in text


def test_skill_and_tools_describe_patches_and_bridge_handoffs():
    content = read_project_lead_skill()['content']
    for value in ['apply_patch', 'sha256', 'Bridge state', 're-read', 'run_checks', 'Audit using normal tools']:
        assert value in content
    assert 'write_file' not in TOOLS and 'edit_file' not in TOOLS
    schema, _, readonly, idempotent = TOOLS['apply_patch']
    assert not readonly and not idempotent
    assert set(schema.model_json_schema()['required']) == {'workspace_id', 'changes', 'message'}


@pytest.mark.parametrize('version', [LEGACY[-1], MODERN])
async def test_http_patch_read_and_destructive_annotations(env, version):
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(env['service'])),
                                 base_url='http://127.0.0.1:8765') as client:
        async def rpc(method, name=None, args=None):
            params = {} if name is None else dict(name=name, arguments={'workspace_id': env['id'], **(args or {})})
            headers = {'X-Bridge-Token': env['token'], 'Accept': 'application/json', 'MCP-Protocol-Version': version}
            if version == MODERN:
                params['_meta'] = {PREFIX+'protocolVersion': version, PREFIX+'clientCapabilities': {}}
                headers['Mcp-Method'] = method
                if name:
                    headers['Mcp-Name'] = name
            response = await client.post('/mcp', headers=headers,
                                         json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            body = response.json()
            # The modern transport answers a JSON-RPC error with HTTP 400.
            assert response.status_code == (400 if 'error' in body and version == MODERN else 200), response.text
            return body
        tools = (await rpc('tools/list'))['result']['tools']
        for t in tools:
            assert t['annotations']['destructiveHint'] == (t['name'] in DESTRUCTIVE_TOOLS)

        def value(r):
            assert not r['result']['isError'], r
            return json.loads(r['result']['content'][0]['text'])
        message = 'Write the plan note.'
        new = value(await rpc('tools/call', 'apply_patch', dict(message=message, changes=[
            {'op': 'create', 'path': NOTE, 'content': 'hello\n'}])))['files'][0]['sha256']
        edited = value(await rpc('tools/call', 'apply_patch', dict(message=message, changes=[
            {'op': 'edit', 'path': NOTE, 'sha256': new, 'old': 'hello', 'new': 'world'}])))['files'][0]['sha256']
        current = value(await rpc('tools/call', 'read_file', dict(path=NOTE)))
        assert current['sha256'] == edited and current['lines'][0]['text'] == 'world'
        bad = await rpc('tools/call', 'apply_patch', dict(message=message, changes=[
            {'op': 'create', 'path': 'src/main.py', 'content': 'bad'}]))
        assert bad['result']['isError']
        bad = await rpc('tools/call', 'apply_patch', dict(message=message, changes=[
            {'op': 'edit', 'path': NOTE, 'old': 'world', 'new': 'bad'}]))
        assert bad['error']['code'] == -32602


@pytest.mark.parametrize('kind', ['secret', 'excluded'])
def test_save_handoff_uses_same_policy_before_publication(env, payload, kind):
    if kind == 'secret':
        payload['context'] = 'sk-'+'A'*30
    else:
        env['service'].manage_workspace(env['id'], 'set_excludes', ['*.md'])
        payload['context_hashes'] = {'README.md': digest((env['root']/'README.md').read_bytes())}
    with pytest.raises(BridgeError):
        call(env, 'save_handoff', **payload)
    assert not (env['root']/HANDOFF).exists()
    assert env['service'].db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


async def test_http_large_bounded_patch_and_notification_denial(env):
    headers = {'X-Bridge-Token': env['token'], 'Accept': 'application/json'}
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(env['service'])),
                                 base_url='http://127.0.0.1:8765', headers=headers) as client:
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'apply_patch', 'arguments': {
            'workspace_id': env['id'], 'message': 'Write a large note.',
            'changes': [{'op': 'create', 'path': NOTE, 'content': 'x'*MAX_WRITE}]}}}
        r = await client.post('/mcp', json=msg)
        assert r.status_code == 200 and not r.json()['result']['isError'], r.text
        assert (env['root']/NOTE).stat().st_size == MAX_WRITE
        msg['params']['arguments']['changes'][0]['path'] = 'notes/not-created.md'
        msg.pop('id')
        r = await client.post('/mcp', json=msg)
        assert r.status_code == 202 and not (env['root']/'notes/not-created.md').exists()
        msg['id'] = 2
        msg['padding'] = 'z' * (40 * 1024 * 1024)
        r = await client.post('/mcp', json=msg)
        assert r.status_code in {400, 413} and not (env['root']/'notes/not-created.md').exists()


def test_existing_state_opens_without_migration_and_files_survive(env, payload):
    job = call(env, 'save_handoff', **payload)
    created = write(env)
    columns = [tuple(r) for r in env['service'].db.execute('PRAGMA table_info(jobs)')]
    reopened = Service(env['state'], env['config'])
    reopened._node_transport = {env['node_id']: env['node']['transport']}
    reopened._node_transport_services = {env['node_id']: env['node']['service']}
    try:
        reopened.authenticate(env['id'], env['token'])
        assert columns == [tuple(r) for r in reopened.db.execute('PRAGMA table_info(jobs)')]
        assert reopened.job(reopened.workspace(env['id']), job['id'])['state'] == 'prepared'
        value = reopened.call(env['id'], env['token'], 'read_file',
                              dict(path=NOTE, start_line=1, max_lines=20, expected_sha256=created))
        assert value['lines'][0]['text'] == 'first'
    finally:
        reopened.close()
