"""Device layer: logging, file locations, and Logitech HID++ 2.0 transport.

Carried over from 1.0.x, including the fixes from the hidapi review:
  * on macOS, devices are opened non-exclusively so the OS keeps getting input;
  * a HID handle is only closed after its own read has failed (no I/O pending),
    and only from the main thread, so no write can be in flight.
"""
import ctypes
import os
import queue
import re
import sys
import threading
import time

import hid

__version__ = "1.2.1"

if sys.platform == "darwin":
    # hidapi on macOS opens devices exclusively ("seize") by default, which can
    # stop macOS itself getting typing/pointer input from a Bluetooth keyboard
    # or mouse while we hold it open. The compiled module exports the setting;
    # loading it again by path returns the same, already-loaded library.
    try:
        ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)
        EXCLUSIVE_OFF = True
    except Exception as _e:
        EXCLUSIVE_OFF = _e
else:
    EXCLUSIVE_OFF = True

LOGI_VID = 0x046D
SHORT, LONG = 0x10, 0x11
LONG_LEN = 20
SW_ID = 0x0A              # request tag; --probe/--diagnose/settings use other values
BT_INDEX = 0xFF
RECEIVER_SLOTS = range(1, 7)

F_ROOT, F_FEATURE_SET, F_NAME = 0x0000, 0x0001, 0x0005
F_CHANGE_HOST, F_REPROG, F_WIRELESS_STATUS = 0x1814, 0x1B04, 0x1D4B
DIVERT, UNDIVERT = 0x03, 0x02          # setCidReporting flags (divert bit + "valid" bit)
PROBE_TIMEOUT = 0.5

reports = queue.Queue()   # (link_key, report) from reader threads; ("__net__", msg) too
deferred = []             # reports pulled off the queue while waiting for a reply

# ------------------------------------------------------------------- files

if sys.platform == "win32":
    _base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "EasySwitchRelay")
    DATA_DIR = LOG_DIR = _base
elif sys.platform == "darwin":
    DATA_DIR = os.path.expanduser("~/Library/Application Support/EasySwitchRelay")
    LOG_DIR = os.path.expanduser("~/Library/Logs/EasySwitchRelay")
else:
    DATA_DIR = LOG_DIR = os.path.expanduser("~/.local/state/easyswitch-relay")
