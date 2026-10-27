"""Signed messages between the relays on your computers (local network only).

Every packet is "ESR1" + HMAC-SHA256(key, payload) + payload (JSON). The key is
derived from the pairing code you enter on each computer, so only relays with
the same code can make each other act. Packets also carry a timestamp and a
random nonce; anything older than two minutes, from the future, or seen
before is dropped. Nothing is sent until a pairing code is set.

Messages:
  hello  every 10 s: name, and which devices are connected here (and channels)
  here   a device just connected here, on channel N
  goto   please move device X to channel N (from a shortcut key)
  keys   the shortcut-key setup, so it only has to be done once
"""
import hashlib
import hmac
import json
import secrets
import socket
import threading
import time

import esr_hid
from esr_config import normalize_code

MAGIC = b"ESR1"
MAX_AGE = 120.0
PEER_TIMEOUT = 35.0


def derive_key(code):
    return hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode(),
                               b"easyswitch-relay/v1", 200_000, 32)


def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:   # the address of the interface a LAN packet would leave from (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return {ip for ip in ips if not ip.startswith("127.")}


class Net:
    def __init__(self, code, port, name, extra_peers=()):
        self.key = derive_key(code)
        self.port = int(port)
        self.name = name
        self.extra = [p for p in extra_peers if p]
        self.iid = secrets.token_hex(6)        # identifies this relay run
        self.peers = {}                        # iid -> {name, addr, seen, devices}
        self.lock = threading.Lock()           # peers is shared with the receive thread
        self.seen = {}                         # (iid, nonce) -> time, for replays
        self.error = ""
        self.sock = None
        self.running = False

    def start(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", self.port))
            s.settimeout(0.5)
            self.sock = s
        except OSError as e:
            self.error = f"could not listen on UDP port {self.port}: {e}"
            esr_hid.log("network:", self.error)
            return False
        self.running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()
        esr_hid.log(f"network: listening on UDP port {self.port} as {self.name!r}")
        return True

    def stop(self):
        self.running = False
        try:
            self.sock and self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ sending
    def _targets(self):
        t = {"255.255.255.255"}
        for ip in local_ipv4s():
            parts = ip.split(".")
            if len(parts) == 4:
                t.add(".".join(parts[:3] + ["255"]))      # assumes a /24 home network
        now = time.time()
        with self.lock:
            for p in self.peers.values():
                if now - p["seen"] < PEER_TIMEOUT * 4:
                    t.add(p["addr"])
        t.update(self.extra)
        return t

    def send(self, mtype, **fields):
        if not self.sock:
            return
        msg = {"v": 1, "t": mtype, "id": self.iid, "n": secrets.token_hex(8),
               "ts": time.time(), "name": self.name, **fields}
        payload = json.dumps(msg, separators=(",", ":")).encode()
        pkt = MAGIC + hmac.new(self.key, payload, hashlib.sha256).digest() + payload
        for addr in self._targets():
            try:
                self.sock.sendto(pkt, (addr, self.port))
            except OSError:
                pass

    # ---------------------------------------------------------- receiving
    def _recv_loop(self):
        while self.running:
            try:
                data, (addr, _port) = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.5)
                continue
            msg = self.verify(data)
            if msg is None or msg.get("id") == self.iid:
                continue
            with self.lock:
                old = self.peers.get(msg["id"], {})
                self.peers[msg["id"]] = {"name": str(msg.get("name", "?"))[:40], "addr": addr,
                                         "seen": time.time(),
                                         "devices": msg.get("devices", old.get("devices", []))}
            esr_hid.reports.put(("__net__", msg))

    def verify(self, data):
        if len(data) < 37 or not data.startswith(MAGIC):
            return None
        mac, payload = data[4:36], data[36:]
        if not hmac.compare_digest(mac, hmac.new(self.key, payload, hashlib.sha256).digest()):
            return None
        try:
            msg = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(msg, dict) or msg.get("v") != 1:
            return None
        now = time.time()
        if abs(now - float(msg.get("ts", 0))) > MAX_AGE:
            return None
        nonce = (str(msg.get("id")), str(msg.get("n")))
        if nonce in self.seen:
            return None
        self.seen[nonce] = now
        if len(self.seen) > 2000:
            self.seen = {k: v for k, v in self.seen.items() if now - v < MAX_AGE * 2}
        return msg

    def peer_list(self):
        now = time.time()
        with self.lock:
            return [{"name": p["name"], "addr": p["addr"], "age": round(now - p["seen"]),
                     "devices": p["devices"]}
                    for p in self.peers.values() if now - p["seen"] < PEER_TIMEOUT]
