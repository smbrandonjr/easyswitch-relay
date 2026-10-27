# EasySwitch Relay

Press an Easy-Switch key (1, 2 or 3) on your **MX Keys** and your **MX Master 3S**
switches to the same computer at the same moment. No more flipping the mouse over,
and no more typing into the wrong machine.

Version 1.0.0 · Windows 10/11 (x64) and macOS 11+ · runs per user, no admin rights.

---

## How it works

- The relay asks the keyboard to send Easy-Switch presses to the computer instead
  of acting on them itself.
- When you press one, the relay tells the mouse to switch, then the keyboard.
  The relay's part takes a few milliseconds; the rest is the devices reconnecting.
- Nothing is saved on the keyboard or mouse. If the relay isn't running on a
  computer, the keys simply go back to switching only the keyboard, so you can't
  get locked out.
- It talks only to the Logitech devices' own control channel. It never sees what
  you type, makes no network connections, and needs no admin rights.

## Before you start

1. **Use the same channel number for each computer on both devices.** For example,
   Windows PC A = 1 on the keyboard *and* the mouse, Windows PC B = 2 on both, the
   Mac = 3 on both. Re-pair anything that doesn't match. The relay's check will
   tell you if a computer is on different numbers.
2. **Uninstall Logi Options+ (and older Logi Options) on all three computers.**
   They take over the same keys and will conflict.
3. **Bolt receiver or Bluetooth both work.** A Logi Bolt receiver on each computer
   switches a bit faster and has no warm-up gap (see "Day to day").

## 1. Build

You build once per operating system, then copy the result to any other computer
with that OS. Building needs Python 3.10–3.14 on the build machine only; the
finished app runs without Python. Every build dependency is pinned to an exact
version and checked against its published SHA-256 hash, and nothing is built from
source during install.

**Windows** (build on one PC, copy to the other):

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

This creates `dist\EasySwitchRelay-windows-x64.zip` (with a `.sha256` file next to it).

**Mac:**

```bash
bash macos/build.sh
```

This creates `dist/EasySwitchRelay-macos-arm64.zip` (`x86_64` on an Intel Mac).
The Mac's built-in Python is too old; install one from python.org or with
`brew install python@3.12`.

**Or build on GitHub, with no Python anywhere:** push this folder to a GitHub
repository and run **Build** from the Actions tab. You get both zips, each with a
signed record of how it was built that you can check with
`gh attestation verify <zip> --repo <you>/<repo>`. The workflow builds for Apple
Silicon Macs; for an Intel Mac, build on the Mac itself.

## 2. Install (on each computer)

Unzip the build, connect the keyboard and mouse to that computer, then run:

| | Install | Re-check devices | Uninstall |
|---|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File install.ps1` | `& "$env:LOCALAPPDATA\Programs\EasySwitchRelay\easyswitch-relay.exe" --probe` | `powershell -ExecutionPolicy Bypass -File uninstall.ps1` |
| Mac | `bash install.sh` | `bash install.sh --check` | `bash uninstall.sh` |

The installer:

- copies the app into your user folder (Windows: `%LOCALAPPDATA%\Programs\EasySwitchRelay`;
  Mac: `~/Applications/EasySwitch Relay.app`);
- sets it to start at sign-in and restart if it ever crashes;
- checks your devices and prints the result;
- starts the relay.

Running the installer again updates the app in place.

**Mac only: Input Monitoring.** macOS asks for this the first time the relay runs,
because the keyboard counts as an input device. Allow it under
**System Settings → Privacy & Security → Input Monitoring → EasySwitch Relay**,
then run `bash install.sh --check`. If you rebuild the app, macOS treats it as a
new app, so you'll need to allow it again.

A good check result looks like:

```
found keyboard 'MX Keys' ...; this computer is channel 1 of 3
found device   'MX Master 3S' ...; this computer is channel 1 of 3
RESULT: OK. This computer is channel 1 on all devices.
```

## Day to day

- **Press 1, 2 or 3 on the keyboard.** The mouse and keyboard move together.
- **Pressing the key for the computer you're already on does nothing.** That's on
  purpose.
- **Bluetooth warm-up:** for a second or two after the devices arrive at a computer,
  a press may move only the keyboard. Over a Bolt receiver this doesn't happen.
- **Out of sync** (for example, the mouse was switched with its own button): flip
  the mouse over and press its channel button once. You're back in step.
- **A computer that's asleep or signed out** isn't running the relay, so from that
  computer the keys switch only the keyboard.

## Troubleshooting

**Log:** Windows `%LOCALAPPDATA%\EasySwitchRelay\relay.log`; Mac
`~/Library/Logs/EasySwitchRelay/relay.log`.

| Check result or symptom | What to do |
|---|---|
| `no Logitech keyboard or mouse found` | Make sure the devices are connected to this computer right now. On a Mac, check Input Monitoring. |
| `no keyboard accepted the Easy-Switch takeover` | Logi Options+ may still be running; uninstall it. Otherwise send me the log. |
| `different channel numbers` | Re-pair so this computer has the same number on both devices. |
| Keys switch only the keyboard | Is the relay running? Windows: Task Manager → `easyswitch-relay`. Mac: `pgrep -lf easyswitch-relay`. |
| Mac stops taking typing while the relay runs | Quit it (`bash uninstall.sh`) and send me the log. This is a known macOS risk the relay guards against. |
| Windows warns the app is unrecognized, or antivirus flags it | It's unsigned and packaged with PyInstaller, which some antivirus tools flag. Build it yourself from this folder if you prefer, and compare the `.sha256` before copying it to another PC. |

## What's in this folder

| File | What it is |
|---|---|
| `easyswitch_relay.py` | The relay itself (about 500 lines, readable top to bottom). |
| `easyswitch-relay.spec` | PyInstaller recipe: a folder app with no console and no Dock icon. |
| `requirements-build.txt` | Hash-locked build dependencies: hidapi 0.15.0, PyInstaller 6.22.3, and PyInstaller's own dependencies. |
| `windows/`, `macos/` | Build, install and uninstall scripts. |
| `.github/workflows/build.yml` | Optional cloud build, with actions pinned to exact commits. |

## Security notes

- **hidapi 0.15.0** (the USB/HID library) was reviewed before use:
  - the published package matches its public source;
  - nothing malicious and no network code was found;
  - the issues found were in how the relay called it, and are fixed here.
- **Runtime access:** the relay opens only Logitech vendor control interfaces
  (vendor ID 046D, usage pages FF00/FF43). It does not open the normal keyboard
  input channel.
- **Files it writes:** only its log and a lock file that stops two copies running.
- **Removal:** the uninstallers remove everything, including the Mac's Input
  Monitoring entry. Logs are kept unless you ask for them to be removed.
