"""--diagnose: a detailed report of what each Logitech device supports.
Contains no personal data: only device names, feature lists and key IDs."""
import os
import sys
import time

import hid

import esr_hid as H
from esr_relay import HOST_KEYS

FEATURE_NAMES = {
    0x0000: "ROOT", 0x0001: "FEATURE_SET", 0x0003: "FIRMWARE_INFO", 0x0005: "DEVICE_NAME",
    0x0007: "DEVICE_FRIENDLY_NAME", 0x1000: "BATTERY_STATUS", 0x1004: "UNIFIED_BATTERY",
    0x1814: "CHANGE_HOST", 0x1815: "HOSTS_INFO", 0x1b04: "REPROG_CONTROLS_V4",
    0x1d4b: "WIRELESS_DEVICE_STATUS", 0x1981: "BACKLIGHT", 0x1982: "BACKLIGHT2",
    0x2110: "SMART_SHIFT", 0x2121: "HIRES_WHEEL", 0x2150: "THUMB_WHEEL",
    0x2201: "ADJUSTABLE_DPI", 0x40a3: "FN_INVERSION", 0x4100: "ENCRYPTION",
    0x4220: "LOCK_KEY_STATE", 0x4521: "DISABLE_KEYS", 0x4531: "MULTIPLATFORM",
    0x4540: "KEYBOARD_INTERNATIONAL_LAYOUTS", 0x00c2: "DFU_CONTROL_SIGNED",
    0x00d0: "DFU", 0x1802: "DEVICE_RESET", 0x1803: "GPIO_ACCESS", 0x1805: "OOBSTATE",
    0x1806: "CONFIG_DEVICE_PROPS", 0x18a1: "LED_SW_CONTROL",
}
CID_FLAGS = ["mouse", "fn", "nonstd", "fn-sensitive", "reprogrammable", "divertable",
             "persist-divertable", "virtual"]


def diagnose():
    """Write a detailed report of what each Logitech device supports. Contains
    no personal data: only device names, feature lists and key IDs."""
    report = []

    def out(line=""):
        report.append(line)
        H.log(line)

    T = 1.0                                     # generous timeouts for this report
    out(f"=== EasySwitch Relay {H.__version__} diagnose ({sys.platform}) ===")
    out("Move the mouse and press a key so both are awake. Collecting in 2 seconds...")
    time.sleep(2)

    infos = hid.enumerate(H.LOGI_VID, 0)
    out(f"{len(infos)} Logitech HID interface(s):")
    for i in infos:
        out(f"  pid={i['product_id']:04x} usage_page={i['usage_page']:04x} "
            f"usage={i['usage']:04x} interface={i['interface_number']} "
            f"bus={i.get('bus_type', '?')} name={i.get('product_string')!r}")

    links = {}
    H.scan(links)
    out(f"{len(links)} HID++ endpoint(s) opened.")
    for link in links.values():
        kind = "receiver" if link.is_receiver else "bluetooth"
        for idx in (H.RECEIVER_SLOTS if link.is_receiver else [H.BT_INDEX]):
            ping = H.request(link, idx, H.F_ROOT, 1, [0, 0, 0x5A], T) or \
                H.request(link, idx, H.F_ROOT, 1, [0, 0, 0x5A], T)
            if not ping:
                if not link.is_receiver:
                    out(f"\n[{kind}] no answer: {H.last_error}")
                continue
            f_name = H.get_feature(link, idx, H.F_NAME, T)
            name = H.get_name(link, idx, f_name, T) if f_name else "?"
            dtype = H.get_type(link, idx, f_name, T) if f_name else "?"
            out(f"\n[{kind} {idx:#04x}] HID++ {ping[0]}.{ping[1]}  name={name!r}  type={dtype}")
            fs = H.get_feature(link, idx, 0x0001, T)
            feats = {}
            if fs:
                n = (H.request(link, idx, fs, 0, timeout=T) or [0])[0]
                for i in range(1, n + 1):
                    r = H.request(link, idx, fs, 1, [i], T)
                    if r:
                        feats[(r[0] << 8) | r[1]] = i
            out("  features: " + ", ".join(
                f"{fid:04x}{'(' + FEATURE_NAMES[fid] + ')' if fid in FEATURE_NAMES else ''}"
                for fid in sorted(feats)))
            f_host = H.get_feature(link, idx, H.F_CHANGE_HOST, T)
            if f_host:
                h = H.request(link, idx, f_host, 0, timeout=T)
                out(f"  CHANGE_HOST: hosts={h[0] if h else '?'} "
                    f"this computer=channel {h[1] + 1 if h else '?'}")
            rp = H.get_feature(link, idx, H.F_REPROG, T)
            if not rp:
                continue
            n = (H.request(link, idx, rp, 0, timeout=T) or [0])[0]
            out(f"  REPROG_CONTROLS_V4: {n} controls (cid flags):")
            for i in range(n):
                r = H.request(link, idx, rp, 1, [i], T)
                if not r:
                    out(f"    #{i}: {H.last_error}")
                    continue
                cid, flags = (r[0] << 8) | r[1], r[4]
                flags2 = r[8] if len(r) > 8 else 0
                names = [CID_FLAGS[b] for b in range(8) if flags & (1 << b)]
                out(f"    cid={cid:#06x} task={(r[2] << 8) | r[3]:#06x} "
                    f"flags={flags:#04x}[{','.join(names)}] flags2={flags2:#04x}"
                    f"{'   <-- Easy-Switch key' if cid in HOST_KEYS else ''}")
            for cid in HOST_KEYS:
                cur = H.request(link, idx, rp, 2, [cid >> 8, cid & 0xFF], T)
                cur_s = f"{cur[2]:#04x}" if cur else H.last_error
                res = H.request(link, idx, rp, 3, [cid >> 8, cid & 0xFF, H.DIVERT, 0, 0], T)
                res_s = "accepted" if res else H.last_error
                out(f"  Easy-Switch {cid:#06x}: reporting before={cur_s}  divert -> {res_s}")
                link.send(idx, rp, 3, [cid >> 8, cid & 0xFF, 0x02, 0, 0])   # undo
    for link in links.values():
        link.alive = False
    path = os.path.join(H.LOG_DIR, "diagnose.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(report) + "\n")
        H.log(f"\nReport saved to {path}")
    except OSError as e:
        H.log("could not save report:", e)
    return 0