LOG_PATH = os.path.join(LOG_DIR, "relay.log")
STATUS_PATH = os.path.join(DATA_DIR, "status.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
CAPTURE_FLAG = os.path.join(DATA_DIR, "capturing")
LOGREQ_FLAG = os.path.join(DATA_DIR, "request-logs")      # ask other computers for logs
PEER_LOG_DIR = os.path.join(DATA_DIR, "peer-logs")
UPDATE_ALL_FLAG = os.path.join(DATA_DIR, "request-update-all")
LOG_MAX = 1_000_000

# ----------------------------------------------------------------- logging

_log_file = None
_log_lock = threading.Lock()
log_listeners = []        # callables(line) - used by the settings window


def open_log():
    global _log_file
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        _log_file = open(LOG_PATH, "a", encoding="utf-8")
    except OSError:
        _log_file = None


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    with _log_lock:
        for stream in (sys.stdout, _log_file):
            if stream is None:
                continue
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass
        if _log_file and _log_file.tell() > LOG_MAX:
            try:
                _log_file.close()
                os.replace(LOG_PATH, LOG_PATH + ".old")
            except OSError:
                pass
            open_log()
    for fn in list(log_listeners):
        try:
            fn(line)
        except Exception:
            pass


def attach_parent_console():
    """The Windows build has no console. When run from a terminal (e.g. --probe),
    print into that terminal instead."""
    if sys.platform == "win32" and sys.stdout is None:
        try:
            if ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stdout.write("\n")
        except Exception:
            pass


_lock_handle = None


def single_instance():
    """True if no other copy of the relay is running for this user."""
    global _lock_handle
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _lock_handle = open(os.path.join(DATA_DIR, "relay.lock"), "a+")
        if sys.platform == "win32":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def macos_input_monitoring(request_access=True):
    """macOS needs Input Monitoring to open a keyboard's HID interface.
    Returns True/False/None (granted / not / unknown)."""
    if sys.platform != "darwin":
        return True
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
        iokit.IOHIDRequestAccess.restype = ctypes.c_bool
        LISTEN_EVENT, GRANTED = 1, 0
        if iokit.IOHIDCheckAccess(LISTEN_EVENT) == GRANTED:
            return True
        if request_access and iokit.IOHIDRequestAccess(LISTEN_EVENT):
            return True
        log("Input Monitoring is not allowed yet. Turn on EasySwitch Relay in System "
            "Settings > Privacy & Security > Input Monitoring.")
        return False
    except Exception as e:
        log("could not check Input Monitoring permission:", e)
        return None

# ------------------------------------------------------------------- links


class Link:
    """One HID++ endpoint: a receiver, or a Bluetooth device. On Windows one
    endpoint shows up as several HID collections (short/long reports); we open
    them all, read from all, and write long reports to the long one."""

    def __init__(self, key, infos):
        self.key = key
        self.is_receiver = infos[0]["usage_page"] == 0xFF00
        self.product = infos[0].get("product_string") or ""
        self.alive = True
        self.write_err = ""           # last write error text, logged when it changes
        self.write_err_at = 0.0
        self.refused_at = 0.0         # last time the OS said "not permitted" (macOS Secure Input)
        self.handles = []
        long_handle = None
        seen = set()
        for info in infos:
            if info["path"] in seen:
                continue
            seen.add(info["path"])
            d = hid.device()
            try:
                d.open_path(info["path"])
            except (OSError, IOError):
                continue
            self.handles.append(d)
            if info["usage"] in (0x0002, 0x0202):
                long_handle = d
        if not self.handles:
            raise OSError("could not open any HID++ collection")
        self.out = long_handle or self.handles[0]
        self.threads = [threading.Thread(target=self._reader, args=(d,), daemon=True)
                        for d in self.handles]
        for t in self.threads:
            t.start()

    def _reader(self, d):
        # Runs until its own read() fails (device removed). Never stops on a
        # timeout: on Windows a timed-out read stays pending in the kernel and
        # closing a handle with I/O pending is a use-after-free in hidapi.
        try:
            while True:
                r = d.read(64, timeout_ms=250)
                if r:
                    reports.put((self.key, r))
        except (OSError, IOError, ValueError):
            pass
        self.alive = False

    def shutdown(self):
        """Main thread only. Closes each handle whose reader has finished; a
        handle whose reader still runs is deliberately left open (safe)."""
        for t, d in zip(self.threads, self.handles):
            t.join(timeout=1.0)
            if not t.is_alive():
                try:
                    d.close()
                except Exception:
                    pass

    def send(self, idx, feat, fn, params=()):
        pkt = [LONG, idx, feat, (fn << 4) | SW_ID, *params]
        pkt += [0] * (LONG_LEN - len(pkt))
        try:
            ok = self.out.write(pkt) > 0
        except (OSError, IOError, ValueError) as e:
            ok, err = False, str(e)
        else:
            err = ""
        if not ok:
            self._note_write_error(err)
        return ok

    def refused(self, within=10.0):
        """True if the OS recently refused writes outright (on macOS: another app
        holds Secure Input, which blocks writes to keyboards)."""
        return self.refused_at and time.monotonic() - self.refused_at < within

    def _note_write_error(self, err):
        """Log why writes fail (the operating system's own error text), once per
        distinct error and at most once a minute: this is what tells apart
        'device not connected', 'another app has it' and 'not permitted'."""
        try:
            err = err or (self.out.error() or "")
        except Exception:
            pass
        err = (err or "unknown error").strip()[:200]
        now = time.monotonic()
        if "not permitted" in err.lower() or "e00002e2" in err.lower():
            self.refused_at = now
        if err != self.write_err or now - self.write_err_at > 60:
            self.write_err, self.write_err_at = err, now
            log(f"write to {self.product or 'device'} failed: {err}")


def group_key(path):
    p = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
    return re.sub(r"&col[0-9a-f]+", "", p.lower())   # merge Windows "&Col01/&Col02"


_open_failures = set()


def scan(links):
    """Open any Logitech HID++ endpoints not already open. Returns new keys."""
    new = []
    groups = {}
    try:
        found = hid.enumerate(LOGI_VID, 0)
    except Exception as e:              # e.g. a device with a malformed name string
        log("enumerate failed:", e)
        return new
    for info in found:
        up, u = info["usage_page"], info["usage"]
        if (up == 0xFF00 and u in (0x0001, 0x0002)) or up == 0xFF43:
            groups.setdefault(group_key(info["path"]), []).append(info)
    for key, infos in groups.items():
        if key in links and links[key].alive:
            continue
        try:
            links[key] = Link(key, infos)
            new.append(key)
            _open_failures.discard(key)
        except OSError as e:
            if key not in _open_failures:
                _open_failures.add(key)
                hint = (" (on a Mac this usually means Input Monitoring is off)"
                        if sys.platform == "darwin" else "")
                log("can't open", repr(infos[0].get("product_string")), "-", e, hint)
    return new

# ---------------------------------------------------------------- requests


HIDPP_ERRORS = {1: "unknown", 2: "invalid argument", 3: "out of range", 4: "hardware error",
                5: "logitech internal", 6: "invalid feature index", 7: "invalid function",
                8: "busy", 9: "unsupported"}
last_error = "none"
NO_REPLY = "no reply (device asleep or connected elsewhere?)"


def request(link, idx, feat, fn, params=(), timeout=PROBE_TIMEOUT):
    """Send a HID++ 2.0 request and wait for its reply. Returns the payload or
    None; on None, last_error says why."""
    global last_error
    fnb = (fn << 4) | SW_ID
    if not link.send(idx, feat, fn, params):
        last_error = f"write failed ({link.write_err})"
        return None
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            last_error = NO_REPLY
            return None
        try:
            key, r = reports.get(timeout=left)
        except queue.Empty:
            last_error = NO_REPLY
            return None
        if key == link.key and isinstance(r, list) and len(r) >= 5 and r[1] == idx:
            if r[0] == LONG and r[2] == feat and r[3] == fnb:
                return r[4:]
            if r[2] in (0xFF, 0x8F) and r[3] == feat and r[4] == fnb:
                code = r[5] if len(r) > 5 else -1
                kind = "HID++ 2.0" if r[2] == 0xFF else "receiver"
                last_error = f"{kind} error {code} ({HIDPP_ERRORS.get(code, '?')})"
                return None
        deferred.append((key, r))


def get_feature(link, idx, fid, timeout=PROBE_TIMEOUT):
    r = request(link, idx, F_ROOT, 0, [fid >> 8, fid & 0xFF], timeout)
    return r[0] if r and r[0] else None


def get_name(link, idx, f_name, timeout=PROBE_TIMEOUT):
    """The device's name, or None unless it was read completely (a name read
    halfway, e.g. when the OS starts refusing writes mid-read, is useless)."""
    r = request(link, idx, f_name, 0, timeout=timeout)
    if not r or not 0 < r[0] <= 64:
        return None
    n, out = r[0], []
    while len(out) < n:
        c = request(link, idx, f_name, 1, [len(out)], timeout)
        if not c:
            return None
        out += c[: n - len(out)]
    name = bytes(out).split(b"\0")[0].decode(errors="replace").strip()
    return name or None


DEVICE_TYPES = {0: "keyboard", 1: "remote control", 2: "numpad", 3: "mouse",
                4: "trackpad", 5: "trackball", 6: "presenter", 7: "receiver"}


def get_type(link, idx, f_name, timeout=PROBE_TIMEOUT):
    r = request(link, idx, f_name, 2, timeout=timeout)
    return DEVICE_TYPES.get(r[0], "other") if r else "other"


def list_controls(link, idx, f_reprog, timeout=PROBE_TIMEOUT):
    """[(cid, task, flags)] from REPROG_CONTROLS_V4."""
    out = []
    n = (request(link, idx, f_reprog, 0, timeout=timeout) or [0])[0]
    for i in range(n):
        r = request(link, idx, f_reprog, 1, [i], timeout)
        if r:
            out.append(((r[0] << 8) | r[1], (r[2] << 8) | r[3], r[4]))
    return out


DIVERTABLE = 0x20

# Friendly names for keys, by Logitech "task" ID (what the key does by default).
TASK_NAMES = {0x0001: "Volume up", 0x0002: "Volume down", 0x0003: "Mute",
              0x0004: "Play/Pause", 0x0005: "Next track", 0x0006: "Previous track"}
CID_NAMES = {0x00D1: "Easy-Switch 1", 0x00D2: "Easy-Switch 2", 0x00D3: "Easy-Switch 3"}


def key_label(cid, task=None):
    if cid in CID_NAMES:
        return CID_NAMES[cid]
    if task in TASK_NAMES:
        return f"{TASK_NAMES[task]} key"
    return f"Key {cid:#06x}"


def find_keyboard(links):
    """(link, idx, f_reprog, name) for a keyboard connected here, or None."""
    scan(links)
    for link in list(links.values()):
        if not link.alive:
            continue
        for idx in (RECEIVER_SLOTS if link.is_receiver else [BT_INDEX]):
            f_name = get_feature(link, idx, F_NAME)
            if not f_name or get_type(link, idx, f_name) != "keyboard":
                continue
            f_reprog = get_feature(link, idx, F_REPROG)
            if f_reprog:
                return link, idx, f_reprog, get_name(link, idx, f_name) or "keyboard"
    return None


def take_keys(link, idx, f_reprog, cids, timeout=1.0):
    """Ask the keyboard to report these keys to us. Returns (accepted, refused)
    where refused maps cid -> reason. Every request is confirmed."""
    ok, bad = [], {}
    for c in cids:
        r = request(link, idx, f_reprog, 3, [c >> 8, c & 0xFF, DIVERT, 0, 0], timeout)
        if r is None:
            r = request(link, idx, f_reprog, 3, [c >> 8, c & 0xFF, DIVERT, 0, 0], timeout)
        if r is None:
            bad[c] = last_error
        else:
            ok.append(c)
    return ok, bad


def release_keys(link, idx, f_reprog, cids):
    for c in cids:
        link.send(idx, f_reprog, 3, [c >> 8, c & 0xFF, UNDIVERT, 0, 0])


def describe_report(r):
    return " ".join(f"{b:02x}" for b in r[:12])


def log_tail(n=80):
    """The last n lines of this computer's relay log."""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n")[:200] for ln in f.readlines()[-n:]]
    except OSError:
        return []


def macos_secure_input():
    """(pid, app name) of the process holding macOS Secure Event Input, or None.
    While any app holds it, macOS refuses writes to keyboards."""
    if sys.platform != "darwin":
        return None
    import subprocess
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-l", "-w", "0", "-d", "4"],
                             capture_output=True, timeout=10).stdout.decode(errors="replace")
        m = re.search(r'"kCGSSessionSecureInputPID"\s*=\s*(\d+)', out)
        if not m:
            return None
        pid = int(m.group(1))
        name = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], capture_output=True,
                              timeout=5).stdout.decode(errors="replace").strip()
        return pid, os.path.basename(name) or f"process {pid}"
    except Exception:
        return None
