"""Isolated official CLI baseline. One launch is not an attested inference count."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .storage import private_directory, write_json
from .transport import Result

MAX_BYTES = 8 * 1024 * 1024


def arguments(executable, work, payload):
    settings = {"cli_auth_credentials_store": "file", "model_provider": "openai", "chatgpt_base_url": "https://chatgpt.com/backend-api",
                "model_reasoning_effort": payload["reasoning"]["effort"], "web_search": "disabled",
                "features.shell_tool": False, "features.multi_agent": False, "agents.enabled": False,
                "apps._default.enabled": False, "memories.use_memories": False, "memories.generate_memories": False,
                "project_doc_max_bytes": 0, "analytics.enabled": False, "feedback.enabled": False}
    command = [executable, "--no-daemon", "-a", "never", "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(work), "--model", payload["model"], "--color", "never"]
    for key, value in settings.items():
        command.extend(["-c", key + "=" + json.dumps(value)])
    return [*command, "-"]


async def terminate(process):
    if os.name == "nt":
        if process.returncode is None:
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(killer.wait(), 5)
            except asyncio.TimeoutError:
                killer.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        pass


async def call(snapshot, payload):
    if payload.get("tools"):
        return Result("unsupported_tools", wire="cli", submissions_known=False)
    with tempfile.TemporaryDirectory(prefix="codex-channel-trial-") as temp:
        root = Path(temp)
        private_directory(root)
        home, work = root / "codex", root / "work"
        private_directory(home)
        work.mkdir()
        auth = {**snapshot.auth, "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        write_json(home / "auth.json", auth, new=True)
        allowed = {"SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "LANG", "LC_ALL", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy"}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        for name in ("profile", "local", "roaming", "tmp"):
            (root / name).mkdir()
        env.update(CODEX_HOME=str(home), USERPROFILE=str(root / "profile"), LOCALAPPDATA=str(root / "local"), APPDATA=str(root / "roaming"), TEMP=str(root / "tmp"), TMP=str(root / "tmp"))
        if os.environ.get("EXCEL_BRIDGE_PROXY"):
            env["HTTPS_PROXY"] = os.environ["EXCEL_BRIDGE_PROXY"]
        command = arguments(snapshot.executable, work, payload)
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(*command, cwd=work, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=MAX_BYTES, **options)
        try:
            prompt = payload["instructions"] + "\n\n" + payload["input"][0]["content"]
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()
            size, completed, answer, usage = 0, False, [], {}
            async for line in process.stdout:
                size += len(line)
                if size > MAX_BYTES:
                    return Result("cli_output_limit", wire="cli", submissions_known=False)
                event = json.loads(line)
                if event.get("type") in {"turn.failed", "error"}:
                    return Result("cli_failed", wire="cli", submissions_known=False)
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        answer.append(item.get("text", ""))
                    elif item.get("type") not in {"reasoning"}:
                        return Result("cli_unexpected_tool", wire="cli", submissions_known=False)
                if event.get("type") == "turn.completed":
                    completed = True
                    usage = {k: v for k, v in (event.get("usage") or {}).items() if k in {"input_tokens", "output_tokens"} and type(v) is int and v >= 0}
            code = await process.wait()
            return Result("completed" if completed and code == 0 else "cli_missing_completion", answer="".join(answer),
                          usage=usage, wire="cli", submissions_known=False)
        finally:
            await terminate(process)
