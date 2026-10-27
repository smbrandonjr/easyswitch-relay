# EasySwitch Relay

Keeps a Logitech keyboard and mouse (for example **MX Keys** and **MX Master 3S**)
on the same computer, so you never type into the wrong one.

Version 1.1.2 · Windows 10/11 (x64) and macOS 11+ · runs per user, no admin rights.

---

## Two ways to switch

| | **Follow (network)** | **Shortcut keys** |
|---|---|---|
| You press | the Easy-Switch keys, as usual (or the mouse's own button) | keys you choose, e.g. media keys you rarely use |
| What happens | the keyboard switches; the mouse follows | both devices switch together |
| Speed | the mouse arrives about 1–3 s after the keyboard | as fast as the devices can reconnect |
| Needs | the same pairing code on every computer, same local network | nothing else (with Follow on, they also work when the mouse is elsewhere) |

You can use both at once. On keyboards whose firmware allows it (the regular MX Keys
does; **MX Keys for Business does not**), the Easy-Switch keys themselves switch both
devices together.

## Install

Download the zip for your system from the
[latest release](../../releases/latest), unzip it, connect the keyboard and mouse to
that computer, then:

| | Install (also updates; settings are kept) | Uninstall |
|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File install.ps1` | `powershell -ExecutionPolicy Bypass -File uninstall.ps1` |
| Mac | `bash install.sh` | `bash uninstall.sh` |

The installer:

- copies the app into your user folder;
- sets the relay to start at sign-in and restart if it ever crashes;
- checks your devices;
- starts the relay and opens the **settings window**.

To reopen the settings later:

- **Windows:** Start menu → **EasySwitch Relay**.
- **Mac:** open **EasySwitch Relay** in your Applications folder (`~/Applications`).

**Mac:**

- Allow **Input Monitoring** when asked (System Settings → Privacy & Security). The
  keyboard counts as an input device.
- If macOS asks whether the app may accept incoming network connections, choose
  Allow. Network follow needs it.

**Windows:** the first time network follow is on, Windows may ask whether
EasySwitch Relay may use your network. Allow it on **private** networks.

## Set up (once)

1. **Same channel numbers:** each computer should be the same number on both devices,
   for example Work PC = 1, Laptop = 2, Mac = 3. The Status tab shows each device's
   channel here.
2. **Follow tab**, on the first computer: click **Generate**, then **Copy**, then
   **Save**.
3. On each other computer: paste or type the same pairing code into its Follow tab,
   then **Save**. Within about 10 seconds each computer lists the others on its
   Status tab.
4. **Shortcut keys tab** (optional): click **Set...** next to a channel and press
   the key to use. Keys you rarely use are best; their normal action is replaced
   while the relay runs. With "use the same keys on all my computers" on, you only
   do this once.

## Day to day

- Switch with the Easy-Switch keys as usual. The mouse follows.
- Or press a shortcut key. Both devices switch together.
- **Out of sync** (for example, a computer was asleep): press the Easy-Switch or
  shortcut key again, or flip the mouse over and use its button.
- **A computer that's asleep or signed out** isn't running the relay, so it can't
  send the mouse anywhere. Wake it first.

## Troubleshooting

The **Status** tab shows the devices connected here, the other computers heard on
the network, and the last switch. **Open log** shows the details.

**Get logs from other computers** fetches each paired computer's recent log over the
signed network and saves it in the `peer-logs` folder next to your settings. That way
you can read a Mac's log from a PC, or the other way round. Only your paired computers
can request or send logs.

| Symptom | What to do |
|---|---|
| Status says the relay isn't running | Click **Start relay**. |
| No other computers listed | Same pairing code and same port on each? Firewall prompt allowed? Some networks block broadcasts: enter the other computers' addresses under Follow → Advanced. |
| Mouse doesn't follow | Is the computer that has the mouse awake, with the relay running? Check its Status tab. |
| A shortcut key does nothing | Set it again on the Shortcut keys tab while the keyboard is connected to that computer. |
| **Set...** says no key press was seen | Use a media key on the top row (volume, play/pause). If the keyboard is in F1–F12 mode, hold Fn while pressing it. Still nothing? Run the key test below and send the output. |
| Mac: no devices found, or the keyboard shows "Connected here: no" while you're typing on it | Turn on Input Monitoring for EasySwitch Relay (the Status tab warns if it's off), then reopen the settings. |

**Device check from a terminal.** These write to the log and print the result.

| | Check devices | Detailed report (`diagnose.txt` next to the log) | Key test (prints what each key press sends) |
|---|---|---|---|
| Windows | `& "$env:LOCALAPPDATA\Programs\EasySwitchRelay\easyswitch-relay.exe" --probe` | `... --diagnose` | `... --keytest` |
| Mac | `"$HOME/Applications/EasySwitch Relay.app/Contents/MacOS/easyswitch-relay" --probe` | `... --diagnose` | `... --keytest` |

**Where files live:**

- **Windows:** settings, status and log are all in `%LOCALAPPDATA%\EasySwitchRelay`.
- **Mac:** settings and status are in `~/Library/Application Support/EasySwitchRelay`;
  the log is in `~/Library/Logs/EasySwitchRelay`.

## Security and privacy

- **Network:** the network is off until you set a pairing code.
  - **Signing:** messages are signed with a key derived from the code (HMAC-SHA256).
    A computer without the code can't make yours do anything.
  - **Freshness:** messages older than two minutes, or seen before, are ignored.
  - **Reach:** they stay on your local network (UDP port 47811 by default).
  - **What they can do:** at most, move your keyboard or mouse to one of its paired
    computers, or share your shortcut-key choices.
- **Devices:** the relay only talks to Logitech devices' own control channel. It never
  sees what you type, and nothing is written permanently to the devices.
- **Files:** the settings file (it holds the pairing code) is readable only by your
  user account on macOS.
- **Uninstall** removes the app, the sign-in entry, the Start menu entry and the Mac's
  Input Monitoring entry. Settings and log are kept unless you add `-RemoveLogs`
  (Windows) or `--remove-logs` (Mac).

## Building it yourself

Pushing a version tag (or publishing a release) builds both apps on GitHub's machines
and attaches them to the release with a signed build-provenance record:

```
gh attestation verify <zip> --repo <owner>/<repo>
```

**To build locally** you need Python 3.10–3.14 from python.org (it includes the Tk
library the settings window uses):

- Windows: `powershell -ExecutionPolicy Bypass -File windows\build.ps1`
- Mac: `bash macos/build.sh`

**Build hardening:**

- Every build dependency is pinned and hash-checked in `requirements-build.txt`.
- Packages install from prebuilt files only, so nothing runs code during install.
- The workflow's actions are pinned to exact commits.

| File | What it is |
|---|---|
| `easyswitch_relay.py` | Entry point: settings window, `--run`, `--probe`, `--diagnose`. |
| `esr_hid.py` | Device layer: Logitech HID++ transport, logging, file locations. |
| `esr_relay.py` | The relay: device presence, shortcut keys, follow, status. |
| `esr_net.py` | Signed messages between your computers. |
| `esr_config.py` | Settings shared by the relay and the settings window. |
| `esr_ui.py` | The settings window. |
| `esr_diag.py` | The `--diagnose` report. |
