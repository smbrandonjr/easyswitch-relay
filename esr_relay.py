"""The relay: watches the keyboard and mouse, handles shortcut keys, and keeps
the two devices on the same computer.

Two ways a switch happens:
  * Shortcut keys (and the Easy-Switch keys, on keyboards that allow it): the
    key press is reported here, and this computer moves its own devices
    straight away; a device that isn't here is moved by whichever computer
    has it, via a signed network message.
  * Follow (network): when the keyboard (or mouse) connects to a computer,
    that computer announces it, and the computer holding the other device
    sends it to the same channel.
"""
import json
import os
import queue
import sys
import time
import traceback

import esr_hid as H
import esr_config as C

HOST_KEYS = {0x00D1: 0, 0x00D2: 1, 0x00D3: 2}   # Easy-Switch CIDs -> 0-based channel

PING_TAG = 0xA5
PING_PRESENT_EVERY = 1.0      # s; devices that are here
PING_ABSENT_EVERY = 1.0       # s; known devices that aren't here (backs off if slow)
PING_UNKNOWN_EVERY = 3.0      # s; endpoints/slots we haven't identified yet
ABSENT_AFTER = 3.5            # s without any reply -> device has left
REDIVERT_EVERY = 2.0
RESCAN_EVERY = 1.0 if sys.platform == "darwin" else 2.0
STATUS_EVERY = 2.0
HELLO_EVERY = 10.0
HERE_DEBOUNCE = 2.0
CAPTURE_FRESH = 5.0           # s; the settings window is capturing a key
PERM_CHECK_EVERY = 30.0       # s; macOS Input Monitoring status
REOPEN_FIRST = 8.0            # s; macOS: reopen a quiet device's connection after this...
REOPEN_MAX = 300.0            # ...backing off to at most every 5 minutes


class Device:
    def __init__(self, name):
        self.name = name
        self.type = "other"
        self.link_key = None
        self.idx = None
        self.f_host = self.f_reprog = self.f_wds = None
        self.channel = None          # 0-based channel THIS computer occupies
        self.nb_hosts = 3
        self.controls = []           # keyboards: [(cid, task, flags)]
        self.present = False
        self.last_reply = 0.0
        self.last_seen_wall = 0.0
        self.reopen_wait = REOPEN_FIRST
        self.next_reopen = 0.0
        self.next_ping = 0.0
        self.ping_every = PING_ABSENT_EVERY
        self.pressed = set()
        self.diverted = set()
        self.refused = {}            # cid -> reason
        self.last_here = 0.0

    def divertable(self):
        return {c for c, _t, f in self.controls if f & H.DIVERTABLE}

    def to_status(self):
        return {"name": self.name, "type": self.type, "present": self.present,
                "last_seen": self.last_seen_wall,
                "channel": None if self.channel is None else self.channel + 1,
                "hosts": self.nb_hosts,
                "easy_switch_divertable": bool(set(HOST_KEYS) & self.divertable()),
                "diverted": sorted(self.diverted),
                "refused": {f"{c:#06x}": r for c, r in self.refused.items()},
                "controls": [[c, t, f] for c, t, f in self.controls]}


