import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.asyncio.server import serve

from excel_codex_bridge import sse, upstream_routes
from excel_codex_bridge.research import siwc, native_cli, tasks
from excel_codex_bridge.research.credentials import resolve, Snapshot, Unavailable
from excel_codex_bridge.research.transport import collect
from excel_codex_bridge.ws_upstream import FixedEndpointConnect, open_response, WebSocketBridge

MODEL="gpt-5.6-sol"


@pytest.fixture
def signed():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="fixture",use="sig",alg="RS256")
    claims={"sub":"test-subject","aud":"oaiapp_fixture","iss":siwc.ISSUER,"iat":int(time.time()),"exp":int(time.time())+300,"nonce":"nonce"}
    def tokens(**changes):
        value=jwt.encode({**claims,**changes},key,algorithm="RS256",headers={"kid":"fixture"})
        return {"id_token":value,"access_token":"access-not-logged","refresh_token":"refresh-not-logged","token_type":"Bearer","expires_in":300,"scope":siwc.SCOPES}
    return tokens,{"keys":[jwk]}


def test_siwc_signature_nonce_audience_expiry_and_account_binding(signed):
    tokens,keys=signed
    record=siwc.validate_token(tokens(),"oaiapp_fixture","nonce",keys)
    assert siwc.DIRECT_SCOPE in record["scopes"]
    for changes in ({"nonce":"wrong"},{"aud":"oaiapp_other"},{"aud":["oaiapp_fixture","other"]},{"iss":"https://other.invalid"},{"exp":1}):
        with pytest.raises(ValueError,match="validation failed"):
            siwc.validate_token(tokens(**changes),"oaiapp_fixture","nonce",keys)
    with pytest.raises(ValueError):
        siwc.validate_token(tokens(sub="changed"),"oaiapp_fixture","nonce",keys,record)
    with pytest.raises(ValueError):
        siwc.validate_token(tokens(),"oaiapp_fixture","nonce",{"keys":[]})


def test_siwc_login_full_pkce_exchange_and_returning_registration(tmp_path,signed,capsys):
    import hashlib
    import threading
    from urllib.parse import parse_qs,urlsplit,urlencode
    tokens,keys=signed
    pending={}
    browser_threads=[]
    auth=tmp_path/"private"/"grant.json"
    urls=[]
    def opener(url):
        urls.append(url)
        pending.clear();pending.update({k:v[0] for k,v in parse_qs(urlsplit(url).query).items()})
        target=pending["redirect_uri"]+"?"+urlencode({"state":pending["state"],"code":"one-time-code","client_id":"oaiapp_fixture"})
        def browser():
            with httpx.Client(trust_env=False,timeout=10) as client:
                assert client.get(target).status_code==200
        thread=threading.Thread(target=browser);browser_threads.append(thread);thread.start()
        return True
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            form={k:v[0] for k,v in parse_qs(request.content.decode()).items()}
            assert form["client_id"]=="oaiapp_fixture" and form["redirect_uri"]==pending["redirect_uri"]
            challenge=base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            assert challenge==pending["code_challenge"] and form["resource"]==siwc.RESOURCE
            return httpx.Response(200,json=tokens(nonce=pending["nonce"]))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200,json={"issuer":siwc.ISSUER,"jwks_uri":siwc.ISSUER+"/.well-known/jwks.json"})
        return httpx.Response(200,json=keys)
    factory=lambda **kwargs:httpx.Client(transport=httpx.MockTransport(handler),**kwargs)
    first=siwc.login(auth,opener=opener,client_factory=factory,timeout=10)
    second=siwc.login(auth,opener=opener,client_factory=factory,timeout=10)
    for thread in browser_threads: thread.join(timeout=10)
    assert first["direct_inference_enabled"] and second["signed_in"]
    a,b=(parse_qs(urlsplit(url).query) for url in urls)
    assert a["client_id"]==["dynamic_agent_client"] and b["client_id"]==["oaiapp_fixture"]
    assert a["state"]!=b["state"] and a["nonce"]!=b["nonce"] and a["ext_agent_host_id"]==b["ext_agent_host_id"]
    assert "id_token_hint" in b and "agent_name_hint" not in b
    assert len(requests)==6
    printed=capsys.readouterr().out
    assert "access-not-logged" not in printed and b["id_token_hint"][0] not in printed


def test_siwc_callback_rejects_state_client_changes_and_duplicate_parameters():
    good={"state":["state"],"code":["code"],"client_id":["oaiapp_fixture"]}
    assert siwc.callback(good,"state")==("code","oaiapp_fixture")
    for value in ({**good,"state":["wrong"]},{**good,"state":["state","state"]},{**good,"client_id":["dynamic_agent_client"]},{**good,"error":["access_denied"]}):
        with pytest.raises(ValueError):
            siwc.callback(value,"state")
    with pytest.raises(ValueError):
        siwc.callback(good,"state",{"client_id":"oaiapp_other"})


