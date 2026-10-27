#!/usr/bin/env python3
"""
Easy-Switch relay for Logitech MX Keys + MX Master 3S (or any HID++ 2.0 pair).

Pressing Easy-Switch 1/2/3 on the keyboard switches the mouse AND the keyboard
to that channel. Run it on every computer the pair is paired with.

How it works (Logitech HID++ 2.0):
  * REPROG_CONTROLS_V4 (0x1B04) "diverts" the keyboard's Host Switch keys
    (CIDs 0xD1-0xD3), so a press is reported to this computer instead of being
    handled inside the keyboard.
  * On a press, CHANGE_HOST (0x1814) setCurrentHost is sent to the mouse,
    then to the keyboard. That's two ~20-byte writes, so the host side adds
    only a few ms. The rest is the devices reconnecting.
  * The keyboard clears diversion whenever it reconnects, so the relay
    re-applies it on reconnect and every couple of seconds. If the relay isn't
    running on a computer, the keys fall back to normal keyboard-only switching.
    Nothing is written permanently to the devices.

Works over Bluetooth, a Logi Bolt/Unifying receiver, or a mix of both.

    easyswitch-relay --probe     # show what it finds, then exit
    easyswitch-relay             # run the relay (normally started at login)

Log file:  Windows  %LOCALAPPDATA%\\EasySwitchRelay\\relay.log
           macOS    ~/Library/Logs/EasySwitchRelay/relay.log
"""
import argparse
import ctypes
import os
import queue
import re
import sys
import threading
import time
import traceback

import hid

__version__ = "1.0.0"

if sys.platform == "darwin":
    # hidapi on macOS opens devices exclusively ("seize") by default, which can
    # stop macOS itself getting typing/pointer input from a Bluetooth keyboard
    # or mouse while we hold it open. The compiled module exports the setting;
    # loading it again by path returns the same, already-loaded library.
    try:
        ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)
        _EXCLUSIVE_OFF = True
    except Exception as _e:
        _EXCLUSIVE_OFF = _e     # reported in the log at startup

LOGI_VID = 0x046D
SHORT, LONG = 0x10, 0x11
LONG_LEN = 20
SW_ID = 0x0A
BT_INDEX = 0xFF
RECEIVER_SLOTS = range(1, 7)

F_ROOT, F_NAME, F_CHANGE_HOST, F_REPROG = 0x0000, 0x0005, 0x1814, 0x1B04
HOST_KEYS = {0x00D1: 0, 0x00D2: 1, 0x00D3: 2}   # Host Switch CID -> 0-based channel
DIVERT = 0x03                                   # temporary divert + "divert valid"

REDIVERT_EVERY = 2.0      # s; catches Bluetooth reconnects (no notification for those)
RESCAN_EVERY = 3.0        # s; new HID nodes / devices not yet identified
PROBE_TIMEOUT = 0.3       # s

reports = queue.Queue()   # (link_key, report) from every reader thread
deferred = []             # reports pulled off the queue while waiting for a reply


# ------------------------------------------------------------- logging & housekeeping

if sys.platform == "win32":
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "EasySwitchRelay")
elif sys.platform == "darwin":
    DATA_DIR = os.path.expanduser("~/Library/Logs/EasySwitchRelay")
else:
    DATA_DIR = os.path.expanduser("~/.local/state/easyswitch-relay")
LOG_PATH = os.path.join(DATA_DIR, "relay.log")
LOG_MAX = 1_000_000           # bytes; one previous log is kept as relay.log.old
_log_file = None
_log_lock = threading.Lock()


def open_log():
    global _log_file
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
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
        if _log_file and _log_file.tell() > LOG_MAX:   # rotate while running
            try:
                _log_file.close()
                os.replace(LOG_PATH, LOG_PATH + ".old")
            except OSError:
                pass
            open_log()


def attach_parent_console():
    """The Windows build has no console of its own. When run from a terminal
    (e.g. --probe), print into that terminal instead."""
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


def macos_input_monitoring():
    """macOS needs Input Monitoring to open a keyboard's HID interface. Ask for
    it (shows the system prompt once) and say plainly if it's missing."""
    if sys.platform != "darwin":
        return
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
        iokit.IOHIDRequestAccess.restype = ctypes.c_bool
        LISTEN_EVENT, GRANTED = 1, 0
        if iokit.IOHIDCheckAccess(LISTEN_EVENT) != GRANTED:
            if not iokit.IOHIDRequestAccess(LISTEN_EVENT):
                log("Input Monitoring is not allowed yet. Turn on EasySwitch Relay in "
                    "System Settings > Privacy & Security > Input Monitoring, then it "
                    "will pick the devices up within a few seconds.")
    except Exception as e:
        log("could not check Input Monitoring permission:", e)


# --------------------------------------------------------------------------- links

