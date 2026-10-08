"""Exercise source/frozen research commands against a local synthetic Responses service."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("command",nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command and args.command[0]=="--" else args.command
    if not command:
        parser.error("pass -- <launcher command>")
    command=[str(Path(p).absolute()) if Path(p).is_file() else p for p in command]
    calls=[]
    fixtures=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            assert self.path=="/v1/responses"
            assert self.headers.get("Authorization")=="Bearer synthetic-channel-key"
            body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            task=next(t for t in fixtures if t["prompt"]==body["input"][0]["content"])
            if task["tools"] and len(body["input"])==1:
                output=[{"type":"reasoning","encrypted_content":"opaque-fixture-not-in-report"},
                        {"type":"function_call","name":"fixture_read","call_id":"c1","arguments":json.dumps({"key":task["fixture"]["key"]})}]
            else:
                if task["tools"]:
                    assert body["input"][1]["encrypted_content"]=="opaque-fixture-not-in-report"
                    assert json.loads(body["input"][-1]["output"])==task["expected"]
                output=[{"type":"message","role":"assistant","content":[{"type":"output_text","text":json.dumps(task["expected"])}]}]
            response={"object":"response","status":"completed","model":body["model"],"reasoning":body["reasoning"],"output":output}
            data=("event: response.completed\ndata: "+json.dumps({"type":"response.completed","response":response})+"\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    with tempfile.TemporaryDirectory(prefix="channels-smoke-") as temp,ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=Path(temp)
        env={**os.environ,"CHANNELS_SMOKE_KEY":"synthetic-channel-key","EXCEL_BRIDGE_UPDATE_CHECK":"0",
             "EXCEL_BRIDGE_CODEX_AUTH":str(root/"no-real-auth.json"),"NO_PROXY":"127.0.0.1,localhost","no_proxy":"127.0.0.1,localhost"}
        env.pop("EXCEL_BRIDGE_PROXY",None)
        def run(*extra):
            process=subprocess.run([*command,"research",*extra],env=env,capture_output=True,text=True,encoding="utf-8",timeout=60)
            if process.returncode:
                raise RuntimeError(process.stdout+process.stderr)
            return json.loads(process.stdout)
        try:
            assert len(run("routes")["routes"])==6
            study=root/"study"
            configuration={"model":"gpt-5.6-sol","seed":91,"max_calls":6,"routes":[
                {"id":name,"kind":"responses-http","base_url":f"http://127.0.0.1:{server.server_port}/v1","key_env":"CHANNELS_SMOKE_KEY"} for name in ("first","second")]}
            path=root/"config.json";path.write_text(json.dumps(configuration))
            run("prepare","--out",str(study),"--config",str(path))
            fixtures.extend(json.loads((study/"manifest.json").read_text())["tasks"])
            assert all(r["ready"] for r in run("inventory","--study",str(study))["routes"])
            assert not run("request-diff","--study",str(study))["inference_started"]
            assert not calls
            run("run","--study",str(study))
            report=run("report","--study",str(study))
            assert len(calls)==6 and all(r["passed_tasks"]==2 for r in report["routes"])
            assert not report["pairs"][0]["same_account_native_comparison"]
            run("run","--study",str(study))
            assert len(calls)==6
            raw=(study/"report.json").read_text()
            assert "synthetic-channel-key" not in raw and "opaque-fixture" not in raw
            print("PASS: packaged multi-channel prepare, request-diff, inventory, six bounded submissions, tool continuation, report and resume")
        finally:
            server.shutdown();thread.join(timeout=5)


if __name__=="__main__": main()
