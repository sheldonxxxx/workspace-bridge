"""Node-owned image decoding at exact adapter admission, no Bridge file access."""
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest

from conftest import patch_node_runtime
from PIL import Image
from workspace_bridge.node_service import NodeService
from workspace_bridge.run_images import validate_images
from workspace_bridge.security import BridgeError, digest
from workspace_bridge.api import AgentStartRun


def test_node_decodes_and_audits_run_images(tmp_path,monkeypatch):
    root=tmp_path/'work';root.mkdir()
    with BytesIO() as buf:
        Image.new('RGB',(32,24),'blue').save(buf,'PNG');data=buf.getvalue()
    (root/'reference.png').write_bytes(data)
    node=NodeService(tmp_path/'node',{'allowed_roots':[str(tmp_path)],"cursor_secret": "c" * 64})
    ws={'id':'ws_'+'1'*24,'root':str(root),'excludes':[],'write_scope':'none'}
    node.register_workspace(ws)
    adapter=node.save_adapter({'name':'OpenCode','plugin': 'opencode'})
    submissions=[]
    class Client:
        def __init__(self,*a,**kw):pass
        def descriptor(self):return SimpleNamespace(supports=lambda f: f=='imageInput')
        def models(self,*a):return [{'selector':'fixture/fake','default':True,'inputModalities':['text','image']}]
        def start_run(self,ident,payload):
            submissions.append(payload)
            return {'id':'run','phase':'active','activeState':'running','outcome':None}
    patch_node_runtime(monkeypatch, Client)
    def invoke(images):
        return node.runtime_call(adapter['id'],'start_run',{'expected_adapter_revision':adapter['revision'],
            'workspace':ws,'arguments':{'conversation_id':'conv','payload':{'clientRunId':'run',
            'input':[{'type':'text','text':'Inspect image'}],'images':images}}})
    try:
        result=invoke([{'path':'reference.png','expected_sha256':digest(data)}])
        image=submissions[0]['input'][1]
        assert image['type']=='image' and image['mimeType']=='image/png'
        assert base64.b64decode(image['data'])
        assert result['inputImages'][0]['sha256']==digest(data)
        assert 'data' not in result['inputImages'][0]
        with pytest.raises(BridgeError):invoke([{'path':'reference.png','expected_sha256':'0'*64}])
        with pytest.raises(BridgeError):invoke([{'path':'../outside.png'}])
        node.configure_workspace(ws['id'],['*.png'],'none')
        with pytest.raises(BridgeError):invoke([{'path':'reference.png'}])
        assert len(submissions)==1
    finally:node.close()


def test_run_images_schema_is_bounded_and_references_only():
    body=AgentStartRun.model_validate({'adapter_id':'adapter_'+'1'*24,'request_id':'test','instruction':'Inspect',
        'images':[{'path':'reference.png'}]}).model_dump()
    assert validate_images(body['images'])==[{'path':'reference.png'}]
    for invalid in ([{'path':'a'}]*5, [{'path':'a','data':'arbitrary'}], [{'path':'a','expected_sha256':'bad'}]):
        with pytest.raises(BridgeError):validate_images(invalid)
