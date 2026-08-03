#!/usr/bin/env python3
"""Resolve, download and install a prebuilt rust-llm-tidy release artifact.

Reads GitHub-Action-style env inputs:
  INPUT_RELEASE_REPO   - owner/name of the repo hosting releases
  INPUT_RELEASE_TAG    - release tag; empty = latest release
  INPUT_DOWNLOAD_ASSET - exact asset name to use; empty = auto-detect from the
                         runner OS/arch
  INPUT_INSTALL_DIR    - install dir; relative paths resolve under $RUNNER_TEMP
  RUNNER_OS / RUNNER_ARCH - current runner platform (auto-detect fallback)
  GITHUB_TOKEN         - optional token (higher rate limits, private repos)

Asset naming conventions understood (in priority order, given the runner):
  - Plain binary:  rust-llm-tidy-<target> , rust-llm-tidy
  - Archive:       <base>.<ext> where <ext> is 7z / tar.gz / zip / tar.xz
  - <target> may be a full rust triple (x86_64-unknown-linux-gnu) or a compact
    form (linux-x64, linux-x86, macos-arm64, macos-x64, windows-x64,
    windows-x86). Matching is case-insensitive.

Archives extract to a single `rust-llm-tidy` (or `rust-llm-tidy.exe`).

Emits:
  asset_name   - resolved asset name (also GITHUB_OUTPUT or fallback set-output)
  binary_path  - absolute path to the installed binary
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
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
        names.append(table.get((runner_os, runner_arch)))
    return [n for n in names if n]


def select_asset(assets, runner_os, runner_arch, explicit_asset):
    if explicit_asset:
        for a in assets:
            if a["name"].lower() == explicit_asset.lower():
                return a
        return None
    bases = target_names(runner_os, runner_arch)
    extensions = ["", ".7z", ".tar.gz", ".zip", ".tar.xz"]
    for base in bases:
        for ext in extensions:
            want = f"{base}{ext}".lower()
            for a in assets:
                if a["name"].lower() == want:
                    return a
    return None


def find_7z_extractor():
    """Return a py7zr callable+args strategy or a 7z binary string."""
    # 1) py7zr (pure python, installed on-demand below if importable)
    try:
        import py7zr  # noqa: F401

        return "py7zr"
    except ImportError:
        pass
    # 2) packaged 7z binaries on PATH
    for name in ("7z", "7za", "7zz"):
        if shutil.which(name):
            return name
    # 3) Windows default 7-Zip install
    win_7z = Path(r"C:\Program Files\7-Zip\7z.exe")
    if win_7z.is_file():
        return str(win_7z)
    # 4) bsdtar (libarchive) - macOS ships libarchive tar; Windows has bsdtar
    #    alongside tar.exe. GNU tar cannot read 7z, so verify libarchive first.
    tar_bin = shutil.which("tar")
    if tar_bin:
        try:
            ver = subprocess.run(
                [tar_bin, "--version"], capture_output=True, text=True, timeout=10
            ).stdout
            if "libarchive" in ver:
                return "bsdtar"
        except Exception:  # noqa: BLE001
            pass
    return None


def ensure_py7zr():
    try:
        import py7zr  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "-q", "py7zr"],
            check=True,
        )
        import py7zr  # noqa: F401,F811

        return True
    except Exception:  # noqa: BLE001
        return False


def extract_7z(archive: Path, dest: Path):
    mode = find_7z_extractor()
    if mode == "py7zr":
        import py7zr

        with py7zr.SevenZipFile(archive) as z:
            z.extractall(dest)
        return
    if mode == "bsdtar":
        subprocess.run(["tar", "-xf", str(archive), "-C", str(dest)], check=True)
        return
    if mode:
        subprocess.run([mode, "x", f"-o{dest}", str(archive), "-y"], check=True)
        return
    if ensure_py7zr():
        import py7zr

        with py7zr.SevenZipFile(archive) as z:
            z.extractall(dest)
        return
    sys.exit(
        "cannot extract .7z asset: no 7z binary and py7zr not installable. "
        "Install py7zr or use a tar.gz/zip release."
    )


def emit_output(name: str, value: str):
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as f:
            f.write(f"{name}={value}\n")
    print(f"::set-output name={name}::{value}")  # noqa: C0209


def main():
    release_repo = getenv("INPUT_RELEASE_REPO", "Sewer56/rust-llm-tidy")
    release_tag = getenv("INPUT_RELEASE_TAG")
    explicit_asset = getenv("INPUT_DOWNLOAD_ASSET")
    install_dir_raw = getenv("INPUT_INSTALL_DIR", "rust-llm-tidy-bin")
    token = getenv("GITHUB_TOKEN")
    runner_os = getenv("RUNNER_OS") or "Linux"
    runner_arch = getenv("RUNNER_ARCH") or "X64"

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
        if lower.endswith(".tar.gz") or lower.endswith(".tar.xz"):
            with tarfile.open(dl) as tar:
                tar.extractall(tmp, filter="data")
        elif lower.endswith(".zip"):
            with zipfile.ZipFile(dl) as z:
                z.extractall(tmp)
        elif lower.endswith(".7z"):
            extract_7z(dl, tmp)
        else:
            # Plain binary asset.
            binary = tmp / asset_name
            dest = tmp / "rust-llm-tidy"
            if binary.resolve() != dest.resolve():
                binary.rename(dest)

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
            sys.exit(f"archive {asset_name} contains no rust-llm-tidy binary")

        dest = install_dir / ("rust-llm-tidy.exe" if os.name == "nt" else "rust-llm-tidy")
        shutil.copy2(found, dest)
        if os.name != "nt":
            dest.chmod(0o755)
        print(f"installed to {dest}")

    emit_output("asset_name", asset_name)
    emit_output("binary_path", str(dest))


if __name__ == "__main__":
    main()
