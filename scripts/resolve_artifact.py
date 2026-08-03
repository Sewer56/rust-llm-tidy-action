#!/usr/bin/env python3
"""Download and extract a rust-llm-tidy release artifact using a native 7-Zip.

Reads GitHub-Action-style env inputs:
  INPUT_RELEASE_REPO   - owner/name of the repo hosting rust-llm-tidy releases
  INPUT_RELEASE_TAG    - release tag; empty = latest release
  INPUT_DOWNLOAD_ASSET - exact asset name to use; empty = auto-detect from the
                         runner OS/arch
  INPUT_INSTALL_DIR    - install dir; relative paths resolve under $RUNNER_TEMP
  SEVENZIP_BINARY      - path to a native 7-Zip CLI (from ensure_7z.py); empty
                         falls back to 7z/7zz on PATH or libarchive tar
  RUNNER_OS / RUNNER_ARCH - current runner platform (auto-detect fallback)
  RUNNER_TEMP          - temp dir for relative install paths
  GITHUB_TOKEN         - optional token (higher rate limits, private repos)

Asset naming conventions understood (in priority order, given the runner):
  - Plain binary:  rust-llm-tidy-<target> , rust-llm-tidy
  - Archive:       rust-llm-tidy-<target>.<ext> , <target>.<ext>
    <ext> in {7z, tar.gz, tar.xz, tar.bz2, zip, tar.zst}
  - <target> may be a full rust triple (x86_64-unknown-linux-gnu) or a compact
    form (linux-x64, linux-x86, macos-arm64, macos-x64, windows-x64,
    windows-x86). Matching is case-insensitive.

Archives extract to a single `rust-llm-tidy` (or `rust-llm-tidy.exe`).
7-Zip does not preserve the executable bit, so it is restored after install.

Emits (via GITHUB_OUTPUT):
  asset_name       - resolved asset name
  binary_path      - absolute path to the installed binary
  sevenzip_binary  - path to the 7-Zip binary used
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")

# Archive extensions 7-Zip can extract. A trailing-compare on the lowercased
# name groups these together; anything else is treated as a plain binary.
_ARCHIVE_SUFFIXES = (".7z", ".zip", ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar")


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


def write_output(name: str, value: str):
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        print(f"{name}={value}")
        return
    with open(out_path, "a") as f:
        f.write(f"{name}={value}\n")


def is_libarchive_tar(exe: str) -> bool:
    try:
        ver = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        return "libarchive" in ver
    except Exception:  # noqa: BLE001
        return False


def find_native_sevenzip():
    """Locate an installed 7z/7zz (or libarchive tar) and describe its style."""
    for name in ("7zz", "7z", "7za"):
        exe = shutil.which(name)
        if exe:
            return exe, "7z"
    for cand in (
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    ):
        if cand.is_file():
            return str(cand), "7z"
    tar_bin = shutil.which("tar")
    if tar_bin and is_libarchive_tar(tar_bin):
        return tar_bin, "libarchive-tar"
    return None, None


def resolve_extractor(sevenzip_binary: str):
    """Return (binary, make_args) for extraction.

    make_args(archive, dest) -> list of argv. Handles native 7-Zip and
    libarchive-tar (macOS/Windows) which takes a different argument layout
    (`-C <dir>` and no -o).
    """
    if sevenzip_binary:
        if is_libarchive_tar(sevenzip_binary):
            # bsdtar style: tar -xf <archive> -C <dest>
            return (
                sevenzip_binary,
                lambda archive, dest: [
                    sevenzip_binary, "-xf", str(archive), "-C", str(dest)
                ],
            )
        # 7-Zip style: 7z x -y <archive> -o<dest>
        return (
            sevenzip_binary,
            lambda archive, dest: [
                sevenzip_binary, "x", "-y", str(archive), f"-o{dest}"
            ],
        )

    exe, style = find_native_sevenzip()
    if not exe:
        sys.exit(
            "no native archiver found; pass SEVENZIP_BINARY or install 7z/7zz "
            "on PATH"
        )
    if style == "libarchive-tar":
        return exe, lambda archive, dest: [exe, "-xf", str(archive), "-C", str(dest)]
    return exe, lambda archive, dest: [exe, "x", "-y", str(archive), f"-o{dest}"]


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
        for ext in ("", ".7z", ".tar.gz", ".zip", ".tar.xz", ".tar.bz2", ".tar.zst")
    }
    for a in assets:
        if a["name"].lower() in candidates:
            return a
    return None


def sanitize_asset_name(name: str) -> str:
    """Reject names that could escape the temp dir (path traversal)."""
    base = Path(name).name
    if base != name or not _SAFE_NAME.fullmatch(base):
        sys.exit(f"unsafe release asset name: {name!r}")
    return base


def is_archive(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(_ARCHIVE_SUFFIXES)


def main():
    release_repo = getenv("INPUT_RELEASE_REPO") or "Sewer56/rust-llm-tidy"
    release_tag = getenv("INPUT_RELEASE_TAG")
    explicit_asset = getenv("INPUT_DOWNLOAD_ASSET")
    install_dir_raw = getenv("INPUT_INSTALL_DIR") or "rust-llm-tidy-bin"
    sevenzip_binary = getenv("SEVENZIP_BINARY")
    token = getenv("GITHUB_TOKEN")
    runner_os = getenv("RUNNER_OS") or "Linux"
    runner_arch = getenv("RUNNER_ARCH") or "X64"

    extractor, make_args = resolve_extractor(sevenzip_binary)

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

    asset_name = sanitize_asset_name(selected["name"])
    url = selected["browser_download_url"]
    used_asset = selected["name"]

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

        if is_archive(asset_name):
            subprocess.run(make_args(dl, tmp), check=True)
        else:
            # Plain binary asset.
            shutil.copy2(dl, tmp / "rust-llm-tidy")

        found = None
        for candidate in (tmp / "rust-llm-tidy", tmp / "rust-llm-tidy.exe"):
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
            sys.exit(f"asset {used_asset} contains no rust-llm-tidy binary")

        dest = install_dir / (
            "rust-llm-tidy.exe" if os.name == "nt" else "rust-llm-tidy"
        )
        shutil.copy2(found, dest)
        # 7-Zip does not preserve the executable bit on extraction.
        if os.name != "nt":
            dest.chmod(0o755)
        print(f"installed to {dest}")

    write_output("asset_name", asset_name)
    write_output("binary_path", str(dest))
    write_output("sevenzip_binary", extractor)


if __name__ == "__main__":
    main()
