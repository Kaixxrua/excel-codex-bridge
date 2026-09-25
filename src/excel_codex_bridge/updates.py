"""Tell the user when a newer release is out.

The check is one GET to GitHub's public releases API, at most every twelve
hours, sending nothing but this version in the User-Agent; it uses the same
proxy settings as the bridge. The answer is kept in the state folder, and the
check runs in the background, so starting never waits on GitHub. A failed
check prints nothing. ``EXCEL_BRIDGE_UPDATE_CHECK=0`` turns it off.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import __version__

REPOSITORY = "Kaixxrua/excel-codex-bridge"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
CHECK_EVERY_SECONDS = 12 * 3600
TIMEOUT_SECONDS = 10.0
CACHE_NAME = "update-check.json"
_MAX_SUMMARY = 160


@dataclass(frozen=True)
class Release:
    version: str
    summary: str
    url: str


def enabled() -> bool:
    value = os.environ.get("EXCEL_BRIDGE_UPDATE_CHECK", "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _version_key(version: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    if match is None:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(version: str, current: str = __version__) -> bool:
    new, old = _version_key(version), _version_key(current)
    return new is not None and old is not None and new > old


def _plain(text: str) -> str:
    """One line of printable text, so a release note cannot move the cursor."""
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.replace("**", "").split())
    return text if len(text) <= _MAX_SUMMARY else text[: _MAX_SUMMARY - 1] + "…"


def release_from(data: object) -> Release | None:
    """The release in a GitHub releases API answer, if it is a usable one."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or _version_key(tag) is None:
        return None
    version = tag.strip().removeprefix("v")
    # The release notes start with a one-line summary; the release name is just the tag.
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    url = data.get("html_url")
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{REPOSITORY}/"):
        url = RELEASES_PAGE
    return Release(version=version, summary=_plain(first_line), url=url)


def fetch_latest(url: str = LATEST_URL) -> Release | None:
    proxy = os.environ.get("EXCEL_BRIDGE_PROXY", "").strip() or None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"excel-codex-bridge/{__version__}",
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, proxy=proxy, trust_env=proxy is None) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return release_from(response.json())
    except (httpx.HTTPError, ValueError):
        return None


class UpdateCheck:
    """The latest release: from the cache while it is recent, else from GitHub."""

    def __init__(
        self,
        cache: Path,
        *,
        fetch: Callable[[], Release | None] = fetch_latest,
        every: float = CHECK_EVERY_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache = cache
        self.fetch = fetch
        self.every = every
        self.clock = clock

    def _load(self) -> tuple[float, Release | None] | None:
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
            checked_at = float(data["checked_at"])
            release = data.get("release")
            return checked_at, Release(**release) if release else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save(self, checked_at: float, release: Release) -> None:
        payload = {"checked_at": checked_at, "release": release.__dict__}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def latest(self) -> Release | None:
        cached = self._load()
        now = self.clock()
        if cached is not None and 0 <= now - cached[0] < self.every:
            return cached[1]
        release = self.fetch()
        if release is None:
            # GitHub unreachable: keep what was known, and ask again next time.
            return cached[1] if cached else None
        self._save(now, release)
        return release

    def newer(self) -> Release | None:
        release = self.latest()
        return release if release is not None and is_newer(release.version) else None


def notice(release: Release) -> str:
    lines = [f"Update available: {release.version} (you have {__version__})."]
    if release.summary:
        lines.append(f"  {release.summary}")
    lines.append(f"  -> Download: {release.url}")
    return "\n".join(lines)


def watch(check: UpdateCheck, announce: Callable[[Release], None]) -> threading.Thread:
    """Announce each newer release once: now, then every ``check.every`` seconds."""

    def run() -> None:
        announced = None
        while True:
            try:
                release = check.newer()
                if release is not None and release.version != announced:
                    announced = release.version
                    announce(release)
            except Exception:  # never let the check take the bridge down
                pass
            time.sleep(check.every)

    thread = threading.Thread(target=run, name="update-check", daemon=True)
    thread.start()
    return thread


def in_background(check: UpdateCheck) -> Callable[[float], Release | None]:
    """Start one check; the returned function waits up to ``timeout`` seconds for its answer."""
    answer: list[Release | None] = []
    done = threading.Event()

    def run() -> None:
        try:
            answer.append(check.newer())
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=run, name="update-check", daemon=True).start()

    def wait(timeout: float) -> Release | None:
        done.wait(timeout)
        return answer[0] if answer else None

    return wait
