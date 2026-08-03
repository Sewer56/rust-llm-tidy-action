#!/usr/bin/env python3
"""Run rust-llm-tidy using a native 7-Zip binary for release-asset extraction.

Reads GitHub-Action-style env inputs:
  INPUT_RELEASE_REPO   - owner/name of the repo hosting rust-llm-tidy releases
  INPUT_RELEASE_TAG    - release tag; empty = latest release
  INPUT_DOWNLOAD_ASSET - exact asset name to use; empty = auto-detect from the
                         runner OS/arch
  INPUT_INSTALL_DIR    - install dir; relative paths resolve under $RUNNER_TEMP
  SEVENZIP_BINARY      - path to a native 7-Zip CLI (from ensure_7z.py step);
                         empty falls back to 7z/7zz on PATH or libarchive tar
  RUNNER_OS / RUNNER_ARCH - current runner platform (auto-detect fallback)
  RUNNER_TEMP          - temp dir for relative install paths
  GITHUB_TOKEN         - optional token (higher rate limits, private repos)

Asset naming conventions understood (in priority order, given the runner):
  - Plain binary:  rust-llm-tidy-<target> , rust-llm-tidy
  - Archive:       rust-llm-tidy-<target>.<ext> , <target>.<ext>
    <ext> in {7z, tar.gz, zip, tar.xz}
  - <target> may be a full rust triple (x86_64-unknown-linux-gnu) or a compact
    form (linux-x64, linux-x86, macos-arm64, macos-x64, windows-x64,
    windows-x86). Matching is case-insensitive.

Archives extract to a single `rust-llm-tidy` (or `rust-llm-tidy.exe`).
7-Zip does not preserve the executable bit, so it is restored after install.

Emits:
  asset_name       - resolved asset name
  binary_path      - absolute path to the installed binary
  sevenzip_binary  - path to the 7-Zip binary used
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


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


def resolve_extractor(sevenzip_binary: str):
    """Return ([cmd], kind) to extract with. Kind is '7z' or 'libarchive-tar'."""
    if sevenzip_binary:
        if is_libarchive_tar(sevenzip_binary):
            return ([sevenzip_binary, "-xf"], "libarchive-tar")
        return ([sevenzip_binary, "x", "-y"], "7z")
    for name in ("7zz", "7z", "7za"):
        exe = shutil.which(name)
        if exe:
            return ([exe, "x", "-y"], "7z")
    tar_bin = shutil.which("tar")
    if tar_bin and is_libarchive_tar(tar_bin):
        return ([tar_bin, "-xf"], "libarchive-tar")
    sys.exit(
        "no native 7-Zip found; pass SEVENZIP_BINARY or install 7z/7zz on PATH"
    )


def target_names(runner_os: str, runner_arch: str):
    """Yield plausible asset target bases, full-triple first, then compact."""
    full = {
        ("Linux", "X64"): "x86_64-unknown-linux-gnu",
        ("Linux", "X86"): "i686-unknown-linux-gnu",
        ("Linux", "ARM64"): "aarch64-unknown-linux-gnu",
        ("Linux", "ARM"): "armv7-unknown-linux-gnueabihf",
        ("macOS", "X64"): "x86_64-apple-darwin",
        ("macOS", "ARM64"): "aarch64-apple-darwin",
        ("Windows", "X64"): "x86_64-pc-windows-msvc",
        ("Windows", "X86"): "i686-pc-windows-msvc",
        ("Windows", "ARM64"): "aarch64-pc-windows-msvc",
    }
    compact = {
        ("Linux", "X64"): "linux-x64",
        ("Linux", "X86"): "linux-x86",
        ("Linux", "ARM64"): "linux-arm64",
        ("Linux", "ARM"): "linux-arm",
        ("macOS", "X64"): "macos-x64",
        ("macOS", "ARM64"): "macos-arm64",
        ("Windows", "X64"): "windows-x64",
        ("Windows", "X86"): "windows-x86",
        ("Windows", "ARM64"): "windows-arm64",
    }
    names = []
    for table in (full, compact):
        v = table.get((runner_os, runner_arch))
        if v:
            names.append(v)
    return names


def select_asset(assets, runner_os, runner_arch, explicit_asset):
    if explicit_asset:
        for a in assets:
            if a["name"].lower() == explicit_asset.lower():
                return a
        return None
    bases = target_names(runner_os, runner_arch)
    candidates = {
        f"{b}{ext}".lower()
        for b in bases
        for ext in ("", ".7z", ".tar.gz", ".zip", ".tar.xz")
    }
    for a in assets:
        if a["name"].lower() in candidates:
            return a
    return None


def extract(extractor_cmd, archive: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run([*extractor_cmd, str(archive), f"-o{dest}"], check=True)


def main():
    release_repo = getenv("INPUT_RELEASE_REPO", "Sewer56/rust-llm-tidy")
    release_tag = getenv("INPUT_RELEASE_TAG")
    explicit_asset = getenv("INPUT_DOWNLOAD_ASSET")
    install_dir_raw = getenv("INPUT_INSTALL_DIR", "rust-llm-tidy-bin")
    sevenzip_binary = getenv("SEVENZIP_BINARY")
    token = getenv("GITHUB_TOKEN")
    runner_os = getenv("RUNNER_OS") or "Linux"
    runner_arch = getenv("RUNNER_ARCH") or "X64"

    extractor_cmd, kind = resolve_extractor(sevenzip_binary)

    base = f"https://api.github.com/repos/{release_repo}/releases"
    try:
        release = (
            api_request(f"{base}/tags/{release_tag}", token)
            if release_tag
            else api_request(f"{base}/latest", token)
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"failed to resolve release ({release_tag or 'latest'}): {exc}")
    assets = release.get("assets", [])

    selected = select_asset(assets, runner_os, runner_arch, explicit_asset)
    if not selected:
        avail = " ".join(sorted(a["name"] for a in assets))
        desc = explicit_asset or f"target {target_names(runner_os, runner_arch)}"
        sys.exit(
            f"no matching release asset for {desc} in {release['tag_name']}; "
            f"available: {avail}"
        )

    asset_name = selected["name"]
    url = selected["browser_download_url"]

    install_dir = Path(install_dir_raw)
    if not install_dir.is_absolute():
        runner_temp = getenv("RUNNER_TEMP")
        base_dir = Path(runner_temp) if runner_temp else Path(tempfile.gettempdir())
        install_dir = base_dir / install_dir_raw
    install_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rlt-dl-") as tmp:
        tmp = Path(tmp)
        dl = tmp / asset_name
        print(f"::group::Downloading {asset_name}")
        download(url, dl, token)
        print(f"downloaded {asset_name} ({dl.stat().st_size} bytes)")
        print("::endgroup::")

        lower = asset_name.lower()
        is_archive = lower.endswith((".7z", ".zip", ".tar.gz", ".tar.xz", ".tar"))
        if is_archive:
            extract(extractor_cmd, dl, tmp)
        else:
            # Plain binary asset.
            shutil.copy2(dl, tmp / "rust-llm-tidy")

        found = None
        for candidate in [tmp / "rust-llm-tidy", tmp / "rust-llm-tidy.exe"]:
            if candidate.is_file():
                found = candidate
                break
        if found is None:
            matches = sorted(
                p
                for p in tmp.rglob("*")
                if p.is_file() and "rust-llm-tidy" in p.name
            )
            if matches:
                found = matches[0]
        if found is None:
            sys.exit(f"asset {asset_name} contains no rust-llm-tidy binary")

        dest = install_dir / ("rust-llm-tidy.exe" if os.name == "nt" else "rust-llm-tidy")
        shutil.copy2(found, dest)
        # 7-Zip does not preserve the executable bit on extraction.
        if os.name != "nt":
            dest.chmod(0o755)
        print(f"installed to {dest}")

    emit_output("asset_name", asset_name)
    emit_output("binary_path", str(dest))
    emit_output("sevenzip_binary", extractor_cmd[0])


if __name__ == "__main__":
    main()