def test_missing_direct_scope_is_not_inference_permission(tmp_path,signed):
    tokens,keys=signed
    record=siwc.validate_token({**tokens(),"scope":"openid profile email"},"oaiapp_fixture","nonce",keys)
    path=tmp_path/"siwc.json"
    siwc.save_record(path,record)
    assert siwc.read_record(path)["subject"]==record["subject"]
    with pytest.raises(Unavailable,match="permission_missing"):
        resolve({"id":"siwc","kind":"siwc","auth_file":str(path)})
    if os.name!="nt":
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
        with pytest.raises(ValueError,match="owner-only"):
            siwc.read_record(path)


def test_websocket_sends_one_create_and_preserves_native_tool_output():
    async def check():
        received=[]
        async def handler(socket):
            received.append(json.loads(await socket.recv()))
            output=[{"type":"reasoning","encrypted_content":"opaque"},
                    {"type":"function_call","name":"fixture_read","arguments":"{}","call_id":"call"}]
            await socket.send(json.dumps({"type":"response.completed","response":{"status":"completed","model":MODEL,"reasoning":{"effort":"low"},"output":output}}))
        async with serve(handler,"127.0.0.1",0) as server:
            port=server.sockets[0].getsockname()[1]
            def connector(url,**kwargs):
                assert url=="wss://chatgpt.com/backend-api/codex/responses"
                assert kwargs["additional_headers"]["Authorization"]=="Bearer synthetic"
                kwargs["proxy"]=None
                return FixedEndpointConnect(f"ws://127.0.0.1:{port}",**kwargs)
            payload=tasks.payload(tasks.generate("smoke",1)[1],MODEL,"low")
            response=await open_response(payload,{"Authorization":"Bearer synthetic"},connector=connector)
            try:
                result=await collect(response,payload,"websocket")
            finally:
                await response.aclose()
            assert result.status=="completed" and result.output[0]["encrypted_content"]=="opaque"
            assert len(received)==1 and received[0]["type"]=="response.create"
            assert "stream" not in received[0]
            assert received[0]["tools"]==payload["tools"]
    asyncio.run(check())


def test_websocket_redirect_does_not_forward_credentials():
    async def check():
        forwarded=[]
        async def destination(socket):
            forwarded.append(True)
        async with serve(destination,"127.0.0.1",0) as target:
            target_port=target.sockets[0].getsockname()[1]
            def redirect(connection,request):
                response=connection.respond(302,"Redirect")
                response.headers["Location"]=f"ws://127.0.0.1:{target_port}/"
                return response
            async with serve(destination,"127.0.0.1",0,process_request=redirect) as source:
                port=source.sockets[0].getsockname()[1]
                def connector(url,**kwargs):
                    kwargs["proxy"]=None
                    return FixedEndpointConnect(f"ws://127.0.0.1:{port}",**kwargs)
                response=await open_response({"model":MODEL,"input":[]},{"Authorization":"Bearer synthetic"},connector=connector)
                assert response.status_code==502
                assert not forwarded
    asyncio.run(check())


def test_native_websocket_is_an_explicit_bridge_route():
    assert "codex-ws" in upstream_routes.CHOICES
    assert isinstance(upstream_routes.create_bridge(None,lambda:None,"codex-ws"),WebSocketBridge)


def test_cli_isolated_home_is_removed_and_tokens_are_not_returned(tmp_path):
    script=tmp_path/"fake.py"
    marker=tmp_path/"home-path.txt"
    script.write_text("import os,json,pathlib,sys\n"+
        f"pathlib.Path({str(marker)!r}).write_text(os.environ['CODEX_HOME'])\n"+
        "home=pathlib.Path(os.environ['CODEX_HOME']); auth=json.loads((home/'auth.json').read_text())\n"+
        "assert auth['tokens']['refresh_token']==''\nassert 'OPENAI_API_KEY' not in os.environ\n"+
        "sys.stdin.read()\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\"ok\":true}'}}))\n"+
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':3}}))\n")
    selected=Snapshot({},"",{},auth={"tokens":{"access_token":"secret","id_token":"secret-id","refresh_token":""}},executable=sys.executable)
    body=tasks.payload(tasks.generate("smoke",1)[0],MODEL,"low")
    with patch.object(native_cli,"arguments",return_value=[sys.executable,str(script)]),patch.dict(os.environ,{"OPENAI_API_KEY":"must-not-inherit"}):
        result=asyncio.run(native_cli.call(selected,body))
    assert result.status=="completed" and result.answer=='{"ok":true}'
    assert not result.submissions_known and result.model is None
    assert not Path(marker.read_text()).exists()
    assert "secret" not in json.dumps(result.record())


def test_cli_cancellation_removes_isolated_credentials(tmp_path):
    script=tmp_path/"wait.py"
    marker=tmp_path/"home-path.txt"
    script.write_text("import pathlib,os,time\n"+f"pathlib.Path({str(marker)!r}).write_text(os.environ['CODEX_HOME'])\n"+"time.sleep(60)\n")
    selected=Snapshot({},"",{},auth={"tokens":{"refresh_token":""}},executable=sys.executable)
    async def check():
        pending=asyncio.create_task(native_cli.call(selected,tasks.payload(tasks.generate("smoke",1)[0],MODEL,"low")))
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.02)
        assert marker.exists()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    with patch.object(native_cli,"arguments",return_value=[sys.executable,str(script)]):
        asyncio.run(check())
    assert not Path(marker.read_text()).exists()
