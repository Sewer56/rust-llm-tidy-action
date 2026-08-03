#!/usr/bin/env python3
"""Ensure a native 7-Zip CLI is available for extracting release artifacts.

Reads GitHub-Action-style env inputs:
  INPUT_SEVENZIP_SOURCE - where 7-Zip comes from:
      auto          use 7z/7zz on the runner if present; else download a
                    standalone 7-Zip (GitHub ip7z/7zip releases)
      fetch         always download a standalone 7-Zip
      version:<x.y> download a specific version (e.g. version:26.02)
      apt           Linux only: require p7zip-full already installed (no download)
      preinstalled  require 7z/7zz on the runner (no download)
  RUNNER_OS / RUNNER_ARCH - current runner platform
  RUNNER_TEMP      - scratch dir; extracted 7-Zip goes to $RUNNER_TEMP/sevenzip-bin,
                     downloaded archives to $RUNNER_TEMP/sevenzip-bin-src
  GITHUB_TOKEN     - optional token for the GitHub API (higher rate limits)

Emits:
  sevenzip_binary - path to the 7-Zip CLI (also SEVENZIP_BINARY env export)
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

SEVENZIP_GITHUB_REPO = "ip7z/7zip"


def getenv(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def api_request(url: str, token: str):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def download(url: str, dest: Path, token: str):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def emit_output(name: str, value: str):
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as f:
            f.write(f"{name}={value}\n")
    print(f"::set-output name={name}::{value}")  # noqa: C0209


def is_libarchive_tar(exe: str) -> bool:
    try:
        ver = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        return "libarchive" in ver
    except Exception:  # noqa: BLE001
        return False


def find_env_sevenzip():
    """Try to locate a usable 7-Zip CLI already on the runner."""
    for name in ("7zz", "7z", "7za"):
        exe = shutil.which(name)
        if exe:
            return exe, "path"
    # macOS/Windows libarchive tar can read 7z (and tar/zip) natively.
    tar_bin = shutil.which("tar")
    if tar_bin and is_libarchive_tar(tar_bin):
        return tar_bin, "libarchive-tar"
    for cand in (
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    ):
        if cand.is_file():
            return str(cand), "windows-install"
    return None, None


def latest_sevenzip_tag(token: str) -> str:
    releases = api_request(
        f"https://api.github.com/repos/{SEVENZIP_GITHUB_REPO}/releases/latest",
        token,
    )
    return releases["tag_name"]


def fetch_sevenzip(
    version: str | None, runner_os: str, runner_arch: str, src_dir: Path, extract_dir: Path, token: str
) -> str:
    tag = version or latest_sevenzip_tag(token)
    short = tag.replace(".", "")
    base_url = f"https://github.com/{SEVENZIP_GITHUB_REPO}/releases/download/{tag}"

    if runner_os == "Windows":
        url = f"{base_url}/7zr.exe"
        archive = src_dir / f"7zr-{tag}.exe"
        if not archive.is_file():
            download(url, archive, token)
        exe = extract_dir / f"7zr-{tag}.exe"
        shutil.copy2(archive, exe)
        return str(exe)

    arch_map = {
        ("Linux", "X64"): "x64",
        ("Linux", "X86"): "x86",
        ("Linux", "ARM64"): "arm64",
        ("Linux", "ARM"): "arm",
        ("macOS", "X64"): "mac",
        ("macOS", "ARM64"): "mac",
    }
    key = (runner_os, runner_arch)
    if key not in arch_map:
        sys.exit(f"unsupported platform for 7-Zip fetch: {runner_os}/{runner_arch}")
    filename = (
        f"7z{short}-mac.tar.xz"
        if runner_os == "macOS"
        else f"7z{short}-linux-{arch_map[key]}.tar.xz"
    )

    archive = src_dir / filename
    if not archive.is_file():
        download(f"{base_url}/{filename}", archive, token)

    # Extract into a versioned subdir so multiple versions can coexist.
    ver_dir = extract_dir / short
    ver_dir.mkdir(parents=True, exist_ok=True)
    if not (ver_dir / "7zz").is_file() and not (ver_dir / "7zzs").is_file():
        with tarfile.open(archive) as tar:
            tar.extractall(ver_dir, filter="data")
    for name in ("7zz", "7zzs"):
        cand = ver_dir / name
        if cand.is_file():
            cand.chmod(0o755)
            return str(cand)
    sys.exit(f"7-Zip tarball for {filename} contains no 7zz binary")


def main():
    source = getenv("INPUT_SEVENZIP_SOURCE", "auto")
    token = getenv("GITHUB_TOKEN")
    runner_os = getenv("RUNNER_OS") or "Linux"
    runner_arch = getenv("RUNNER_ARCH") or "X64"
    runner_temp = getenv("RUNNER_TEMP") or tempfile.gettempdir()
    base = Path(runner_temp)

    explicit_version = None
    if source.startswith("version:"):
        explicit_version = source.split(":", 1)[1].strip()
        source = "fetch"

    if source == "preinstalled":
        found, kind = find_env_sevenzip()
        if not found:
            sys.exit("sevenzip-source: preinstalled requested, but no 7z found on the runner")
        print(f"using {kind}: {found}")
        emit_output("sevenzip_binary", found)
        return

    if source == "apt":
        if runner_os != "Linux":
            sys.exit("sevenzip-source: apt is only supported on Linux")
        found, kind = find_env_sevenzip()
        if not found:
            sys.exit(
                "sevenzip-source: apt requested, but no 7z found; ensure p7zip-full is installed"
            )
        print(f"using {kind}: {found}")
        emit_output("sevenzip_binary", found)
        return

    # auto / fetch
    if source == "auto":
        found, kind = find_env_sevenzip()
        if found:
            print(f"using {kind}: {found}")
            emit_output("sevenzip_binary", found)
            return

    src_dir = base / "sevenzip-bin-src"
    extract_dir = base / "sevenzip-bin"
    src_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    binary = fetch_sevenzip(explicit_version, runner_os, runner_arch, src_dir, extract_dir, token)
    print(f"fetched 7-Zip: {binary}")
    emit_output("sevenzip_binary", binary)


if __name__ == "__main__":
    main()