class Relay:
    def __init__(self, probe_only=False):
        self.probe_only = probe_only
        self.links = {}
        self.devices = {}            # name -> Device
        self.by_addr = {}            # (link_key, idx) -> name
        self.unknown_next = {}       # (link_key, idx) -> next ping time
        self.cfg = C.load()
        self.cfg_mtime = C.mtime()
        self.net = None
        self.net_sig = None
        self.last_switch = ""
        self.hello_sent = False
        self.t = {"scan": 0.0, "divert": 0.0, "status": 0.0, "hello": 0.0, "cfg": 0.0,
                  "perm": -PERM_CHECK_EVERY}
        self.input_monitoring = None          # macOS: does THIS relay have the permission?
        self.retired = []                     # macOS: stale connections kept open, unused
        if not probe_only:
            self.restart_net()

    # ---------------------------------------------------------------- network
    def restart_net(self):
        c = self.cfg
        sig = (C.normalize_code(c["pairing_code"]), int(c["port"]), c["computer_name"],
               tuple(c.get("extra_peers") or []))
        if sig == self.net_sig:
            return
        if self.net:
            self.net.stop()
            self.net = None
        self.net_sig = sig
        if C.code_is_valid(c["pairing_code"]):
            from esr_net import Net
            self.net = Net(c["pairing_code"], c["port"], c["computer_name"],
                           c.get("extra_peers") or [])
            if not self.net.start():
                pass   # error is reported in status; keep the object for that
            self.t["hello"] = 0.0
            self.hello_sent = False
        else:
            H.log("network: off (no pairing code set)")

    def net_send(self, mtype, **fields):
        if self.net and self.net.sock:
            self.net.send(mtype, **fields)

    # ---------------------------------------------------------------- devices
    def probe(self, link, idx):
        """Identify the device at (link, idx). Returns a Device or None."""
        f_host = H.get_feature(link, idx, H.F_CHANGE_HOST)
        if not f_host:
            return None
        info = H.request(link, idx, f_host, 0)
        if not info:
            return None
        f_name = H.get_feature(link, idx, H.F_NAME)
        name = H.get_name(link, idx, f_name) if f_name else f"device {idx:#04x}"
        dev = self.devices.get(name) or Device(name)
        dev.type = H.get_type(link, idx, f_name) if f_name else "other"
        dev.link_key, dev.idx, dev.f_host = link.key, idx, f_host
        dev.nb_hosts, dev.channel = info[0], info[1]
        dev.f_reprog = H.get_feature(link, idx, H.F_REPROG)
        dev.f_wds = H.get_feature(link, idx, H.F_WIRELESS_STATUS)
        if dev.type == "keyboard" and dev.f_reprog and not dev.controls:
            dev.controls = H.list_controls(link, idx, dev.f_reprog)
        new = name not in self.devices
        self.devices[name] = dev
        self.by_addr[(link.key, idx)] = name
        self.unknown_next.pop((link.key, idx), None)
        if new:
            H.log(f"found {dev.type} {name!r} via "
                  f"{'receiver slot ' + str(idx) if link.is_receiver else 'bluetooth'}; "
                  f"this computer is channel {dev.channel + 1} of {dev.nb_hosts}")
            if dev.type == "keyboard":
                es = "yes" if set(HOST_KEYS) <= dev.divertable() else \
                    "no (this keyboard's firmware doesn't allow it)"
                H.log(f"  Easy-Switch keys can be taken over: {es}")
        self.mark_alive(dev, reconnect=True)
        return dev

    def mark_alive(self, dev, reconnect=False):
        was = dev.present
        dev.present = True
        dev.last_reply = time.monotonic()
        dev.last_seen_wall = time.time()
        dev.reopen_wait = REOPEN_FIRST
        dev.ping_every = PING_PRESENT_EVERY
        if not was or reconnect:
            self.on_arrival(dev, was)

    def on_arrival(self, dev, was_present):
        if not was_present:
            H.log(f"{dev.name} is connected here (channel {dev.channel + 1})")
        if dev.type == "keyboard":
            self.apply_divert(dev, report=True)
        now = time.monotonic()
        if dev.type in ("keyboard", "mouse") and now - dev.last_here > HERE_DEBOUNCE:
            dev.last_here = now
            self.net_send("here", dev=dev.type, ch=dev.channel)
        self.t["status"] = 0.0
        self.t["hello"] = min(self.t["hello"], now - HELLO_EVERY + 1.0)

    def check_presence(self):
        now = time.monotonic()
        for dev in self.devices.values():
            link = self.links.get(dev.link_key)
            gone = link is None or not link.alive or now - dev.last_reply > ABSENT_AFTER
            if dev.present and gone:
                dev.present = False
                dev.pressed.clear()
                dev.diverted.clear()
                dev.ping_every = PING_ABSENT_EVERY
                H.log(f"{dev.name} left this computer")
                self.t["status"] = 0.0

    def ping_all(self):
        now = time.monotonic()
        for dev in self.devices.values():
            link = self.links.get(dev.link_key)
            if not link or not link.alive or now < dev.next_ping:
                continue
            t0 = time.monotonic()
            link.send(dev.idx, H.F_ROOT, 1, [0, 0, PING_TAG])
            slow = time.monotonic() - t0 > 0.15
            if not dev.present and slow:
                dev.ping_every = 5.0        # writes to an absent device block here; back off
            dev.next_ping = now + dev.ping_every
        for key, link in list(self.links.items()):
            if not link.alive:
                continue
            for idx in (H.RECEIVER_SLOTS if link.is_receiver else [H.BT_INDEX]):
                k = (key, idx)
                if k in self.by_addr or now < self.unknown_next.get(k, 0):
                    continue
                link.send(idx, H.F_ROOT, 1, [0, 0, PING_TAG])
                self.unknown_next[k] = now + PING_UNKNOWN_EVERY

    def _forget_link(self, k):
        for a in [a for a in self.by_addr if a[0] == k]:
            self.by_addr.pop(a)
        for a in [a for a in self.unknown_next if a[0] == k]:
            self.unknown_next.pop(a)

    def rescan(self):
        for k in [k for k, l in self.links.items() if not l.alive]:
            link = self.links.pop(k)
            H.log(f"connection closed: {link.product or 'device'} (it went away)")
            link.shutdown()
            self._forget_link(k)
        if sys.platform == "darwin":
            self.reopen_quiet()
        for key in H.scan(self.links):
            link = self.links[key]
            H.log(f"connection opened: {link.product or 'device'} "
                  f"({'receiver' if link.is_receiver else 'bluetooth'})")
            found = False
            for idx in (H.RECEIVER_SLOTS if link.is_receiver else [H.BT_INDEX]):
                found = bool(self.probe(link, idx)) or found
            if not found and not link.is_receiver:
                H.log(f"  {link.product or 'device'} didn't answer yet: {H.last_error}")

    def reopen_quiet(self):
        """macOS: a Bluetooth device that comes back can keep the old, now
        unresponsive connection. If a known device has been quiet for a while
        but its connection still looks open, open a fresh one (with back-off).
        The old one is kept open but unused: closing it isn't safe while its
        reader thread is blocked in it."""
        now = time.monotonic()
        for dev in self.devices.values():
            link = self.links.get(dev.link_key)
            if not link or not link.alive or dev.present or now < dev.next_reopen:
                continue
            if now - dev.last_reply < REOPEN_FIRST:
                continue
            dev.next_reopen = now + dev.reopen_wait
            dev.reopen_wait = min(dev.reopen_wait * 2, REOPEN_MAX)
            self.retired.append(self.links.pop(dev.link_key))
            self._forget_link(dev.link_key)
            H.log(f"{dev.name}: no answer on its connection; opening a fresh one")

    # ------------------------------------------------------------------- keys
    def wanted_keys(self, dev):
        """{cid: channel} this keyboard should report to us."""
        want = {}
        ok = dev.divertable()
        for cid, ch in HOST_KEYS.items():
            if cid in ok and ch < dev.nb_hosts:
                want[cid] = ch
        for cid, ch in C.key_cids(self.cfg).items():
            if cid in ok and ch < dev.nb_hosts:
                want[cid] = ch
        return want

    def apply_divert(self, dev, report=False):
        link = self.links.get(dev.link_key)
        if not link or not dev.f_reprog:
            return
        want = self.wanted_keys(dev)
        for cid in sorted(dev.diverted - set(want)):
            link.send(dev.idx, dev.f_reprog, 3, [cid >> 8, cid & 0xFF, H.UNDIVERT, 0, 0])
            dev.diverted.discard(cid)
        for cid in want:
            if report and cid not in dev.diverted:
                ok = H.request(link, dev.idx, dev.f_reprog, 3,
                               [cid >> 8, cid & 0xFF, H.DIVERT, 0, 0]) is not None
                if ok:
                    dev.diverted.add(cid)
                    dev.refused.pop(cid, None)
                elif cid not in dev.refused or dev.refused[cid] != H.last_error:
                    dev.refused[cid] = H.last_error
                    H.log(f"  {dev.name} refused key {cid:#06x}: {H.last_error}")
            else:
                link.send(dev.idx, dev.f_reprog, 3, [cid >> 8, cid & 0xFF, H.DIVERT, 0, 0])
                dev.diverted.add(cid)

    def task_of(self, dev, cid):
        return next((t for c, t, _f in dev.controls if c == cid), None)

    def ch_label(self, ch):
        name = (self.cfg.get("channel_names") or {}).get(str(ch + 1), "")
        return f"channel {ch + 1}" + (f" ({name})" if name else "")

    def capturing(self):
        try:
            return time.time() - os.path.getmtime(H.CAPTURE_FLAG) < CAPTURE_FRESH
        except OSError:
            return False

    def on_keys(self, dev, r):
        pressed = {(r[i] << 8) | r[i + 1] for i in range(4, min(len(r) - 1, 12), 2)} - {0}
        new = pressed - dev.pressed
        dev.pressed = pressed
        if not new or self.capturing():
            return
        want = self.wanted_keys(dev)
        for cid in new:
            if cid in want:
                self.switch_all(want[cid], f"{H.key_label(cid, self.task_of(dev, cid))} pressed")

    # -------------------------------------------------------------- switching
    def set_host(self, dev, ch):
        link = self.links.get(dev.link_key)
        return bool(link) and link.send(dev.idx, dev.f_host, 1, [ch])

    def switch_all(self, ch, why):
        here = [d for d in self.devices.values() if d.present]
        order = sorted(here, key=lambda d: d.type == "keyboard")    # keyboard last
        moved = []
        for d in order:
            if d.channel != ch and self.set_host(d, ch):
                moved.append(d.name)
        missing = {"keyboard", "mouse"} - {d.type for d in here}
        for t in sorted(missing):
            self.net_send("goto", dev=t, ch=ch)
        asked = (f"; asked your other computers to send the {' and '.join(sorted(missing))}"
                 if missing and self.net else "")
        self.last_switch = (f"{time.strftime('%H:%M:%S')} {why}: switched to "
                            f"{self.ch_label(ch)} (moved from here: "
                            f"{', '.join(moved) or 'nothing'}{asked})")
        H.log("->", self.last_switch)
        self.t["status"] = 0.0

    def move_local(self, dtype, ch, why):
        devs = [d for d in self.devices.values() if d.type == dtype and d.present]
        if not devs:
            self.rescan()                      # it may have just arrived unseen
            devs = [d for d in self.devices.values() if d.type == dtype and d.present]
        for d in devs:
            if d.channel == ch:
                continue                       # it's already on that computer (this one)
            if self.set_host(d, ch):
                self.last_switch = (f"{time.strftime('%H:%M:%S')} {why}: sent "
                                    f"{d.name} to {self.ch_label(ch)}")
                H.log("->", self.last_switch)
                self.t["status"] = 0.0

    # ---------------------------------------------------------------- reports
    def handle(self, key, r):
        if key == "__net__":
            return self.handle_net(r)
        if not isinstance(r, list) or len(r) < 4:
            return
        addr = (key, r[1])
        dev = self.devices.get(self.by_addr.get(addr, ""))
        if r[0] == H.SHORT and r[2] == 0x41:                  # receiver: link up/down
            if dev and not (len(r) > 4 and r[4] & 0x40):
                self.mark_alive(dev, reconnect=True)
            elif not dev:
                self.unknown_next[addr] = 0.0
            return
        if dev is None:
            # A reply from something we haven't identified: it's awake, so look now.
            if r[0] == H.LONG and r[2] == H.F_ROOT and r[3] == (0x10 | H.SW_ID):
                link = self.links.get(key)
                if link:
                    self.probe(link, r[1])
            return
        if r[2] == 0x8F:                    # receiver says this slot is empty/offline
            return
        # WIRELESS_DEVICE_STATUS event (function 0, tag 0): the device just (re)connected.
        reconnect = bool(dev.f_wds) and r[0] == H.LONG and r[2] == dev.f_wds and r[3] == 0x00
        self.mark_alive(dev, reconnect=reconnect)
        if dev.type == "keyboard" and dev.f_reprog and r[0] == H.LONG and \
                r[2] == dev.f_reprog and r[3] == 0x00:
            self.on_keys(dev, r)

    def handle_net(self, msg):
        t = msg.get("t")
        who = msg.get("name", "?")
        try:
            ch = int(msg.get("ch", -1))
        except (TypeError, ValueError):
            ch = -1
        dtype = msg.get("dev")
        if t == "here" and 0 <= ch < 6:
            if dtype == "keyboard" and self.cfg["follow_keyboard"]:
                self.move_local("mouse", ch, f"keyboard went to {who}")
            elif dtype == "mouse" and self.cfg["follow_mouse"]:
                self.move_local("keyboard", ch, f"mouse went to {who}")
        elif t == "goto" and 0 <= ch < 6 and dtype in ("keyboard", "mouse"):
            self.move_local(dtype, ch, f"shortcut key on {who}")
        elif t == "keys" and self.cfg["sync_keys"]:
            self.adopt_keys(msg, who)
        elif t == "hello" and msg.get("first") and self.cfg["sync_keys"]:
            self.share_keys()
        elif t == "logreq" and isinstance(msg.get("id"), str):
            self.send_logs(msg["id"])
        elif t == "logtail":
            self.save_peer_log(msg, who)
        elif t == "update":
            self.remote_update(who)

    def check_update_request(self):
        """The settings window asks the other computers to update by creating a
        request file. They each fetch and verify the update themselves."""
        if not os.path.exists(H.UPDATE_ALL_FLAG):
            return
        try:
            os.remove(H.UPDATE_ALL_FLAG)
        except OSError:
            return
        if self.net and self.net.sock:
            H.log("asking the other computers to update")
            self.net_send("update")

    def remote_update(self, who):
        """Another of my computers asked for an update. Only ever installs the
        latest release from this app's own GitHub project, after verifying it."""
        if not self.cfg.get("allow_remote_update", True):
            H.log(f"update requested by {who}, but remote updates are turned off here")
            return
        if getattr(self, "_updating", False):
            return
        self._updating = True

        def work():
            try:
                import esr_update as U
                info = U.latest()
                if not U.is_newer(info["version"]):
                    H.log(f"update requested by {who}: already up to date ({H.__version__})")
                    return
                H.log(f"update requested by {who}: installing {info['version']}")
                U.update_now(unattended=True, info=info)
            except Exception as e:
                H.log(f"update requested by {who} failed: {e}")
            finally:
                self._updating = False
        import threading
        threading.Thread(target=work, daemon=True).start()

    def check_log_request(self):
        """The settings window (or support) asks for the other computers' logs by
        creating a request file; replies are saved in peer-logs/."""
        if not os.path.exists(H.LOGREQ_FLAG):
            return
        try:
            os.remove(H.LOGREQ_FLAG)
        except OSError:
            return
        if self.net and self.net.sock:
            H.log("asking the other computers for their recent logs")
            self.net_send("logreq")

    def send_logs(self, to):
        lines = H.log_tail(80)
        chunks = [lines[i:i + 8] for i in range(0, len(lines), 8)] or [[]]
        status = {"version": H.__version__, "platform": sys.platform,
                  "input_monitoring": self.input_monitoring,
                  "devices": [[d.name, d.type, d.present, d.channel]
                              for d in self.devices.values()]}
        for i, c in enumerate(chunks):
            self.net_send("logtail", to=to, seq=i, total=len(chunks), lines=c,
                          status=status if i == 0 else None)
            time.sleep(0.02)

    def save_peer_log(self, msg, who):
        if not self.net or msg.get("to") != self.net.iid:
            return
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in who)[:40] or "peer"
        os.makedirs(H.PEER_LOG_DIR, exist_ok=True)
        path = os.path.join(H.PEER_LOG_DIR, f"{safe}.txt")
        lines = [str(x)[:200] for x in (msg.get("lines") or [])]
        mode = "w" if msg.get("seq") == 0 else "a"
        with open(path, mode, encoding="utf-8") as f:
            if mode == "w":
                f.write(f"# log from {who}, received {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# status: {json.dumps(msg.get('status'))}\n")
            f.write("\n".join(lines) + ("\n" if lines else ""))

    def adopt_keys(self, msg, who):
        keys, upd = msg.get("keys"), msg.get("updated", 0)
        if not isinstance(keys, dict) or not isinstance(upd, (int, float)):
            return
        if upd <= self.cfg.get("keys_updated", 0):
            return
        clean = {}
        for ch in ("1", "2", "3"):
            k = keys.get(ch)
            clean[ch] = ({"cid": int(k["cid"]), "label": str(k.get("label", ""))[:40]}
                         if isinstance(k, dict) and isinstance(k.get("cid"), int) else None)
        self.cfg["keys"], self.cfg["keys_updated"] = clean, float(upd)
        C.save(self.cfg)
        self.cfg_mtime = C.mtime()
        H.log(f"shortcut keys updated from {who}")
        for d in self.devices.values():
            if d.type == "keyboard" and d.present:
                self.apply_divert(d)

    def share_keys(self):
        if self.cfg.get("keys_updated", 0) > 0:
            self.net_send("keys", keys=self.cfg["keys"], updated=self.cfg["keys_updated"])

    # ----------------------------------------------------------------- config
    def reload_config(self):
        m = C.mtime()
        if m == self.cfg_mtime:
            return
        old = self.cfg
        self.cfg, self.cfg_mtime = C.load(), m
        H.log("settings changed; applying")
        self.restart_net()
        for d in self.devices.values():
            if d.type == "keyboard" and d.present:
                self.apply_divert(d, report=True)
        if self.cfg["keys"] != old["keys"] and self.cfg["sync_keys"]:
            self.share_keys()

    # ----------------------------------------------------------------- status
    def write_status(self):
        st = {"pid": os.getpid(), "version": H.__version__, "updated": time.time(),
              "computer": self.cfg["computer_name"],
              "input_monitoring": self.input_monitoring,
              "devices": [d.to_status() for d in self.devices.values()],
              "network": {"enabled": bool(self.net),
                          "listening": bool(self.net and self.net.sock),
                          "error": self.net.error if self.net else "",
                          "port": self.cfg["port"],
                          "peers": self.net.peer_list() if self.net else []},
              "last_switch": self.last_switch}
        try:
            os.makedirs(H.DATA_DIR, exist_ok=True)
            tmp = H.STATUS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, H.STATUS_PATH)
        except OSError:
            pass

    def hello(self, first=False):
        devs = [{"type": d.type, "name": d.name, "ch": d.channel}
                for d in self.devices.values() if d.present]
        self.net_send("hello", devices=devs, first=first)

    # ------------------------------------------------------------------- loop
    def tick(self):
        now = time.monotonic()
        if now - self.t["scan"] > RESCAN_EVERY:
            self.t["scan"] = now
            self.rescan()
        self.ping_all()
        self.check_presence()
        if now - self.t["divert"] > REDIVERT_EVERY:
            self.t["divert"] = now
            for d in self.devices.values():
                if d.type == "keyboard" and d.present:
                    self.apply_divert(d)
        if not self.probe_only:
            if now - self.t["cfg"] > 1.0:
                self.t["cfg"] = now
                self.reload_config()
                self.check_log_request()
                self.check_update_request()
            if sys.platform == "darwin" and now - self.t["perm"] > PERM_CHECK_EVERY:
                self.t["perm"] = now
                im = H.macos_input_monitoring(request_access=False)
                if im != self.input_monitoring:
                    H.log(f"Input Monitoring for the relay: "
                          f"{ {True: 'allowed', False: 'NOT allowed', None: 'unknown'}[im] }")
                    self.input_monitoring = im
                    self.t["status"] = 0.0
            if now - self.t["hello"] > HELLO_EVERY:
                first = not self.hello_sent
                self.hello_sent = True
                self.t["hello"] = now
                self.hello(first=first)
                if first and self.cfg["sync_keys"]:
                    self.share_keys()
            if now - self.t["status"] > STATUS_EVERY:
                self.t["status"] = now
                self.write_status()
        if H.deferred:
            batch, H.deferred[:] = list(H.deferred), []
        else:
            try:
                batch = [H.reports.get(timeout=0.2)]
            except queue.Empty:
                batch = []
        for key, r in batch:
            self.handle(key, r)

    def run(self):
        H.log(f"relay running as {self.cfg['computer_name']!r}")
        while True:
            try:
                self.tick()
            except Exception:
                H.log("unexpected error (continuing):\n" + traceback.format_exc())
                time.sleep(1.0)


