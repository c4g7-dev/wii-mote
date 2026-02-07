#!/usr/bin/env python3
"""
Wiimote-to-USB Gamepad Bridge

A headless daemon that runs on a Raspberry Pi Zero W, continuously scanning
for Wiimotes via Bluetooth and forwarding their inputs to an Android device
as standard USB HID gamepads over the OTG port.

Supports up to 2 Wiimotes simultaneously. Each Wiimote maps to a separate
HID gamepad device (/dev/hidg0, /dev/hidg1). Android sees them as standard
USB gamepads — no drivers or apps needed on the Android side.

HID report format (3 bytes per gamepad):
    Byte 0: X axis (signed byte, -127..127) — accelerometer tilt left/right
    Byte 1: Y axis (signed byte, -127..127) — accelerometer tilt fwd/back
    Byte 2: Buttons bitmask (8 bits)
        bit 0: D-Pad Left
        bit 1: D-Pad Right
        bit 2: D-Pad Up
        bit 3: D-Pad Down
        bit 4: A
        bit 5: B
        bit 6: Button 1
        bit 7: Button 2

Special combos:
    + and - together: disconnect this Wiimote
    Home:             recalibrate accelerometer zero-point
"""

import logging
import os
import signal
import struct
import sys
import threading
import time

# Attempt to import cwiid — fail gracefully with a clear message
try:
    import cwiid
