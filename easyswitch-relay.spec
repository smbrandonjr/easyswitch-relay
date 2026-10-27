# PyInstaller build recipe for EasySwitch Relay (used by windows/build.ps1 and macos/build.sh).
# Produces a folder app (not a one-file exe): it starts faster and is far less
# likely to be flagged by antivirus than a self-extracting one-file build.
import re
import sys

VERSION = re.search(r'^__version__ = "(.+)"', open("easyswitch_relay.py").read(), re.M).group(1)

a = Analysis(
    ["easyswitch_relay.py"],
    excludes=["tkinter", "unittest", "pydoc", "doctest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="easyswitch-relay",
    console=False,          # background app: no console window at login
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
            "LSUIElement": True,      # no Dock icon, no menu bar
            "LSMinimumSystemVersion": "11.0",
        },
    )
