"""Settings shared by the relay and the settings window (config.json)."""
import base64
import json
import os
import re
import secrets
import socket
import sys
import time

from esr_hid import CONFIG_PATH, DATA_DIR

DEFAULTS = {
    # Network "follow": needs the same pairing code on every computer. Empty = off.
    "pairing_code": "",
    "follow_keyboard": True,     # keyboard moved to another computer -> bring the mouse
    "follow_mouse": False,       # mouse moved -> bring the keyboard
    "port": 47811,
    "extra_peers": [],           # addresses for networks that block broadcasts
    "computer_name": "",
    # Shortcut keys: channel ("1"/"2"/"3") -> {"cid": int, "label": str} or None
    "keys": {"1": None, "2": None, "3": None},
    "keys_updated": 0.0,
    "sync_keys": True,           # share shortcut keys with the other computers
    "channel_names": {"1": "", "2": "", "3": ""},
    # Updates
    "check_updates": True,       # check when the settings window opens
    "allow_remote_update": True, # let my other computers start an update here
}


def default_name():
    try:
        return socket.gethostname().split(".")[0][:40]
    except Exception:
        return "this computer"


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in DEFAULTS:
                    cfg[k] = v
    except (OSError, ValueError):
        pass
    if not cfg["computer_name"]:
        cfg["computer_name"] = default_name()
    for ch in ("1", "2", "3"):
        cfg["keys"].setdefault(ch, None)
        cfg["channel_names"].setdefault(ch, "")
    return cfg


def save(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)        # the pairing code is a secret
    os.replace(tmp, CONFIG_PATH)


def mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


def new_pairing_code():
    """80 random bits as 16 letters/digits, e.g. K3QX-7MPA-2ZRT-9HCW."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode()
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalize_code(code):
    return re.sub(r"[^A-Z2-7]", "", (code or "").upper())


def code_is_valid(code):
    return len(normalize_code(code)) == 16


def key_cids(cfg):
    """{cid: 0-based channel} for the configured shortcut keys."""
    out = {}
    for ch, k in (cfg.get("keys") or {}).items():
        if isinstance(k, dict) and isinstance(k.get("cid"), int) and ch in ("1", "2", "3"):
            out[k["cid"]] = int(ch) - 1
    return out


def stamp_keys(cfg):
    cfg["keys_updated"] = time.time()
