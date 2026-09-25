"""Exercise the shipped Compose config with synthetic credentials only.

Build the image first. Uses its own temporary Compose project/network/container;
never attaches to a real SUB2API network or sends an authenticated OpenAI request.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "smoke-only-api-" + "a" * 48
ADMIN_KEY = "smoke-only-admin-" + "b" * 48
SESSION = json.dumps({"headers": {
    "authorization": "Bearer synthetic-smoke-session-not-a-real-credential",
    "chatgpt-account-id": "synthetic-smoke-account",
}})


def run(*args, input=None, check=True, env=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True,
                            timeout=180, env=env, cwd=ROOT)
    if check and result.returncode:
        raise RuntimeError(f"{args[:3]} failed:\n{result.stdout}\n{result.stderr}")
    return result


def wait(container):
    for _ in range(45):
        result = run("docker", "exec", container, "python", "-c",
                     "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).close()",
                     check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("Sidecar did not become healthy")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    name = "excel-sub2api-smoke-" + uuid.uuid4().hex[:12]
    env = {**os.environ, "SUB2API_NETWORK": name, "EXCEL_BRIDGE_PROXY": ""}
    with tempfile.TemporaryDirectory(prefix=name) as temp:
        directory = Path(temp)
        for filename, key in (("api-key", API_KEY), ("admin-key", ADMIN_KEY)):
            path = directory / filename
            path.write_text(key, encoding="ascii")
            path.chmod(0o600)
        # Exactly these synthetic files; mimic deployment ownership without sudo.
        run("docker", "run", "--rm", "--user", "0:0", "--mount",
            f"type=bind,src={directory},dst=/secrets", args.image, "chown", "10001:10001",
            "/secrets/api-key", "/secrets/admin-key")
        override = directory / "override.json"
        override.write_text(json.dumps({
            "services": {"excel-sub2api": {"container_name": name, "image": args.image, "restart": "no"}},
            "secrets": {filename: {"file": str(directory / filename)} for filename in ("api-key", "admin-key")},
        }), encoding="utf-8")
        compose = ["docker", "compose", "--project-name", name,
                   "-f", str(ROOT / "packaging/sub2api/compose.yaml"), "-f", str(override)]
        run(*compose, "config", "--quiet", env=env)
        run("docker", "network", "create", name)
        try:
            run(*compose, "up", "-d", "--no-build", env=env)
            wait(name)
            metadata = json.loads(run("docker", "inspect", name).stdout)[0]
            assert metadata["Config"]["User"] == "10001:10001"
            assert metadata["HostConfig"]["ReadonlyRootfs"] is True
            assert all(value is None for value in metadata["NetworkSettings"]["Ports"].values())
            print("PASS: shipped Compose, non-root, read-only, no published ports")

            probe = f'''
import httpx
with httpx.Client(base_url="http://excel-sub2api:8000", trust_env=False, timeout=10) as c:
    api = {{"authorization": "Bearer {API_KEY}"}}
    admin = {{"authorization": "Bearer {ADMIN_KEY}", "x-forwarded-for": "127.0.0.1"}}
    assert c.get("/healthz").json() == {{"ok": True}}
    assert c.get("/v1/models").status_code == 401
    models = c.get("/v1/models", headers=api).json()["data"]
    assert models and all(m["id"].endswith("-excel") for m in models)
    assert c.get("/admin/session", headers=admin).status_code == 404
    assert c.post("/admin/session", headers=admin, json={{}}).status_code == 404
    assert c.get("/models", headers={{**api, "origin": "https://example.com"}}).status_code == 403
    assert c.post("/v1/responses", headers=api, json={{"model":models[0]["id"],"input":"no session"}}).status_code == 401
print("PASS: cross-container API authentication, Origin denial, admin isolation and forwarded-header defense")
'''
            print(run("docker", "run", "--rm", "-i", "--network", name, args.image,
                      "python", "-", input=probe).stdout.strip())

            def control(command, payload=None):
                return json.loads(run("docker", "exec", "-i", name, "excel-sub2api", command,
                                      input=payload).stdout)

            assert control("import-session", SESSION)["configured"] is True
            status = control("session-status")
            assert status["configured"] is True and "synthetic" not in json.dumps(status)
            assert control("clear-session")["configured"] is False
            assert control("import-session", SESSION)["configured"] is True
            run("docker", "restart", name)
            wait(name)
            assert control("session-status")["configured"] is False
            print("PASS: stdin import, redacted status, clear, and session loss on restart")
        finally:
            run(*compose, "down", "--timeout", "5", env=env, check=False)
            run("docker", "network", "rm", name, check=False)
    print("SUB2API Docker smoke checks passed (no real credentials or upstream inference).")


if __name__ == "__main__":
    main()
