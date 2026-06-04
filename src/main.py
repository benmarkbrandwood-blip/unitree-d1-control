#!/usr/bin/env python3
"""Unitree D1 arm control — entry point.

Startup sequence:
  1. Configure Ethernet to 192.168.123.162/24 (arm subnet), save original config.
  2. Start USB-C serial debug monitor (non-blocking if cable absent).
  3. Connect to arm via DDS.
  4. Enable joint motors.
  5. Run demo: home → read joints → move J0 to +30° → pause → home.
  6. Disconnect arm, stop USB monitor.
  7. Restore original Ethernet config (also fires automatically via atexit).

Run from project root:
  python3 src/main.py

Manual IP restore after SIGKILL:
  python3 -c "from src.net_config import restore_from_backup; restore_from_backup()"
"""

import logging
import pathlib
import sys
import time

# ── locate project root and make src/ importable ─────────────────────────────
_HERE = pathlib.Path(__file__).parent          # …/src/
_ROOT = _HERE.parent                           # …/unitree-d1-control/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── load config (tomllib stdlib ≥3.11, tomli backport for 3.10) ──────────────
_CONFIG_FILE = _ROOT / "config" / "settings.toml"
try:
    if sys.version_info >= (3, 11):
        import tomllib
        _open_mode = "rb"
    else:
        import tomli as tomllib  # type: ignore[no-redef]
        _open_mode = "rb"
    with open(_CONFIG_FILE, _open_mode) as _f:
        cfg = tomllib.load(_f)
except FileNotFoundError:
    sys.exit(f"Config not found: {_CONFIG_FILE}. Run from the project root directory.")
except ImportError:
    sys.exit(
        "tomli is required for Python < 3.11: pip install tomli\n"
        "Or upgrade to Python 3.11+."
    )

# ── logging setup ─────────────────────────────────────────────────────────────
(_ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_ROOT / "logs" / "main.log"),
    ],
)
log = logging.getLogger("main")

# ── project imports ───────────────────────────────────────────────────────────
from src.net_config import setup as net_setup
from src.usb_monitor import USBMonitor
from src.arm_control import D1Arm


def main() -> None:
    """Run the D1 arm demo sequence."""
    net_cfg = cfg["network"]
    usb_cfg = cfg["usb"]
    demo_cfg = cfg["demo"]

    # 1. Network ---------------------------------------------------------------
    log.info("Configuring Ethernet interface for D1 arm subnet...")
    net_setup(iface=net_cfg["iface"] or None)
    log.info("Network ready — arm at %s is reachable.", net_cfg["arm_ip"])

    # 2. USB debug monitor (non-blocking if absent) ----------------------------
    usb = USBMonitor(port=usb_cfg["port"] or None, baud=usb_cfg["baud"])
    usb.start()

    # 3. Arm -------------------------------------------------------------------
    arm = D1Arm(iface=net_cfg["iface"] or None)
    arm.connect()

    try:
        # 4. Enable motors (joints start in released/passive state) ------------
        arm.enable_all(True)
        time.sleep(0.5)

        # 5. Demo sequence -----------------------------------------------------
        log.info("Homing arm...")
        arm.home()
        time.sleep(1.0)

        joints = arm.get_joints(timeout=3.0)
        log.info("Initial joint angles (deg): %s", joints)

        j0_angle = demo_cfg["j0_test_angle"]
        log.info("Moving J0 (base rotation) to %.1f°...", j0_angle)
        arm.move_joints([j0_angle, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], mode=1)
        time.sleep(demo_cfg["pause_seconds"])

        log.info("Returning to home...")
        arm.home()
        time.sleep(4.0)  # arm needs ~3-4 s to complete homing motion

        tail = usb.get_last_lines(10)
        if tail:
            log.info("USB debug tail:\n%s", "\n".join(tail))
        else:
            log.info("USB debug: no data (USB-C cable may not be connected).")

    finally:
        # 6. Always disconnect cleanly -----------------------------------------
        arm.disconnect()
        usb.stop()
        # net_config atexit handler restores Ethernet automatically


if __name__ == "__main__":
    main()
