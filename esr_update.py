"""In-app updates from this project's GitHub releases.

Safety rules:
  * Only the GitHub repository baked in at build time (esr_build.REPO) is used;
    nothing from the network (including messages from your other computers)
    can change where updates come from.
  * Downloads use HTTPS only (the system's curl, which uses the operating
    system's certificate store), including any redirects.
  * The zip must match the SHA-256 in the release's .sha256 file and, when
    GitHub provides one, the digest GitHub reports for the asset.
  * Only a strictly newer version is installed.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import esr_hid as H

try:
    from esr_build import REPO          # written by the GitHub build
except ImportError:
    REPO = ""

REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class UpdateError(Exception):
    pass


def available():
    """'' if updates can work here, otherwise the reason they can't."""
    if not REPO or not REPO_RE.match(REPO):
        return "This copy was built locally, so it doesn't know where to get updates."
    if sys.platform == "darwin" and platform.machine() != "arm64":
        return "Updates are only published for Apple Silicon Macs."
    if not getattr(sys, "frozen", False):
        return "Updates apply to the installed app, not to the Python source."
    if not curl_path():
        return "curl wasn't found on this computer."
    return ""


def asset_name():
    return ("EasySwitchRelay-windows-x64.zip" if sys.platform == "win32"
            else "EasySwitchRelay-macos-arm64.zip")


def curl_path():
    for c in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe")
              if sys.platform == "win32" else "/usr/bin/curl", shutil.which("curl")):
        if c and os.path.exists(c):
            return c
    return None


def _curl(args, timeout=120):
    cmd = [curl_path(), "--fail", "--silent", "--show-error", "--location",
           "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
           "--max-time", str(timeout), "-A", f"EasySwitchRelay/{H.__version__}"] + args
    kw = {"capture_output": True}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000                     # CREATE_NO_WINDOW
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise UpdateError(f"download failed: {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout


def parse_version(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", str(v).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(remote, local=H.__version__):
    r, l = parse_version(remote), parse_version(local)
    return bool(r and l and r > l)


def latest():
    """{'version', 'published', 'notes', 'zip': {...}, 'sha': {...}} for the latest release."""
    why = available()
    if why:
        raise UpdateError(why)
    data = json.loads(_curl(["-H", "Accept: application/vnd.github+json",
                             f"https://api.github.com/repos/{REPO}/releases/latest"], 30))
    version = data.get("tag_name", "")
    if not parse_version(version):
        raise UpdateError(f"unexpected release name {version!r}")
    assets = {a.get("name"): a for a in data.get("assets", [])}
    name = asset_name()
    if name not in assets or name + ".sha256" not in assets:
        raise UpdateError("the latest release doesn't have a build for this computer yet "
                          "(it may still be building - try again in a few minutes)")
    prefix = f"https://github.com/{REPO}/releases/download/"
    for a in (assets[name], assets[name + ".sha256"]):
        if not str(a.get("browser_download_url", "")).startswith(prefix):
            raise UpdateError("release asset points outside this project's releases")
    return {"version": version.lstrip("v"), "published": data.get("published_at", ""),
            "notes": (data.get("body") or "")[:600], "zip": assets[name],
            "sha": assets[name + ".sha256"]}


def download(info, workdir):
    zpath = os.path.join(workdir, asset_name())
    _curl(["-o", zpath, info["zip"]["browser_download_url"]], 300)
    expected = _curl([info["sha"]["browser_download_url"]], 30).decode().split()[0].lower()
    h = hashlib.sha256()
    with open(zpath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or got != expected:
        raise UpdateError("the download doesn't match its published checksum; not installing")
    digest = str(info["zip"].get("digest") or "")
    if digest.startswith("sha256:") and digest[7:].lower() != got:
        raise UpdateError("the download doesn't match GitHub's checksum; not installing")
    H.log(f"update: downloaded {asset_name()} {info['version']} (sha256 {got[:16]}... verified)")
    return zpath


def install(zpath, workdir, unattended=False):
    """Unpack and hand over to the platform installer, which stops this app,
    replaces it, and starts the new version. Returns once the installer runs."""
    out = os.path.join(workdir, "unpacked")
    if sys.platform == "win32":
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():                     # refuse paths escaping the folder
                if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/"):
                    raise UpdateError("unexpected file name in the update")
            z.extractall(out)
        script = os.path.join(out, "install.ps1")
        if not os.path.exists(script):
            raise UpdateError("the update is missing its installer")
        args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                "-Update"] + (["-NoSettings"] if unattended else [])
        subprocess.Popen(args, creationflags=0x00000008 | 0x08000000,      # detached, no window
                         close_fds=True, cwd=out)
    else:
        subprocess.run(["/usr/bin/ditto", "-x", "-k", zpath, out], check=True)
        folders = [d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d))]
        script = os.path.join(out, folders[0], "install.sh") if folders else ""
        if not os.path.exists(script):
            raise UpdateError("the update is missing its installer")
        log = open(os.path.join(H.LOG_DIR, "update.log"), "a")
        subprocess.Popen(["/bin/bash", script, "--update"] + (["--quiet"] if unattended else []),
                         stdout=log, stderr=log, start_new_session=True, close_fds=True)
    H.log(f"update: installer started ({'unattended' if unattended else 'interactive'})")


def update_now(unattended=False, info=None):
    """Check, download, verify and start installing. Raises UpdateError."""
    info = info or latest()
    if not is_newer(info["version"]):
        raise UpdateError(f"already up to date ({H.__version__})")
    workdir = tempfile.mkdtemp(prefix="easyswitch-update-")
    zpath = download(info, workdir)
    install(zpath, workdir, unattended)
    return info["version"]