class Link:
    """One HID++ endpoint: a receiver, or a Bluetooth device. On Windows one
    endpoint shows up as several HID collections (short/long reports); we open
    them all, read from all, and write long reports to the long one."""

    def __init__(self, key, infos):
        self.key = key
        self.is_receiver = infos[0]["usage_page"] == 0xFF00
        self.alive = True
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
        # A reader runs until its own read() fails (device removed). It never
        # stops on a timeout: on Windows a timed-out read is left pending in the
        # kernel, and closing a handle with I/O pending is a use-after-free in
        # hidapi (hid_close's CancelIo only cancels the calling thread's I/O and
        # doesn't wait). A failed read means that handle has no I/O outstanding.
        try:
            while True:
                r = d.read(64, timeout_ms=250)
                if r:
                    reports.put((self.key, r))
        except (OSError, IOError, ValueError):
            pass
        self.alive = False

    def shutdown(self):
        """Main thread only, so no write() is in flight on these handles.
        Closes each handle whose reader has finished (its read failed, so no
        I/O is pending). A handle whose reader is still running is left open
        on purpose: leaking it is safe, closing it is not."""
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
            return self.out.write(pkt) > 0
        except (OSError, IOError, ValueError):
            return False


def group_key(path):
    p = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
    return re.sub(r"&col[0-9a-f]+", "", p.lower())   # merge Windows "&Col01/&Col02"


def scan(links):
    groups = {}
    try:
        found = hid.enumerate(LOGI_VID, 0)
    except Exception as e:              # e.g. a device with a malformed name string
        log("enumerate failed:", e)
        return
    for info in found:
        up, u = info["usage_page"], info["usage"]
        if (up == 0xFF00 and u in (0x0001, 0x0002)) or up == 0xFF43:
            groups.setdefault(group_key(info["path"]), []).append(info)
    for key, infos in groups.items():
        if key in links and links[key].alive:
            continue
        try:
            links[key] = Link(key, infos)
            _open_failures.discard(key)
        except OSError as e:
            if key not in _open_failures:          # log once, retry quietly
                _open_failures.add(key)
                hint = (" (on a Mac this usually means Input Monitoring is off)"
                        if sys.platform == "darwin" else "")
                log("can't open", repr(infos[0].get("product_string")), "-", e, hint)


_open_failures = set()


# ------------------------------------------------------------------------ requests

def request(link, idx, feat, fn, params=(), timeout=0.5):
    """Send a HID++ 2.0 request and wait for its reply. Returns the payload or None."""
    fnb = (fn << 4) | SW_ID
    if not link.send(idx, feat, fn, params):
        return None
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            key, r = reports.get(timeout=left)
        except queue.Empty:
            return None
        if key == link.key and len(r) >= 5 and r[1] == idx:
            if r[0] == LONG and r[2] == feat and r[3] == fnb:
                return r[4:]
            if r[2] in (0xFF, 0x8F) and r[3] == feat and r[4] == fnb:
                return None          # HID++ 2.0 error / receiver says device absent
        deferred.append((key, r))


def get_feature(link, idx, fid, timeout=PROBE_TIMEOUT):
    r = request(link, idx, F_ROOT, 0, [fid >> 8, fid & 0xFF], timeout)
    return r[0] if r and r[0] else None


def get_name(link, idx, timeout=PROBE_TIMEOUT):
    f = get_feature(link, idx, F_NAME, timeout)
    r = f and request(link, idx, f, 0, timeout=timeout)
    if not r:
        return "?"
    n, out = r[0], []
    while len(out) < n:
        c = request(link, idx, f, 1, [len(out)], timeout)
        if not c:
            break
        out += c[: n - len(out)]
    return bytes(out).split(b"\0")[0].decode(errors="replace")


# ------------------------------------------------------------------------- devices

class Device:
    def __init__(self, link_key, idx, name, f_host, f_reprog, channel):
        self.link_key, self.idx, self.name = link_key, idx, name
        self.f_host, self.f_reprog = f_host, f_reprog
        self.channel = channel        # 0-based channel THIS computer occupies
        self.is_keyboard = False
        self.pressed = set()


def divert(dev, link, wait):
    ok = True
    for cid in HOST_KEYS:
        params = [cid >> 8, cid & 0xFF, DIVERT, 0, 0]
        if wait:
            ok &= request(link, dev.idx, dev.f_reprog, 3, params, PROBE_TIMEOUT) is not None
        else:
            link.send(dev.idx, dev.f_reprog, 3, params)
    return ok


def probe(link, idx):
    f_host = get_feature(link, idx, F_CHANGE_HOST)
    if not f_host:
        return None
    info = request(link, idx, f_host, 0, timeout=PROBE_TIMEOUT)   # getHostInfo
    if not info:
        return None
    dev = Device(link.key, idx, get_name(link, idx), f_host,
                 get_feature(link, idx, F_REPROG), info[1])
    if dev.f_reprog:
        dev.is_keyboard = divert(dev, link, wait=True)
    log(f"found {'keyboard' if dev.is_keyboard else 'device  '} {dev.name!r} "
        f"via {'receiver slot ' + str(idx) if link.is_receiver else 'bluetooth'}; "
        f"this computer is channel {info[1] + 1} of {info[0]}")
    return dev


