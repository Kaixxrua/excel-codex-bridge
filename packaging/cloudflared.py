"""Put a pinned cloudflared next to the packaged excel-codex, for pictures.

    python packaging/cloudflared.py windows-amd64 dist/excel-codex

The bridge serves pictures through a cloudflared quick tunnel
(``src/excel_codex_bridge/local_host.py``) and looks for the binary next to
itself first.  cloudflared is Cloudflare's, under the Apache License 2.0
(``CLOUDFLARED-LICENSE``, copied alongside it).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import tarfile
import urllib.request
from pathlib import Path

VERSION = "2026.9.3"
# GitHub's own digests of the release files.  (Cloudflare's release notes list
# different sums for the macOS archives: those are repacked after signing.)
ASSETS = {
    "windows-amd64": ("cloudflared-windows-amd64.exe",
                      "f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2"),
    "darwin-arm64": ("cloudflared-darwin-arm64.tgz",
                     "587c2cfb1c230fe36c7fa7727da78be459dae028cabe8c001291999350f07095"),
    "darwin-amd64": ("cloudflared-darwin-amd64.tgz",
                     "d1155d0837487f261183b15c1eab6c4ebcad9dc49b94675f1524c3564cea3977"),
    "linux-amd64": ("cloudflared-linux-amd64",
                    "77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2"),
}
URL = "https://github.com/cloudflare/cloudflared/releases/download/{version}/{name}"
HERE = Path(__file__).resolve().parent


def fetch(platform: str, directory: Path) -> Path:
    name, digest = ASSETS[platform]
    url = URL.format(version=VERSION, name=name)
    with urllib.request.urlopen(url, timeout=600) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise SystemExit(f"{name}: sha256 {actual}, expected {digest}")
    if name.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member = archive.extractfile("cloudflared")
            if member is None:
                raise SystemExit(f"{name} has no cloudflared binary")
            data = member.read()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("cloudflared.exe" if platform.startswith("windows") else "cloudflared")
    target.write_bytes(data)
    target.chmod(0o755)
    shutil.copyfile(HERE / "CLOUDFLARED-LICENSE", directory / "CLOUDFLARED-LICENSE")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("platform", choices=sorted(ASSETS))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(f"cloudflared {VERSION} -> {fetch(args.platform, args.directory)}")


if __name__ == "__main__":
    main()
