#!/usr/bin/env python3
"""
EasySwitch Relay: keep a Logitech keyboard and mouse on the same computer.

    easyswitch-relay              open the settings window
    easyswitch-relay --run        run the relay (started automatically at sign-in)
    easyswitch-relay --probe      check the devices connected here, then exit
    easyswitch-relay --diagnose   write a detailed device report (diagnose.txt)
    easyswitch-relay --version

Modules:
  esr_hid      device layer: HID++ transport, logging, file locations
  esr_relay    the relay: presence, shortcut keys, follow, status
  esr_net      signed messages between your computers (local network only)
  esr_config   settings shared by the relay and the settings window
  esr_ui       the settings window
  esr_diag     the --diagnose report
"""
import argparse
import os
import sys

import esr_hid as H


def main():
    ap = argparse.ArgumentParser(
        prog="easyswitch-relay",
        description="Keep a Logitech keyboard and mouse on the same computer.")
    ap.add_argument("--run", action="store_true", help="run the relay in the background")
    ap.add_argument("--probe", action="store_true",
                    help="check the devices connected here, print the result, and exit")
    ap.add_argument("--diagnose", action="store_true",
                    help="write a detailed device report (diagnose.txt next to the log)")
    ap.add_argument("--settings", action="store_true", help="open the settings window")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    args, _unknown = ap.parse_known_args()     # macOS may add its own arguments

    console = args.probe or args.version or args.diagnose
    if console and not os.environ.get("EASYSWITCH_QUIET"):
        H.attach_parent_console()
    if args.version:
        import hid
        hv = getattr(hid, "version_str", lambda: "?")()
        print(f"easyswitch-relay {H.__version__} (hidapi {hv})")
        return 0

    if args.probe or args.diagnose:
        H.SW_ID = 0x0B          # the background relay uses 0x0A; keep replies apart
        H.open_log()
        H.log(f"easyswitch relay {H.__version__} ({sys.platform}, log: {H.LOG_PATH})")
        H.macos_input_monitoring()
        if args.diagnose:
            from esr_diag import diagnose
            return diagnose()
        from esr_relay import probe_check
        return probe_check()

    if args.run:
        H.open_log()
        if not H.single_instance():
            H.log("another copy of the relay is already running; exiting")
            return 0
        H.log(f"easyswitch relay {H.__version__} starting ({sys.platform}, log: {H.LOG_PATH})")
        if H.EXCLUSIVE_OFF is not True:
            H.log("WARNING: could not turn off exclusive device access:", H.EXCLUSIVE_OFF,
                  "- typing on this Mac may stop while the relay runs; quit it if so.")
        H.macos_input_monitoring()
        from esr_relay import Relay
        Relay().run()
        return 0

    # Default (double-click, Start menu, --settings): the settings window.
    from esr_ui import run_settings
    return run_settings()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
