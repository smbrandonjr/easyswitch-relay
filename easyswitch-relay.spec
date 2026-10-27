# PyInstaller build recipe for EasySwitch Relay (used by windows/build.ps1 and macos/build.sh).
# Produces a folder app (not a one-file exe): it starts faster and is far less
# likely to be flagged by antivirus than a self-extracting one-file build.
import re
import sys

VERSION = re.search(r'^__version__ = "(.+)"', open("esr_hid.py").read(), re.M).group(1)

a = Analysis(
    ["easyswitch_relay.py"],
    excludes=["unittest", "pydoc", "doctest"],     # tkinter is needed: settings window
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="easyswitch-relay",
    console=False,          # no console window (the relay runs in the background)
    upx=False,              # UPX-packed binaries are an antivirus red flag
    codesign_identity=None, # macOS: ad-hoc signature (build.sh re-signs the bundle)
)
coll = COLLECT(exe, a.binaries, a.datas, name="easyswitch-relay", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="EasySwitch Relay.app",
        bundle_identifier="local.easyswitch.relay",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,      # no Dock icon for the background relay
            "NSHumanReadableCopyright": "",
            "LSMinimumSystemVersion": "11.0",
        },
    )
