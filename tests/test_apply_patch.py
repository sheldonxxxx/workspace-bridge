"""apply_patch, the Node edit journal, undo_edit, the diff-based secret check and deny overrides."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import stat

import httpx
import pytest

from admin_helpers import admin_cookie
from workspace_bridge import edit_journal
from workspace_bridge.api import TOOLS, make_admin, make_mcp
from workspace_bridge.security import BridgeError, allowed, digest, validate_excludes

SECRET = 'api_key = "sk-proj-abcdefghijklmnopqrstuvwx"'


def call(e, name, **args):
    return e['service'].call(e['id'], e['token'], name, args)


def sha(e, path):
    return digest((e['root'] / path).read_bytes())


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) if p.is_file() else None
            for p in sorted(root.rglob('*'))}


@pytest.fixture
def ws(env):
    env['service'].manage_workspace(env['id'], 'set_write_scope', write_scope='workspace')
    (env['root'] / 'old.md').write_text('old\n')
    (env['root'] / 'tmp.json').write_text('{}\n')
    os.chmod(env['root'] / 'old.md', 0o640)
    return env


def five_files(e):
    main = sha(e, 'src/main.py')
    return [
        {'op': 'edit', 'path': 'src/main.py', 'sha256': main, 'old': 'a + b', 'new': 'a + b  # sum'},
        {'op': 'edit', 'path': 'src/main.py', 'sha256': main, 'old': 'def add', 'new': 'def plus'},
        {'op': 'create', 'path': 'src/util/retry.py', 'content': 'RETRIES = 3\n'},
        {'op': 'move', 'path': 'old.md', 'to': 'docs/old.md', 'sha256': sha(e, 'old.md')},
        {'op': 'delete', 'path': 'tmp.json', 'sha256': sha(e, 'tmp.json')},
        {'op': 'edit', 'path': 'README.md', 'sha256': sha(e, 'README.md'), 'old': '# Alpha', 'new': '# Alpha project'},
    ]


def journal_rows(e):
    return e['service'].list_edits(e['id'])['edits']


def test_five_file_patch_applies_atomically_and_undoes_cleanly(ws):
    before = tree(ws['root'])
    result = call(ws, 'apply_patch', changes=five_files(ws), message='Add retry helper and tidy docs')
    root = ws['root']
    assert (root / 'src/main.py').read_text() == 'def plus(a, b):\n    return a + b  # sum\n'
    assert (root / 'src/util/retry.py').read_text() == 'RETRIES = 3\n'
    assert (root / 'docs/old.md').read_text() == 'old\n' and not (root / 'old.md').exists()
    assert stat.S_IMODE((root / 'docs/old.md').stat().st_mode) == 0o640
    assert not (root / 'tmp.json').exists()
    assert {f['path']: f['sha256'] for f in result['files']} == {
        'src/main.py': sha(ws, 'src/main.py'), 'src/util/retry.py': sha(ws, 'src/util/retry.py'),
        'old.md': None, 'docs/old.md': sha(ws, 'docs/old.md'), 'tmp.json': None,
        'README.md': sha(ws, 'README.md')}
    assert result['created_folders'] == ['src/util', 'docs']
    [entry] = journal_rows(ws)
    assert entry['edit_id'] == result['edit_id'] and entry['actor'] == 'chatgpt'
    assert entry['message'] == 'Add retry helper and tidy docs' and entry['state'] == 'applied'
    assert not any('.wb-write-' in name for name in tree(root))

    undone = call(ws, 'undo_edit', edit_id=result['edit_id'])
    assert tree(root) == before
    assert undone['undid'] == result['edit_id']
    with pytest.raises(BridgeError) as exc:
        call(ws, 'undo_edit', edit_id=result['edit_id'])
    assert exc.value.code == 'already_undone'
    # An undo is itself journaled, so it can be undone again.
    call(ws, 'undo_edit', edit_id=undone['edit_id'])
    assert (root / 'src/util/retry.py').read_text() == 'RETRIES = 3\n'
    assert [e['kind'] for e in journal_rows(ws)] == ['undo', 'undo', 'patch']


def test_a_failed_check_changes_nothing(ws):
    before = tree(ws['root'])
    changes = five_files(ws)
    changes[-1]['sha256'] = '0' * 64
    with pytest.raises(BridgeError) as exc:
        call(ws, 'apply_patch', changes=changes, message='stale')
    assert exc.value.code == 'stale_evidence'
    assert tree(ws['root']) == before and journal_rows(ws) == []


def test_a_failed_switch_puts_back_what_was_already_switched(ws, monkeypatch):
    before = tree(ws['root'])
    real, seen = edit_journal._switch, []

    def failing(safe, item, staged):
        seen.append(item.path)
        if len(seen) == 4:
            raise BridgeError('disk full', 'write_failed')
        return real(safe, item, staged)
    monkeypatch.setattr(edit_journal, '_switch', failing)
    with pytest.raises(BridgeError) as exc:
        call(ws, 'apply_patch', changes=five_files(ws), message='fails midway')
    assert exc.value.code == 'write_failed'
    assert tree(ws['root']) == before and journal_rows(ws) == []


def test_undo_reports_files_changed_since_and_changes_nothing(ws):
    result = call(ws, 'apply_patch', changes=five_files(ws), message='five')
    (ws['root'] / 'README.md').write_text('edited by hand\n')
    (ws['root'] / 'tmp.json').write_text('recreated\n')
    after = tree(ws['root'])
    with pytest.raises(BridgeError) as exc:
        call(ws, 'undo_edit', edit_id=result['edit_id'])
    assert exc.value.code == 'undo_conflict'
    assert 'tmp.json' in str(exc.value) and 'README.md' in str(exc.value)
    assert 'src/main.py' not in str(exc.value)
    assert tree(ws['root']) == after
    with pytest.raises(BridgeError) as exc:
        call(ws, 'undo_edit', edit_id='edit_' + '0' * 24)
    assert exc.value.code == 'not_found'


@pytest.mark.parametrize('changes,code', [
    ([{'op': 'create', 'path': 'README.md', 'content': 'x'}], 'conflict'),
    ([{'op': 'move', 'path': 'old.md', 'to': 'README.md', 'sha256': None}], 'conflict'),
    ([{'op': 'create', 'path': '.env', 'content': 'x'}], 'policy_denied'),
    ([{'op': 'create', 'path': '.git/config', 'content': 'x'}], 'policy_denied'),
    ([{'op': 'create', 'path': 'a.md', 'content': 'x'}, {'op': 'create', 'path': 'A.md', 'content': 'y'}],
     'invalid_arguments'),
    ([{'op': 'create', 'path': 'a.md', 'content': 'x'}, {'op': 'delete', 'path': 'a.md', 'sha256': '0' * 64}],
     'invalid_arguments'),
    ([{'op': 'edit', 'path': 'README.md', 'sha256': None, 'old': 'nothing like this', 'new': 'x'}],
     'match_not_found'),
    ([{'op': 'edit', 'path': 'src/main.py', 'sha256': None, 'old': 'a', 'new': 'x'}], 'ambiguous_match'),
    ([{'op': 'create', 'path': f'f{i}.md', 'content': 'x'} for i in range(51)], 'too_large'),
    ([{'op': 'create', 'path': 'a.md', 'content': 'x' * 600_000},
      {'op': 'create', 'path': 'b.md', 'content': 'x' * 600_000}], 'too_large'),
    ([{'op': 'create', 'path': 'big.md', 'content': 'x' * 300_000}], 'too_large'),
    ([{'op': 'create', 'path': 'bin.md', 'content': 'a\x00b'}], 'binary_file'),
    ([{'op': 'chmod', 'path': 'README.md'}], 'invalid_arguments'),
])
def test_patch_checks(ws, changes, code):
    for change in changes:
        if change.get('sha256', '') is None:
            change['sha256'] = sha(ws, change['path'])
    before = tree(ws['root'])
    with pytest.raises(BridgeError) as exc:
        call(ws, 'apply_patch', changes=changes, message='checked')
    assert exc.value.code == code
    assert tree(ws['root']) == before and journal_rows(ws) == []


def test_write_scope_governs_patches_and_undo(ws):
    service = ws['service']
    service.manage_workspace(ws['id'], 'set_write_scope', write_scope='handoff')
    with pytest.raises(BridgeError) as exc:
        call(ws, 'apply_patch', changes=five_files(ws), message='denied')
    assert exc.value.code == 'policy_denied'
    service.manage_workspace(ws['id'], 'set_write_scope', write_scope='workspace')
    result = call(ws, 'apply_patch', changes=five_files(ws), message='allowed')
    service.manage_workspace(ws['id'], 'set_write_scope', write_scope='none')
    with pytest.raises(BridgeError) as exc:
        call(ws, 'undo_edit', edit_id=result['edit_id'])
    assert exc.value.code == 'policy_denied'


def test_secret_check_blocks_only_new_matches(ws):
    root = ws['root']
    (root / 'settings.py').write_text(f'{SECRET}\nDEBUG = False\n')
    # A file that already holds a match stays editable, including the line beside it.
    call(ws, 'apply_patch', message='turn on debug', changes=[
        {'op': 'edit', 'path': 'settings.py', 'sha256': sha(ws, 'settings.py'),
         'old': 'DEBUG = False', 'new': 'DEBUG = True'}])
    edited = call(ws, 'edit_file', path='settings.py', old_text='DEBUG = True', new_text='DEBUG = 1',
                  expected_sha256=sha(ws, 'settings.py'))
    call(ws, 'write_file', path='settings.py', content=f'# keys\n{SECRET}\nDEBUG = 2\n',
         expected_sha256=edited['sha256'])
    for name, args in [
        ('apply_patch', dict(message='copy key', changes=[
            {'op': 'edit', 'path': 'settings.py', 'sha256': sha(ws, 'settings.py'),
             'old': 'DEBUG = 2', 'new': SECRET}])),
        ('apply_patch', dict(message='new key', changes=[
            {'op': 'create', 'path': 'keys.py', 'content': 'token = "ghp_abcdefghijklmnopqrstuvwxyz"\n'}])),
        ('edit_file', dict(path='settings.py', old_text='DEBUG = 2', new_text='password = "hunter2hunter2"',
                           expected_sha256=sha(ws, 'settings.py'))),
        ('write_file', dict(path='notes.md', content=SECRET)),
    ]:
        with pytest.raises(BridgeError) as exc:
            call(ws, name, **args)
        assert exc.value.code == 'secret_content'
    assert not (root / 'keys.py').exists() and not (root / 'notes.md').exists()
    # Moving or deleting a file that holds a match is fine: nothing new is added.
    call(ws, 'apply_patch', message='move settings', changes=[
        {'op': 'move', 'path': 'settings.py', 'to': 'config/settings.py', 'sha256': sha(ws, 'settings.py')}])


def test_deny_overrides_show_only_default_hidden_names(ws):
    service, root = ws['service'], ws['root']
    (root / 'build').mkdir()
    (root / 'build' / 'out.js').write_text('built\n')
    (root / '.env').write_text('A=1\n')
    with pytest.raises(BridgeError):
        call(ws, 'read_file', path='build/out.js', start_line=1, max_lines=10, expected_sha256=None)
    service.manage_workspace(ws['id'], 'set_excludes', excludes=['!build', 'docs/private/*'])
    assert call(ws, 'read_file', path='build/out.js', start_line=1, max_lines=10,
                expected_sha256=None)['lines'][0]['text'] == 'built'
    listed = call(ws, 'list_dir', path='', depth=1)
    assert 'build' in {entry['path'] for entry in listed['entries']}
    info = call(ws, 'workspace_info')
    assert info['shown_default_hidden'] == ['build'] and info['extra_exclusions'] == ['docs/private/*']
    for value in ['!.env', '!.git', '!*.pem', '!src', '!secrets']:
        with pytest.raises(BridgeError) as exc:
            service.manage_workspace(ws['id'], 'set_excludes', excludes=[value])
        assert exc.value.code == 'invalid_arguments'
    assert allowed('vendor/x.go', ['!vendor']) and not allowed('vendor/.env', ['!vendor'])
    assert not allowed('build/x', ['!build', 'build']) and not allowed('dist/x', ['!build'])
    assert validate_excludes(['!*.log', '!Node_Modules']) == ['!*.log', '!Node_Modules']
    with pytest.raises(BridgeError):
        call(ws, 'read_file', path='.env', start_line=1, max_lines=10, expected_sha256=None)


def test_journal_retention_and_interrupted_entries(ws, monkeypatch):
    first = call(ws, 'apply_patch', message='first', changes=[
        {'op': 'create', 'path': 'a.md', 'content': 'a' * 1000}])
    node_state = Path(ws['tmp'] / 'node-state')
    db = sqlite3.connect(node_state / 'edit-journal.sqlite3')
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    db.execute('UPDATE edits SET created=? WHERE id=?', (old, first['edit_id']))
    db.commit()
    second = call(ws, 'apply_patch', message='second', changes=[
        {'op': 'delete', 'path': 'a.md', 'sha256': sha(ws, 'a.md')}])
    assert [e['edit_id'] for e in journal_rows(ws)] == [second['edit_id']]
    monkeypatch.setattr(edit_journal, 'RETENTION_BYTES', 100)
    third = call(ws, 'apply_patch', message='third', changes=[
        {'op': 'delete', 'path': 'README.md', 'sha256': sha(ws, 'README.md')}])
    fourth = call(ws, 'apply_patch', message='fourth', changes=[
        {'op': 'delete', 'path': 'src/main.py', 'sha256': sha(ws, 'src/main.py')}])
    assert [e['edit_id'] for e in journal_rows(ws)] == [fourth['edit_id'], third['edit_id']]

    db.execute("UPDATE edits SET state='applying' WHERE id=?", (fourth['edit_id'],))
    db.commit()
    db.close()
    journal = edit_journal.EditJournal(node_state / 'edit-journal.sqlite3')
    try:
        row, _ = journal.entry(ws['id'], fourth['edit_id'])
        assert row['state'] == 'interrupted'
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_mcp_schema_large_patch_and_manager_journal(ws):
    assert {'apply_patch', 'undo_edit'} <= TOOLS.keys()
    schema = TOOLS['apply_patch'][0].model_json_schema()
    assert schema['additionalProperties'] is False and {'workspace_id', 'changes', 'message'} <= set(schema['required'])
    content = 'line\n' * 160_000  # Over the ordinary 1 MiB MCP body once two files carry it.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(ws['service'])),
                                 base_url='http://127.0.0.1:8765') as client:
        headers = {'X-Bridge-Token': ws['token'], 'Accept': 'application/json'}
        listed = (await client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})).json()['result']['tools']
        tool = next(t for t in listed if t['name'] == 'apply_patch')
        assert tool['annotations']['readOnlyHint'] is False and tool['annotations']['destructiveHint'] is True
        response = await client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'apply_patch', 'arguments': {
                'workspace_id': ws['id'], 'message': 'two large files', 'changes': [
                    {'op': 'create', 'path': 'big1.txt', 'content': content[:200_000]},
                    {'op': 'create', 'path': 'big2.txt', 'content': content[:200_000]},
                    {'op': 'create', 'path': 'big3.txt', 'content': content[:200_000]},
                    {'op': 'create', 'path': 'big4.txt', 'content': content[:200_000]},
                    {'op': 'create', 'path': 'big5.txt', 'content': content[:200_000]}]}}})
        result = response.json()['result']
        assert not result.get('isError'), result
        assert (ws['root'] / 'big5.txt').stat().st_size == 200_000
    app = make_admin(ws['service'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8766') as client:
        assert (await client.get(f"/api/workspaces/{ws['id']}/edits")).status_code == 401
        page = (await client.get(f"/api/workspaces/{ws['id']}/edits",
                                 headers={'Cookie': admin_cookie(app)})).json()
    [entry] = page['edits']
    assert entry['message'] == 'two large files' and len(entry['files']) == 5
    assert 'before' not in str(entry) and page['retention']['days'] == 30
