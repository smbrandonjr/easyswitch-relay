"""Settings window. Talks to the running relay only through files: it writes
config.json (the relay applies changes within a second) and reads status.json
(the relay updates it every 2 seconds)."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import esr_config as C
import esr_hid as H

TASK_NAME = "EasySwitch Relay"
AGENT_LABEL = "local.easyswitch.relay"


def relay_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run"]
    return [sys.executable, os.path.abspath(sys.argv[0]), "--run"]


def start_relay():
    try:
        if sys.platform == "win32":
            r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True,
                               creationflags=0x08000000)          # CREATE_NO_WINDOW
            if r.returncode == 0:
                return True
            subprocess.Popen(relay_command(), creationflags=0x00000008 | 0x08000000)
            return True
        if sys.platform == "darwin":
            r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                               capture_output=True)
            if r.returncode == 0:
                return True
        subprocess.Popen(relay_command(), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        messagebox.showerror("EasySwitch Relay", f"Couldn't start the relay: {e}")
        return False


def open_path(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)                                    # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("EasySwitch Relay", f"Couldn't open {path}: {e}")


def read_status():
    try:
        with open(H.STATUS_PATH, encoding="utf-8") as f:
            st = json.load(f)
        st["fresh"] = time.time() - float(st.get("updated", 0)) < 7
        return st
    except (OSError, ValueError):
        return {"fresh": False}


# --------------------------------------------------------------- key capture

class KeyCapture:
    """Briefly takes over every divertable key on the keyboard connected here
    and reports the first one pressed. The relay is told (via a flag file) to
    ignore key presses meanwhile."""
    links = {}

    def __init__(self, result_q):
        self.q = result_q
        self.cancelled = False

    def _flag(self, on):
        try:
            if on:
                os.makedirs(H.DATA_DIR, exist_ok=True)
                with open(H.CAPTURE_FLAG, "w") as f:
                    f.write(str(time.time()))
            elif os.path.exists(H.CAPTURE_FLAG):
                os.remove(H.CAPTURE_FLAG)
        except OSError:
            pass

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.q.put(("error", f"Key capture failed: {e}"))
        finally:
            self._flag(False)

    def _run(self):
        self._flag(True)
        kb = H.find_keyboard(self.links)
        if not kb:
            self.q.put(("error", "No keyboard is connected to this computer right now. "
                                 "Switch the keyboard here (and press a key to wake it), "
                                 "then try again."))
            return
        link, idx, f_reprog, kb_name = kb
        controls = H.list_controls(link, idx, f_reprog)
        tasks = {c: t for c, t, _f in controls}
        cids = [c for c, _t, f in controls if f & H.DIVERTABLE]
        if not cids:
            self.q.put(("error", f"{kb_name} doesn't let software use any of its keys."))
            return
        H.log(f"key capture: asking {kb_name} for {len(cids)} keys: "
              + ", ".join(f"{c:#06x}" for c in cids))
        taken, refused = H.take_keys(link, idx, f_reprog, cids)
        for c, why in refused.items():
            H.log(f"key capture: {c:#06x} refused: {why}")
        if not taken:
            self.q.put(("error", f"{kb_name} refused every key ({next(iter(refused.values()))}). "
                                 "Use Open log on the Status tab and send me the log."))
            return
        H.log(f"key capture: {len(taken)} keys ready, waiting for a press")
        self.q.put(("ready", (kb_name, len(taken), len(cids))))
        end = time.monotonic() + 20
        got, seen = None, 0
        try:
            while time.monotonic() < end and not self.cancelled and got is None:
                self._flag(True)
                try:
                    key, r = H.reports.get(timeout=0.5)
                except queue.Empty:
                    continue
                if key != link.key or not isinstance(r, list) or len(r) < 4 or r[1] != idx:
                    continue
                if seen < 40:                 # what the keyboard actually sends (for support)
                    seen += 1
                    H.log("key capture: received", H.describe_report(r))
                if r[2] == f_reprog and r[3] == 0x00 and len(r) >= 6:   # key event
                    pressed = [(r[i] << 8) | r[i + 1] for i in range(4, min(len(r) - 1, 12), 2)]
                    pressed = [c for c in pressed if c]
                    if pressed:
                        got = pressed[0]
        finally:
            H.release_keys(link, idx, f_reprog, taken)   # the relay re-takes its own in ~2 s
        if got is not None:
            H.log(f"key capture: got {got:#06x}")
            self.q.put(("key", {"cid": got, "label": H.key_label(got, tasks.get(got))}))
        elif not self.cancelled:
            H.log(f"key capture: no key press seen ({seen} reports from the keyboard)")
            self.q.put(("error", "No key press was seen. Try a media key on the top row "
                                 "(volume, play/pause). If your keyboard is in F1-F12 mode, "
                                 "hold Fn while pressing it. If that still fails, use Open "
                                 "log on the Status tab and send me the log."))


# ------------------------------------------------------------------- window

class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.cfg = C.load()
        self.capture = None
        self.capture_q = queue.Queue()
        self.capture_ch = None
        root.title("EasySwitch Relay")
        root.minsize(620, 520)
        base = tkfont.nametofont("TkDefaultFont")
        self.f_bold = base.copy()
        self.f_bold.configure(weight="bold")
        self.f_head = base.copy()
        self.f_head.configure(weight="bold", size=max(base.cget("size") + 2, 11)
                              if base.cget("size") > 0 else base.cget("size") - 2)
        self.f_mono = tkfont.nametofont("TkFixedFont").copy()
        self._build()
        self._load_into_widgets()
        self.refresh()

    # ------------------------------------------------------------ layout
    def _build(self):
        r = self.root
        pad = {"padx": 12, "pady": 6}
        top = ttk.Frame(r)
        top.pack(fill="x", **pad)
        self.state_lbl = ttk.Label(top, text="", font=self.f_head)
        self.state_lbl.pack(side="left")
        self.start_btn = ttk.Button(top, text="Start relay", command=self.on_start)
        self.start_btn.pack(side="right")

        nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.tab_status = ttk.Frame(nb, padding=12)
        self.tab_follow = ttk.Frame(nb, padding=12)
        self.tab_keys = ttk.Frame(nb, padding=12)
        nb.add(self.tab_status, text="Status")
        nb.add(self.tab_follow, text="Follow (network)")
        nb.add(self.tab_keys, text="Shortcut keys")
        self._build_status()
        self._build_follow()
        self._build_keys()

        bottom = ttk.Frame(r)
        bottom.pack(fill="x", padx=12, pady=(0, 12))
        self.saved_lbl = ttk.Label(bottom, text="", foreground="#2a7a2a")
        self.saved_lbl.pack(side="left")
        ttk.Button(bottom, text="Close", command=r.destroy).pack(side="right")
        ttk.Button(bottom, text="Save", command=self.on_save).pack(side="right", padx=(0, 8))

    def _build_status(self):
        t = self.tab_status
        ttk.Label(t, text="Devices", font=self.f_bold).pack(anchor="w")
        self.dev_tree = ttk.Treeview(t, columns=("type", "here", "ch"), show="tree headings",
                                     height=4)
        self.dev_tree.heading("#0", text="Device")
        self.dev_tree.heading("type", text="Type")
        self.dev_tree.heading("here", text="Connected here")
        self.dev_tree.heading("ch", text="Channel here")
        self.dev_tree.column("#0", width=230)
        for c, w in (("type", 90), ("here", 120), ("ch", 110)):
            self.dev_tree.column(c, width=w, anchor="center")
        self.dev_tree.pack(fill="x", pady=(4, 10))

        ttk.Label(t, text="Other computers", font=self.f_bold).pack(anchor="w")
        self.peer_tree = ttk.Treeview(t, columns=("addr", "devs", "seen"), show="tree headings",
                                      height=3)
        self.peer_tree.heading("#0", text="Computer")
        self.peer_tree.heading("addr", text="Address")
        self.peer_tree.heading("devs", text="Has")
        self.peer_tree.heading("seen", text="Last heard")
        self.peer_tree.column("#0", width=170)
        for c, w in (("addr", 120), ("devs", 190), ("seen", 90)):
            self.peer_tree.column(c, width=w, anchor="center")
        self.peer_tree.pack(fill="x", pady=(4, 6))
        self.net_lbl = ttk.Label(t, text="", wraplength=580, justify="left")
        self.net_lbl.pack(anchor="w", pady=(0, 8))

        self.last_lbl = ttk.Label(t, text="", wraplength=580, justify="left")
        self.last_lbl.pack(anchor="w", pady=(0, 8))
        self.es_lbl = ttk.Label(t, text="", wraplength=580, justify="left")
        self.es_lbl.pack(anchor="w")

        row = ttk.Frame(t)
        row.pack(anchor="w", pady=(10, 0))
        ttk.Button(row, text="Open log", command=lambda: open_path(H.LOG_PATH)).pack(side="left")
        ttk.Button(row, text="Open settings folder",
                   command=lambda: open_path(H.DATA_DIR)).pack(side="left", padx=8)

    def _build_follow(self):
        t = self.tab_follow
        wrap = 580
        ttk.Label(t, wraplength=wrap, justify="left", text=(
            "Keeps the keyboard and mouse together when you switch with their own buttons, "
            "including the Easy-Switch keys. When one device connects to a computer, that "
            "computer tells the others, and the one holding the other device sends it over. "
            "Expect the second device to follow 1-3 seconds after the first.")).pack(anchor="w")

        box = ttk.LabelFrame(t, text="Pair your computers", padding=10)
        box.pack(fill="x", pady=10)
        ttk.Label(box, wraplength=wrap - 30, justify="left", text=(
            "Use the same pairing code on every computer. Generate one here, then type it into "
            "this window on your other computers. Leave it empty to turn network follow off.")
                  ).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(box, text="Pairing code:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.code_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.code_var, width=26, font=self.f_mono
                  ).grid(row=1, column=1, sticky="w", pady=(8, 0), padx=6)
        ttk.Button(box, text="Generate", command=self.on_generate).grid(row=1, column=2,
                                                                         pady=(8, 0))
        ttk.Button(box, text="Copy", command=self.on_copy).grid(row=1, column=3, padx=6,
                                                                 pady=(8, 0))
        ttk.Label(box, text="This computer's name:").grid(row=2, column=0, sticky="w",
                                                          pady=(8, 0))
        self.name_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.name_var, width=26).grid(row=2, column=1, sticky="w",
                                                                  padx=6, pady=(8, 0))

        self.fk_var = tk.BooleanVar()
        self.fm_var = tk.BooleanVar()
        ttk.Checkbutton(t, variable=self.fk_var, text=(
            "When the keyboard switches computers, bring the mouse")).pack(anchor="w")
        ttk.Checkbutton(t, variable=self.fm_var, text=(
            "When the mouse switches computers (its own button), bring the keyboard")
                        ).pack(anchor="w", pady=(4, 0))

        adv = ttk.LabelFrame(t, text="Advanced", padding=10)
        adv.pack(fill="x", pady=10)
        ttk.Label(adv, text="Network port (UDP):").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.port_var, width=8).grid(row=0, column=1, sticky="w",
                                                                 padx=6)
        ttk.Label(adv, text="Other computers' addresses:").grid(row=1, column=0, sticky="w",
                                                                 pady=(6, 0))
        self.peers_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.peers_var, width=40).grid(row=1, column=1, sticky="w",
                                                                   padx=6, pady=(6, 0))
        ttk.Label(adv, foreground="#666", wraplength=wrap - 30, justify="left", text=(
            "Only needed if your computers don't find each other automatically (some "
            "networks block broadcasts). Separate addresses with commas. All computers "
            "must use the same port.")).grid(row=2, column=0, columnspan=2, sticky="w",
                                              pady=(4, 0))
        if sys.platform == "win32":
            ttk.Label(t, foreground="#666", wraplength=wrap, justify="left", text=(
                "The first time network follow is on, Windows may ask whether EasySwitch Relay "
                "may use your network. Allow it on private networks.")).pack(anchor="w")

    def _build_keys(self):
        t = self.tab_keys
        ttk.Label(t, wraplength=580, justify="left", text=(
            "Keys that switch the keyboard and mouse together, instantly. Pick keys you rarely "
            "use; their normal action is replaced while the relay runs. These work even "
            "without network follow, as long as both devices are on the computer you're "
            "using; with network follow on, they also work when the mouse is elsewhere.")
                  ).pack(anchor="w")
        grid = ttk.Frame(t)
        grid.pack(fill="x", pady=12)
        self.key_lbls, self.chname_vars = {}, {}
        ttk.Label(grid, text="Computer", font=self.f_bold).grid(
            row=0, column=0, sticky="w")
        ttk.Label(grid, text="Name (optional)", font=self.f_bold).grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="Shortcut key", font=self.f_bold).grid(
            row=0, column=2, sticky="w", padx=6)
        for i, ch in enumerate(("1", "2", "3"), start=1):
            ttk.Label(grid, text=f"Channel {ch}").grid(row=i, column=0, sticky="w", pady=4)
            v = tk.StringVar()
            self.chname_vars[ch] = v
            ttk.Entry(grid, textvariable=v, width=18).grid(row=i, column=1, padx=6, pady=4)
            lbl = ttk.Label(grid, text="(none)", width=24)
            lbl.grid(row=i, column=2, sticky="w", padx=6)
            self.key_lbls[ch] = lbl
            ttk.Button(grid, text="Set...", command=lambda c=ch: self.on_set_key(c)).grid(
                row=i, column=3, padx=4)
            ttk.Button(grid, text="Clear", command=lambda c=ch: self.on_clear_key(c)).grid(
                row=i, column=4, padx=4)
        self.sync_var = tk.BooleanVar()
        ttk.Checkbutton(t, variable=self.sync_var, text=(
            "Use the same shortcut keys on all my computers (needs network follow)")
                        ).pack(anchor="w")
        self.capture_lbl = ttk.Label(t, text="", wraplength=580, justify="left",
                                     foreground="#1a4f9c")
        self.capture_lbl.pack(anchor="w", pady=(12, 0))

    # ---------------------------------------------------------- data flow
    def _load_into_widgets(self):
        c = self.cfg
        self.code_var.set(c["pairing_code"])
        self.name_var.set(c["computer_name"])
        self.fk_var.set(bool(c["follow_keyboard"]))
        self.fm_var.set(bool(c["follow_mouse"]))
        self.port_var.set(str(c["port"]))
        self.peers_var.set(", ".join(c.get("extra_peers") or []))
        self.sync_var.set(bool(c["sync_keys"]))
        for ch in ("1", "2", "3"):
            self.chname_vars[ch].set(c["channel_names"].get(ch, ""))
        self._show_keys()

    def _show_keys(self):
        for ch, lbl in self.key_lbls.items():
            k = self.cfg["keys"].get(ch)
            lbl.configure(text=k.get("label") or f"Key {k['cid']:#06x}" if k else "(none)")

    def on_save(self):
        code = self.code_var.get().strip()
        if code and not C.code_is_valid(code):
            messagebox.showerror("EasySwitch Relay", "That pairing code isn't valid. Use "
                                 "Generate, or type the 16-character code exactly as shown "
                                 "on your other computer.")
            return
        try:
            port = int(self.port_var.get())
            assert 1024 <= port <= 65535
        except (ValueError, AssertionError):
            messagebox.showerror("EasySwitch Relay", "The port must be a number from 1024 "
                                 "to 65535.")
            return
        fresh = C.load()             # keep anything the relay changed meanwhile (synced keys)
        if self.cfg["keys"] != fresh["keys"] and self.cfg["keys_updated"] > fresh["keys_updated"]:
            fresh["keys"], fresh["keys_updated"] = self.cfg["keys"], self.cfg["keys_updated"]
        fresh.update({
            "pairing_code": code.upper(),
            "computer_name": self.name_var.get().strip()[:40] or C.default_name(),
            "follow_keyboard": self.fk_var.get(), "follow_mouse": self.fm_var.get(),
            "port": port, "sync_keys": self.sync_var.get(),
            "extra_peers": [p.strip() for p in self.peers_var.get().split(",") if p.strip()],
            "channel_names": {ch: v.get().strip()[:30] for ch, v in self.chname_vars.items()},
        })
        try:
            C.save(fresh)
        except OSError as e:
            messagebox.showerror("EasySwitch Relay", f"Couldn't save settings: {e}")
            return
        self.cfg = fresh
        self._show_keys()
        self.saved_lbl.configure(text="Saved. The relay applies changes within a second.")
        self.root.after(5000, lambda: self.saved_lbl.configure(text=""))

    def on_generate(self):
        if self.code_var.get().strip() and not messagebox.askyesno(
                "EasySwitch Relay", "Replace the current pairing code? Your other computers "
                                    "will need the new one."):
            return
        self.code_var.set(C.new_pairing_code())

    def on_copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.code_var.get().strip())
        self.saved_lbl.configure(text="Pairing code copied.")
        self.root.after(3000, lambda: self.saved_lbl.configure(text=""))

    def on_start(self):
        if start_relay():
            self.state_lbl.configure(text="Starting the relay...")

    def on_set_key(self, ch):
        if self.capture:
            return
        self.capture_ch = ch
        self.capture = KeyCapture(self.capture_q)
        self.capture_lbl.configure(text="Looking for the keyboard...")
        threading.Thread(target=self.capture.run, daemon=True).start()
        self.root.after(100, self.poll_capture)

    def on_clear_key(self, ch):
        self.cfg["keys"][ch] = None
        C.stamp_keys(self.cfg)
        self._show_keys()
        self.capture_lbl.configure(text=f"Channel {ch} shortcut cleared. Click Save to apply.")

    def poll_capture(self):
        try:
            kind, val = self.capture_q.get_nowait()
        except queue.Empty:
            if self.capture:
                self.root.after(100, self.poll_capture)
            return
        ch = self.capture_ch
        if kind == "ready":
            name, n_ok, n_all = val
            self.capture_lbl.configure(text=(
                f"Press the key on your {name} to use for channel {ch} (within 20 seconds). "
                f"Media keys on the top row work best; if the keyboard is in F1-F12 mode, "
                f"hold Fn while pressing. ({n_ok} of {n_all} usable keys are ready.)"))
            self.root.after(100, self.poll_capture)
            return
        self.capture = None
        if kind == "error":
            self.capture_lbl.configure(text=val)
            return
        for other, k in self.cfg["keys"].items():
            if other != ch and k and k.get("cid") == val["cid"]:
                self.cfg["keys"][other] = None
        self.cfg["keys"][ch] = val
        C.stamp_keys(self.cfg)
        self._show_keys()
        self.capture_lbl.configure(text=(
            f"{val['label']} will switch both devices to channel {ch}. Click Save to apply."))

    # ------------------------------------------------------------ status
    def refresh(self):
        st = read_status()
        if st.get("fresh"):
            self.state_lbl.configure(text=f"● Relay running on {st.get('computer', '?')}",
                                     foreground="#2a7a2a")
            self.start_btn.pack_forget()
        else:
            self.state_lbl.configure(text="● Relay not running", foreground="#b03030")
            if not self.start_btn.winfo_ismapped():
                self.start_btn.pack(side="right")

        self.dev_tree.delete(*self.dev_tree.get_children())
        es_notes = []
        for d in st.get("devices", []) if st.get("fresh") else []:
            self.dev_tree.insert("", "end", text=d["name"], values=(
                d["type"], "yes" if d["present"] else "no",
                d["channel"] if d["channel"] else "?"))
            if d["type"] == "keyboard":
                es_notes.append(
                    f"{d['name']}: its Easy-Switch keys "
                    + ("are taken over directly, so they switch both devices instantly."
                       if d.get("easy_switch_divertable") else
                       "can't be taken over (keyboard firmware), so use network follow "
                       "and/or shortcut keys."))
        self.es_lbl.configure(text="\n".join(es_notes))

        self.peer_tree.delete(*self.peer_tree.get_children())
        net = st.get("network", {}) if st.get("fresh") else {}
        for p in net.get("peers", []):
            has = ", ".join(f"{x.get('type')} (ch {x['ch'] + 1})" for x in p.get("devices", [])
                            if isinstance(x.get("ch"), int)) or "-"
            self.peer_tree.insert("", "end", text=p["name"],
                                  values=(p["addr"], has, f"{p['age']} s ago"))
        if not st.get("fresh"):
            msg = "Start the relay to see live status."
        elif not net.get("enabled"):
            msg = "Network follow is off. Set a pairing code on the Follow tab to turn it on."
        elif net.get("error"):
            msg = f"Network problem: {net['error']}"
        elif not net.get("peers"):
            msg = ("Network follow is on, but no other computers have been heard yet. "
                   "Make sure they run version 1.1 or later with the same pairing code.")
        else:
            msg = ""
        self.net_lbl.configure(text=msg)
        last = st.get("last_switch") if st.get("fresh") else ""
        self.last_lbl.configure(text=f"Last switch: {last}" if last else "")
        self.root.after(1000, self.refresh)


def run_settings():
    H.SW_ID = 0x0C          # keep replies apart from the relay's and --probe's
    try:
        if sys.platform == "win32":
            ctypes_scale()      # must happen before the window is created
    except Exception:
        pass
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"Can't open the settings window: {e}")
        return 1
    SettingsApp(root)
    root.lift()
    root.focus_force()
    root.attributes("-topmost", True)
    root.after(600, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0


def ctypes_scale():
    """Crisp text on high-DPI Windows screens."""
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