# ------------------------------------------------------------------ --probe

def probe_check(wait=8.0):
    r = Relay(probe_only=True)
    H.log("Looking for the keyboard and mouse. Move the mouse and press a key now "
          "so both are awake...")
    end = time.monotonic() + wait
    while time.monotonic() < end:
        r.tick()
        devs = list(r.devices.values())
        if any(d.type == "keyboard" and d.present for d in devs) and \
                any(d.type == "mouse" and d.present for d in devs):
            break
    present = [d for d in r.devices.values() if d.present]
    kbs = [d for d in present if d.type == "keyboard"]
    mice = [d for d in present if d.type == "mouse"]
    if not present:
        H.log("RESULT: no Logitech keyboard or mouse found. Are they connected to THIS "
              "computer right now?" + (" On a Mac, also check Input Monitoring."
                                       if sys.platform == "darwin" else ""))
        return 1
    problems = []
    if not kbs:
        problems.append("no keyboard answered")
    if not mice:
        problems.append("no mouse answered (move it and check again)")
    chans = {d.channel for d in present}
    if len(chans) > 1:
        problems.append("this computer is on different channel numbers on the keyboard "
                        "and the mouse; re-pair so it uses the same number on both")
    for kb in kbs:
        if not set(HOST_KEYS) <= kb.divertable():
            H.log("NOTE: this keyboard's Easy-Switch keys can't be taken over, so use "
                  "network follow and/or shortcut keys (open the EasySwitch Relay settings).")
    if problems:
        H.log("RESULT: " + "; ".join(problems) + ".")
        return 1
    H.log(f"RESULT: OK. This computer is channel {chans.pop() + 1} on all devices.")
    return 0