except ImportError:
    print(
        "ERROR: cwiid module not found. Install it with:\n"
        "  sudo apt-get install python3-cwiid\n"
        "Or build from source: https://github.com/abstrakraft/cwiid",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NUM_PLAYERS = 2
POLL_RATE_HZ = 100
POLL_INTERVAL = 1.0 / POLL_RATE_HZ
SCAN_RETRY_DELAY = 2.0  # seconds between scan attempts
CONNECT_RUMBLE_DURATION = 0.3  # seconds of rumble on connect
DISCONNECT_RUMBLE_DURATION = 0.5  # seconds of rumble on manual disconnect

# LED bitmasks for player numbers (cwiid LED constants)
PLAYER_LEDS = [
    cwiid.LED1_ON,                          # Player 1: LED 1
    cwiid.LED2_ON,                          # Player 2: LED 2
]

# Accelerometer defaults (overridden by per-Wiimote calibration)
DEFAULT_ACC_ZERO = (128, 128, 128)
ACC_SENSITIVITY = 2.0  # multiplier for raw-to-axis conversion

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("wiimote-bridge")

# ---------------------------------------------------------------------------
# HID Report helpers
# ---------------------------------------------------------------------------

# The 3-byte struct: signed byte (X), signed byte (Y), unsigned byte (buttons)
REPORT_FORMAT = "<bbB"
ZERO_REPORT = struct.pack(REPORT_FORMAT, 0, 0, 0)


def clamp(value, minimum, maximum):
    """Clamp a value to the given range."""
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def acc_to_axis(raw, zero, sensitivity=ACC_SENSITIVITY):
    """Convert a raw accelerometer value to a signed axis byte (-127..127).

    Args:
        raw: Raw accelerometer reading (0-255 range).
        zero: Calibrated zero-point (value at rest).
        sensitivity: Multiplier for the raw offset.

    Returns:
        Integer in range -127..127.
    """
    offset = (raw - zero) * sensitivity
    return clamp(int(offset), -127, 127)


# Mapping table: (cwiid button constant, HID button bit)
# Used by encode_buttons() to convert cwiid bitmask to HID bitmask.
_BUTTON_MAP = (
    (cwiid.BTN_LEFT,  0x01),  # bit 0: D-Pad Left
    (cwiid.BTN_RIGHT, 0x02),  # bit 1: D-Pad Right
    (cwiid.BTN_UP,    0x04),  # bit 2: D-Pad Up
    (cwiid.BTN_DOWN,  0x08),  # bit 3: D-Pad Down
    (cwiid.BTN_A,     0x10),  # bit 4: A
    (cwiid.BTN_B,     0x20),  # bit 5: B
    (cwiid.BTN_1,     0x40),  # bit 6: Button 1
    (cwiid.BTN_2,     0x80),  # bit 7: Button 2
)


def encode_buttons(cwiid_buttons):
    """Convert cwiid button bitmask to our 8-bit HID button byte.

    Iterates over the mapping table and sets the corresponding HID bit
    for each pressed Wiimote button.
    """
    hid_buttons = 0
    for cwiid_btn, hid_bit in _BUTTON_MAP:
        if cwiid_buttons & cwiid_btn:
            hid_buttons |= hid_bit
    return hid_buttons


def build_report(x_axis, y_axis, buttons_byte):
    """Pack a 3-byte HID gamepad report."""
    return struct.pack(REPORT_FORMAT, x_axis, y_axis, buttons_byte)


# ---------------------------------------------------------------------------
# HID device writer
# ---------------------------------------------------------------------------

class HIDWriter:
    """Manages writing HID reports to a /dev/hidgX device file."""

    def __init__(self, device_path):
        self.device_path = device_path
        self._fd = None

    def open(self):
        """Open the HID gadget device for writing."""
        if self._fd is not None:
            return
        try:
            self._fd = open(self.device_path, "wb+", buffering=0)
            logger.info("Opened HID device: %s", self.device_path)
        except OSError as exc:
            logger.error("Cannot open %s: %s", self.device_path, exc)
            raise

    def write(self, report):
        """Write a raw HID report (bytes) to the device."""
        if self._fd is None:
            return
        try:
            self._fd.write(report)
        except OSError as exc:
            logger.warning("Write to %s failed: %s", self.device_path, exc)

    def release_all(self):
        """Send a zero report (all buttons released, axes centered)."""
        self.write(ZERO_REPORT)

    def close(self):
        """Send a release report and close the device."""
        if self._fd is not None:
            try:
                self.release_all()
                self._fd.close()
            except OSError:
                pass
            finally:
                self._fd = None
            logger.info("Closed HID device: %s", self.device_path)


# ---------------------------------------------------------------------------
# Player slot — manages one Wiimote + one HID output
# ---------------------------------------------------------------------------

class PlayerSlot:
    """Manages scanning, connecting, and forwarding for one Wiimote player."""

    def __init__(self, player_num, hidg_path):
        """
        Args:
            player_num: 0-based player index.
            hidg_path: Path to the HID gadget device (e.g. /dev/hidg0).
        """
        self.player_num = player_num
        self.player_label = f"P{player_num + 1}"
        self.hidg_path = hidg_path
        self.hid = HIDWriter(hidg_path)

        self._wiimote = None
        self._thread = None
        self._running = False
        self._acc_zero = DEFAULT_ACC_ZERO

    def start(self):
        """Start the player slot thread (scan + forward loop)."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name=f"player-{self.player_num}",
            daemon=True,
        )
        self._thread.start()
        logger.info("[%s] Slot started, scanning for Wiimote...", self.player_label)

    def stop(self):
        """Signal the player slot thread to stop."""
        self._running = False
        self._disconnect()

    def join(self, timeout=5):
        """Wait for the player slot thread to finish."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # --- internal ---

    def _run(self):
        """Main loop: scan for Wiimote, forward inputs, handle disconnect."""
        while self._running:
            # Phase 1: scan and connect
            wiimote = self._scan_for_wiimote()
            if wiimote is None:
                continue  # _running was set to False, or scan error

            self._wiimote = wiimote

            # Phase 2: configure the connected Wiimote
            try:
                self._configure_wiimote(wiimote)
            except Exception:
                logger.exception(
                    "[%s] Failed to configure Wiimote", self.player_label
                )
                self._disconnect()
                continue

            # Phase 3: open HID device and forward inputs
            try:
                self.hid.open()
            except OSError:
                self._disconnect()
                continue

            logger.info("[%s] Forwarding inputs -> %s", self.player_label, self.hidg_path)
            self._forward_loop(wiimote)

            # Phase 4: cleanup after disconnect
            self._disconnect()
            if self._running:
                logger.info("[%s] Wiimote disconnected, rescanning...", self.player_label)

    def _scan_for_wiimote(self):
        """Block until a Wiimote is found or the slot is stopped.

        Returns:
            A cwiid.Wiimote instance, or None if stopped / error.
        """
        while self._running:
            logger.info(
                "[%s] Waiting for Wiimote (press 1+2 on controller)...",
                self.player_label,
            )
            try:
                wiimote = cwiid.Wiimote()
                logger.info("[%s] Wiimote connected!", self.player_label)
                return wiimote
            except RuntimeError:
                # No Wiimote responded within cwiid's internal timeout (~2s)
                logger.debug("[%s] No Wiimote found, retrying...", self.player_label)
                time.sleep(SCAN_RETRY_DELAY)
        return None

    def _configure_wiimote(self, wiimote):
        """Set LEDs, enable reporting, rumble, and calibrate accelerometer."""
        # Set player LED
        wiimote.led = PLAYER_LEDS[self.player_num]

        # Enable button + accelerometer reporting
        wiimote.rpt_mode = cwiid.RPT_BTN | cwiid.RPT_ACC

        # Brief rumble to confirm connection
        wiimote.rumble = True
        time.sleep(CONNECT_RUMBLE_DURATION)
        wiimote.rumble = False

        # Calibrate accelerometer zero-point
        self._calibrate_accelerometer(wiimote)

    def _calibrate_accelerometer(self, wiimote):
        """Read the accelerometer calibration zero-point from the Wiimote."""
        try:
            # cwiid returns calibration as ((zero_x, zero_y, zero_z), (one_x, one_y, one_z))
            cal = wiimote.get_acc_cal(cwiid.EXT_NONE)
            self._acc_zero = cal[0]  # zero-gravity point
            logger.info(
                "[%s] Accelerometer calibrated: zero=(%d, %d, %d)",
                self.player_label,
                *self._acc_zero,
            )
        except Exception:
            self._acc_zero = DEFAULT_ACC_ZERO
            logger.warning(
                "[%s] Accelerometer calibration failed, using defaults",
                self.player_label,
            )

    def _forward_loop(self, wiimote):
        """Poll the Wiimote state and write HID reports at POLL_RATE_HZ."""
        while self._running:
            try:
                state = wiimote.state
            except Exception:
                # Wiimote disconnected or communication error
                logger.warning("[%s] Lost connection to Wiimote", self.player_label)
                break

            buttons = state.get("buttons", 0)

            # Check for disconnect combo: + and - pressed together
            if (buttons & cwiid.BTN_PLUS) and (buttons & cwiid.BTN_MINUS):
                logger.info("[%s] Disconnect combo pressed (+/-)", self.player_label)
                try:
                    wiimote.rumble = True
                    time.sleep(DISCONNECT_RUMBLE_DURATION)
                    wiimote.rumble = False
                except Exception:
                    pass
                break

            # Check for recalibrate combo: Home button
            if buttons & cwiid.BTN_HOME:
                logger.info("[%s] Recalibrating accelerometer (Home pressed)", self.player_label)
                self._calibrate_accelerometer(wiimote)
                # Don't send a report this frame — let the user hold still
                time.sleep(0.5)
                continue

            # Read accelerometer
            acc = state.get("acc", DEFAULT_ACC_ZERO)
            x_axis = acc_to_axis(acc[0], self._acc_zero[0])
            y_axis = acc_to_axis(acc[1], self._acc_zero[1])

            # Encode buttons
            hid_buttons = encode_buttons(buttons)

            # Build and send HID report
            report = build_report(x_axis, y_axis, hid_buttons)
            self.hid.write(report)

            time.sleep(POLL_INTERVAL)

    def _disconnect(self):
        """Clean up Wiimote connection and HID device."""
        self.hid.release_all()
        self.hid.close()

        if self._wiimote is not None:
            try:
                self._wiimote.rumble = False
                self._wiimote.led = 0
                self._wiimote.close()
            except Exception:
                pass
            self._wiimote = None


# ---------------------------------------------------------------------------
# Bridge daemon — manages all player slots
# ---------------------------------------------------------------------------

class WiimoteBridge:
    """Top-level daemon that manages multiple PlayerSlots."""

    def __init__(self, num_players=NUM_PLAYERS):
        self.num_players = num_players
        self.slots = []
        self._shutdown_event = threading.Event()

    def start(self):
        """Start all player slot threads and wait for shutdown signal."""
        logger.info(
            "Wiimote Bridge starting (%d player slots)", self.num_players
        )

        # Verify HID gadget devices exist
        for i in range(self.num_players):
            hidg = f"/dev/hidg{i}"
            if not os.path.exists(hidg):
                logger.error(
                    "%s not found — is the USB gadget configured? "
                    "Run setup_usb_gadget.sh first.",
                    hidg,
                )
                sys.exit(1)

        # Create and start player slots
        for i in range(self.num_players):
            slot = PlayerSlot(player_num=i, hidg_path=f"/dev/hidg{i}")
            self.slots.append(slot)
            slot.start()

        logger.info(
            "All slots active. Press 1+2 on Wiimotes to connect. "
            "Send SIGTERM or SIGINT to shut down."
        )

        # Wait for shutdown
        self._shutdown_event.wait()

    def shutdown(self):
        """Gracefully stop all player slots."""
        logger.info("Shutting down Wiimote Bridge...")
        for slot in self.slots:
            slot.stop()
        for slot in self.slots:
            slot.join(timeout=3)
        logger.info("Wiimote Bridge stopped.")
        self._shutdown_event.set()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_bridge_instance = None


def _signal_handler(signum, _frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s, initiating shutdown...", sig_name)
    if _bridge_instance is not None:
        _bridge_instance.shutdown()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Entry point for the Wiimote Bridge daemon."""
    global _bridge_instance

    # Register signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    bridge = WiimoteBridge(num_players=NUM_PLAYERS)
    _bridge_instance = bridge

    try:
        bridge.start()
    except KeyboardInterrupt:
        bridge.shutdown()
    except Exception:
        logger.exception("Fatal error in Wiimote Bridge")
        bridge.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()
