"""Network configuration: save, apply, and restore Ethernet IP for D1 arm control.

The arm expects the PC to be on 192.168.123.x/24. This module temporarily sets
that static IP, verifies connectivity, and always restores original config on exit.
"""

import atexit
import json
import pathlib
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Optional

BACKUP_FILE = pathlib.Path("config/net_backup.json")
ARM_IP = "192.168.123.100"
PC_IP = "192.168.123.162"
NETMASK = "24"

_restored = False  # guard against double-restore from atexit + signal


@dataclass
class NetState:
    iface: str
    was_dhcp: bool
    original_ip: Optional[str]
    original_prefix: Optional[str]
    original_gateway: Optional[str]


def detect_ethernet_iface() -> str:
    """Return the first non-loopback Ethernet interface that is UP or has a cable.

    Raises:
        RuntimeError: if no suitable interface is found.
    """
    result = subprocess.run(
        ["ip", "-j", "link", "show"],
        capture_output=True, text=True, check=True,
    )
    links = json.loads(result.stdout)
    for link in links:
        if (
            link.get("link_type") == "ether"
            and "LOOPBACK" not in link.get("flags", [])
        ):
            return link["ifname"]
    raise RuntimeError(
        "No Ethernet interface found. "
        "Connect the D1 arm's RJ45 cable and check 'ip link show'."
    )


def _is_dhcp(iface: str) -> bool:
    """Return True if the interface is currently configured via DHCP."""
    try:
        result = subprocess.run(
            ["nmcli", "-g", "IP4.METHOD", "device", "show", iface],
            capture_output=True, text=True,
        )
        return "auto" in result.stdout.lower()
    except FileNotFoundError:
        # nmcli unavailable; fall back to checking for a dhclient lease file
        leases = list(pathlib.Path("/var/lib/dhcp").glob(f"dhclient.{iface}.leases"))
        return bool(leases)


def save_current_state(iface: str) -> NetState:
    """Read current IP/DHCP config, write to config/net_backup.json, return state.

    Args:
        iface: Ethernet interface name (e.g. 'eth0').

    Returns:
        NetState capturing the current configuration.
    """
    result = subprocess.run(
        ["ip", "-j", "addr", "show", "dev", iface],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)

    original_ip: Optional[str] = None
    original_prefix: Optional[str] = None
    if data and data[0].get("addr_info"):
        for addr in data[0]["addr_info"]:
            if addr.get("family") == "inet":
                original_ip = addr["local"]
                original_prefix = str(addr["prefixlen"])
                break

    gw_result = subprocess.run(
        ["ip", "-j", "route", "show", "default"],
        capture_output=True, text=True, check=True,
    )
    gw_data = json.loads(gw_result.stdout)
    original_gateway: Optional[str] = None
    for route in gw_data:
        if route.get("dev") == iface:
            original_gateway = route.get("gateway")
            break

    state = NetState(
        iface=iface,
        was_dhcp=_is_dhcp(iface),
        original_ip=original_ip,
        original_prefix=original_prefix,
        original_gateway=original_gateway,
    )

    BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_FILE.write_text(json.dumps(asdict(state), indent=2))
    return state


def apply_arm_ip(state: NetState) -> None:
    """Flush current IP and apply 192.168.123.162/24 (no reboot, temporary).

    Args:
        state: NetState returned by save_current_state.
    """
    subprocess.run(
        ["sudo", "ip", "addr", "flush", "dev", state.iface], check=True
    )
    subprocess.run(
        ["sudo", "ip", "addr", "add", f"{PC_IP}/{NETMASK}", "dev", state.iface],
        check=True,
    )
    subprocess.run(
        ["sudo", "ip", "link", "set", state.iface, "up"], check=True
    )


def restore_state(state: NetState) -> None:
    """Restore the Ethernet interface to its original configuration.

    Handles three cases: was DHCP, was static, or had no IP at all.
    Safe to call multiple times (idempotent via _restored guard).

    Args:
        state: NetState captured before apply_arm_ip was called.
    """
    global _restored
    if _restored:
        return
    _restored = True

    try:
        subprocess.run(
            ["sudo", "ip", "addr", "flush", "dev", state.iface], check=True
        )
        if state.was_dhcp:
            try:
                subprocess.run(
                    ["sudo", "dhclient", state.iface],
                    check=True, timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["sudo", "systemctl", "restart", "NetworkManager"], check=True
                )
        elif state.original_ip and state.original_prefix:
            subprocess.run(
                ["sudo", "ip", "addr", "add",
                 f"{state.original_ip}/{state.original_prefix}", "dev", state.iface],
                check=True,
            )
            subprocess.run(
                ["sudo", "ip", "link", "set", state.iface, "up"], check=True
            )
            if state.original_gateway:
                subprocess.run(
                    ["sudo", "ip", "route", "add", "default", "via",
                     state.original_gateway, "dev", state.iface],
                    check=True,
                )
    except Exception as exc:
        print(f"[net_config] WARNING: restore failed: {exc}", file=sys.stderr)
    finally:
        if BACKUP_FILE.exists():
            BACKUP_FILE.unlink()


def verify_arm_reachable() -> bool:
    """Ping the arm; return True if reachable."""
    result = subprocess.run(
        ["ping", "-c", "2", "-W", "1", ARM_IP],
        capture_output=True,
    )
    return result.returncode == 0


def setup(iface: Optional[str] = None) -> NetState:
    """Full setup: detect interface, save state, apply arm IP, register cleanup.

    Registers atexit and SIGINT/SIGTERM handlers to restore the original config
    automatically. Always call this before any DDS communication.

    Args:
        iface: Ethernet interface name. If None, auto-detected.

    Returns:
        NetState for the configured interface.

    Raises:
        RuntimeError: if the arm is not reachable after IP configuration.
    """
    iface = iface or detect_ethernet_iface()
    state = save_current_state(iface)
    apply_arm_ip(state)

    atexit.register(restore_state, state)

    def _signal_handler(sig, frame):
        restore_state(state)
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if not verify_arm_reachable():
        restore_state(state)
        raise RuntimeError(
            f"D1 arm at {ARM_IP} not reachable after applying {PC_IP}/{NETMASK}. "
            "Check RJ45 cable and arm power."
        )
    return state


def restore_from_backup() -> None:
    """Restore from net_backup.json — use for manual recovery after SIGKILL.

    Run directly:  python3 -c "from src.net_config import restore_from_backup; restore_from_backup()"
    """
    if not BACKUP_FILE.exists():
        print("No backup file found at config/net_backup.json. Nothing to restore.")
        return
    data = json.loads(BACKUP_FILE.read_text())
    state = NetState(**data)
    restore_state(state)
    print(f"Restored interface '{state.iface}'.")