def switch(ch, kb, links, devices):
    t = time.perf_counter()
    others = [d for d in devices.values() if d is not kb]
    for d in others:                       # mouse first, keyboard last
        link = links.get(d.link_key)
        if link:
            link.send(d.idx, d.f_host, 1, [ch])
    kb_link = links.get(kb.link_key)
    if kb_link:
        kb_link.send(kb.idx, kb.f_host, 1, [ch])
    log(f"-> channel {ch + 1}: {len(others)} other device(s) + keyboard "
        f"({(time.perf_counter() - t) * 1000:.1f} ms)")


def handle(key, r, links, devices):
    if len(r) < 4:
        return
    dev = devices.get((key, r[1]))
    if not (dev and dev.is_keyboard):
        return
    if r[0] == SHORT and r[2] == 0x41:     # receiver: keyboard (re)connected
        if key in links:
            divert(dev, links[key], wait=False)
        return
    if r[0] != LONG or r[2] != dev.f_reprog or r[3] != 0x00:
        return                             # not a divertedButtonsEvent
    pressed = {(r[i] << 8) | r[i + 1] for i in range(4, min(len(r) - 1, 12), 2)} - {0}
    new = (pressed - dev.pressed) & HOST_KEYS.keys()
    dev.pressed = pressed
    for cid in new:
        ch = HOST_KEYS[cid]
        if ch != dev.channel:              # pressing this computer's own key: no-op
            switch(ch, dev, links, devices)


# ---------------------------------------------------------------------------- main

def probe_verdict(devices):
    """Log a plain-language result for --probe. Returns the exit code."""
    kbs = [d for d in devices.values() if d.is_keyboard]
    if not devices:
        log("RESULT: no Logitech keyboard or mouse found. Are they connected to THIS "
            "computer right now?" + (" On a Mac, also check Input Monitoring." if
                                    sys.platform == "darwin" else ""))
        return 1
    if not kbs:
        log("RESULT: found devices, but no keyboard accepted the Easy-Switch takeover.")
        return 1
    chans = {d.channel for d in devices.values()}
    if len(chans) > 1:
        log("RESULT: this computer is on different channel numbers on the keyboard and "
            "the mouse. Re-pair so this computer uses the same number on both.")
        return 1
    log(f"RESULT: OK. This computer is channel {chans.pop() + 1} on all devices.")
    return 0


def run(probe_only=False):
    links, devices = {}, {}
    last_scan = last_divert = 0.0

    while True:
        try:
            now = time.monotonic()

            if now - last_scan > RESCAN_EVERY:
                last_scan = now
                for k in [k for k, l in links.items() if not l.alive]:
                    links.pop(k).shutdown()
                scan(links)
                for link in list(links.values()):
                    for idx in (RECEIVER_SLOTS if link.is_receiver else [BT_INDEX]):
                        if (link.key, idx) not in devices:
                            d = probe(link, idx)
                            if d:
                                devices[(link.key, idx)] = d
                if probe_only:
                    return probe_verdict(devices)

            if now - last_divert > REDIVERT_EVERY:
                last_divert = now
                for d in devices.values():
                    if d.is_keyboard and d.link_key in links:
                        divert(d, links[d.link_key], wait=False)

            if deferred:
                batch, deferred[:] = list(deferred), []
            else:
                try:
                    batch = [reports.get(timeout=0.25)]
                except queue.Empty:
                    batch = []
            for key, r in batch:
                handle(key, r, links, devices)

        except Exception:
            # Keep the relay alive through anything unexpected; state is kept.
            if probe_only:
                raise
            log("unexpected error (continuing):\n" + traceback.format_exc())
            time.sleep(1.0)


def main():
    ap = argparse.ArgumentParser(
        prog="easyswitch-relay",
        description="Make the keyboard's Easy-Switch keys switch the mouse too.")
    ap.add_argument("--probe", action="store_true",
                    help="check devices and permissions, print the result, and exit")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    args = ap.parse_args()

    if args.probe or args.version:
        attach_parent_console()
    if args.version:
        print(f"easyswitch-relay {__version__} (hidapi {getattr(hid, "version_str", lambda: "?")()})")
        return 0

    open_log()
    if not args.probe and not single_instance():
        log("another copy of the relay is already running; exiting")
        return 0
    log(f"easyswitch relay {__version__} starting ({sys.platform}, log: {LOG_PATH})")
    if sys.platform == "darwin" and _EXCLUSIVE_OFF is not True:
        log("WARNING: could not turn off exclusive device access:", _EXCLUSIVE_OFF,
            "- typing on this Mac may stop while the relay runs; quit it if so.")
    macos_input_monitoring()
    return run(probe_only=args.probe)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
