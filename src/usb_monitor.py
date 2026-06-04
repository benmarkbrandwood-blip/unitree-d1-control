"""USB-C serial monitor for D1 arm debug output.

Reads from /dev/ttyACM0 or /dev/ttyUSB0 in a background daemon thread,
buffers the last 200 lines in memory, and writes timestamped entries to
logs/usb_monitor.log. Non-blocking: if no USB port is found, it silently
continues without serial monitoring.
"""

import collections
import datetime
import logging
import pathlib
import threading
from typing import Optional

import serial
import serial.tools.list_ports

LOG_FILE = pathlib.Path("logs/usb_monitor.log")
BAUD_RATE = 115200

# USB-to-serial chip vendor IDs: STM32 VCP, Silicon Labs CP210x, WCH CH340
_KNOWN_VIDS = {0x0483, 0x10C4, 0x1A86}


def find_d1_usb_port() -> Optional[str]:
    """Scan serial ports and return the most likely D1 debug port path.

    First tries known USB-to-serial VIDs, then falls back to any ACM/USB device.

    Returns:
        Device path (e.g. '/dev/ttyACM0') or None if nothing found.
    """
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid in _KNOWN_VIDS:
            return port.device
    for port in ports:
        if "ACM" in port.device or "USB" in port.device:
            return port.device
    return None


class USBMonitor:
    """Reads the D1 USB-C debug serial port in a background daemon thread."""

    def __init__(self, port: Optional[str] = None, baud: int = BAUD_RATE):
        """
        Args:
            port: Serial device path. If None, auto-detects via find_d1_usb_port.
            baud: Baud rate (default 115200, D1 debug default).
        """
        self._port = port or find_d1_usb_port()
        self._baud = baud
        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lines: collections.deque = collections.deque(maxlen=200)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        """Open serial port and start background reader thread.

        Silent no-op if no port is found (USB cable not connected).
        """
        if self._port is None:
            logging.warning("USB monitor: no port detected — running without debug serial.")
            return
        try:
            self._serial = serial.Serial(self._port, self._baud, timeout=1.0)
            self._thread = threading.Thread(
                target=self._reader, daemon=True, name="usb-monitor"
            )
            self._thread.start()
            logging.info("USB monitor started on %s @ %d baud.", self._port, self._baud)
        except serial.SerialException as exc:
            logging.warning("USB monitor: could not open %s: %s", self._port, exc)

    def _reader(self) -> None:
        """Background thread: read lines, buffer in deque, append to log file."""
        with open(LOG_FILE, "a") as fh:
            while not self._stop_event.is_set():
                try:
                    if self._serial and self._serial.in_waiting:
                        raw = self._serial.readline()
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        if line:
                            ts = datetime.datetime.now().isoformat(timespec="milliseconds")
                            entry = f"[{ts}] {line}"
                            self._lines.append(entry)
                            fh.write(entry + "\n")
                            fh.flush()
                except (serial.SerialException, OSError):
                    break

    def stop(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        logging.info("USB monitor stopped.")

    def get_last_lines(self, n: int = 20) -> list[str]:
        """Return the last n lines received from the debug port.

        Args:
            n: Maximum number of lines to return.

        Returns:
            List of timestamped log lines (may be fewer than n if buffer is smaller).
        """
        return list(self._lines)[-n:]
