#!/usr/bin/env python3
"""
Wiimote-to-USB Gamepad Bridge

A headless daemon that runs on a Raspberry Pi Zero W, continuously scanning
for Wiimotes via Bluetooth and forwarding their inputs to an Android device
as standard USB HID gamepads over the OTG port.

Supports up to 2 Wiimotes simultaneously. Each Wiimote maps to a separate
HID gamepad device (/dev/hidg0, /dev/hidg1). Android sees them as standard
USB gamepads — no drivers or apps needed on the Android side.

HID report format (4 bytes per gamepad):
    Byte 0: X axis (signed byte, -127..127) — accelerometer tilt left/right
    Byte 1: Y axis (signed byte, -127..127) — accelerometer tilt fwd/back
    Byte 2: Hat switch (low nibble) — D-Pad direction
        0=Up, 1=Up-Right, 2=Right, 3=Down-Right,
        4=Down, 5=Down-Left, 6=Left, 7=Up-Left, 8=None
    Byte 3: Buttons bitmask (low nibble)
        bit 0: A      → Android BUTTON_A
        bit 1: B      → Android BUTTON_B
        bit 2: Button 1 → Android BUTTON_C
        bit 3: Button 2 → Android BUTTON_X

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

# Module-level lock: only one thread may perform a Bluetooth scan at a time.
# The Pi Zero W's single HCI adapter cannot handle concurrent inquiry scans;
# without this, cwiid.Wiimote() calls from two threads collide and produce
# "Bluetooth name read error" failures.
_bt_scan_lock = threading.Lock()
POLL_RATE_HZ = 100
POLL_INTERVAL = 1.0 / POLL_RATE_HZ
SCAN_RETRY_DELAY = 2.0  # seconds between scan attempts
HIDG_WAIT_INTERVAL = 3.0  # seconds between checks for /dev/hidg* availability
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

# The 4-byte struct: signed byte (X), signed byte (Y), hat switch, buttons
REPORT_FORMAT = "<bbBB"
ZERO_REPORT = struct.pack(REPORT_FORMAT, 0, 0, 8, 0)  # hat=8 means no direction


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
# D-Pad is handled separately via hat switch — only face buttons here.
_BUTTON_MAP = (
    (cwiid.BTN_A,  0x01),  # bit 0: Button 1 → Android BUTTON_A
    (cwiid.BTN_B,  0x02),  # bit 1: Button 2 → Android BUTTON_B
    (cwiid.BTN_1,  0x04),  # bit 2: Button 3 → Android BUTTON_C
    (cwiid.BTN_2,  0x08),  # bit 3: Button 4 → Android BUTTON_X
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


def encode_hat_switch(cwiid_buttons):
    """Convert D-pad button state to HID hat switch value.

    Hat switch values (clockwise from north):
        0=Up, 1=Up-Right, 2=Right, 3=Down-Right,
        4=Down, 5=Down-Left, 6=Left, 7=Up-Left,
        8=Null (no direction pressed).
    """
    up = bool(cwiid_buttons & cwiid.BTN_UP)
    down = bool(cwiid_buttons & cwiid.BTN_DOWN)
    left = bool(cwiid_buttons & cwiid.BTN_LEFT)
    right = bool(cwiid_buttons & cwiid.BTN_RIGHT)

    if up and right:
        return 1
    if up and left:
        return 7
    if down and right:
        return 3
    if down and left:
        return 5
    if up:
        return 0
    if right:
        return 2
    if down:
        return 4
    if left:
        return 6
    return 8  # null — no direction


def build_report(x_axis, y_axis, hat_switch, buttons_byte):
    """Pack a 4-byte HID gamepad report."""
    return struct.pack(REPORT_FORMAT, x_axis, y_axis, hat_switch, buttons_byte)


# ---------------------------------------------------------------------------
# HID device writer
# ---------------------------------------------------------------------------

class HIDWriter:
    """Manages writing HID reports to a /dev/hidgX device file.

    Resilient to the device not existing (USB cable not connected) or
    disappearing mid-session (cable unplugged). Callers should use
    try_open() which never raises, and write() which silently drops
    reports when the device isn't available.
    """

    def __init__(self, device_path):
        self.device_path = device_path
        self._fd = None
        self._reopen_at = 0.0  # earliest time we may retry opening

    @property
    def is_open(self):
        """True if the device file is currently open."""
        return self._fd is not None

    def is_available(self):
        """Check if the HID gadget device file exists on disk."""
        return os.path.exists(self.device_path)

    def try_open(self):
        """Try to open the HID gadget device. Returns True on success.

        Never raises — returns False if the device doesn't exist or
        can't be opened. Respects a cooldown after write failures to
        avoid a fast open-fail-close loop. Safe to call repeatedly.
        """
        if self._fd is not None:
            return True
        if time.time() < self._reopen_at:
            return False
        if not self.is_available():
            return False
        try:
            self._fd = open(self.device_path, "wb+", buffering=0)
            logger.info("Opened HID device: %s", self.device_path)
            return True
        except OSError as exc:
            logger.debug("Cannot open %s: %s", self.device_path, exc)
            self._reopen_at = time.time() + HIDG_WAIT_INTERVAL
            return False

    def write(self, report):
        """Write a raw HID report (bytes) to the device.

        Silently drops the report if the device isn't open.
        Closes the device on write failure (e.g. USB cable unplugged)
        and sets a cooldown before retrying to avoid rapid retry loops.
        """
        if self._fd is None:
            return
        try:
            self._fd.write(report)
        except OSError as exc:
            logger.warning("Write to %s failed (USB disconnected?): %s", self.device_path, exc)
            self.close()
            self._reopen_at = time.time() + HIDG_WAIT_INTERVAL

    def release_all(self):
        """Send a zero report (all buttons released, axes centered)."""
        self.write(ZERO_REPORT)

    def close(self):
        """Send a release report and close the device."""
        if self._fd is not None:
            try:
                self._fd.write(ZERO_REPORT)
            except OSError:
                pass
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None
            logger.info("Closed HID device: %s", self.device_path)


# ---------------------------------------------------------------------------
# Player slot — manages one Wiimote + one HID output
# ---------------------------------------------------------------------------

class PlayerSlot:
    """Manages scanning, connecting, and forwarding for one Wiimote player.

    The slot is always scanning for a Wiimote via Bluetooth, regardless of
    whether USB (and thus /dev/hidgX) is available. When a Wiimote is
    connected, inputs are forwarded to the HID device if it exists; if USB
    is not connected, the Wiimote stays paired and inputs are silently
    dropped until the USB cable is plugged in.
    """

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
        self._usb_was_connected = False

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

            # Phase 3: forward inputs (handles USB not being ready yet)
            logger.info("[%s] Wiimote ready, forwarding inputs", self.player_label)
            self._forward_loop(wiimote)

            # Phase 4: cleanup after disconnect
            self._disconnect()
            if self._running:
                logger.info("[%s] Wiimote disconnected, rescanning...", self.player_label)

    def _scan_for_wiimote(self):
        """Block until a Wiimote is found or the slot is stopped.

        Uses a module-level lock so that only one player slot performs a
        Bluetooth inquiry scan at a time (the Pi Zero W's single HCI
        adapter cannot handle concurrent scans).

        Returns:
            A cwiid.Wiimote instance, or None if stopped / error.
        """
        while self._running:
            logger.info(
                "[%s] Waiting for Wiimote (press 1+2 on controller)...",
                self.player_label,
            )
            acquired = _bt_scan_lock.acquire(timeout=SCAN_RETRY_DELAY)
            if not acquired:
                # Other slot is scanning — wait and retry
                continue
            try:
                wiimote = cwiid.Wiimote()
                logger.info("[%s] Wiimote connected!", self.player_label)
                return wiimote
            except RuntimeError:
                # No Wiimote responded within cwiid's internal timeout (~2s)
                logger.debug("[%s] No Wiimote found, retrying...", self.player_label)
            finally:
                _bt_scan_lock.release()
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
        """Poll the Wiimote state and write HID reports at POLL_RATE_HZ.

        Resilient to USB cable state changes:
        - If /dev/hidgX doesn't exist yet, keeps polling the Wiimote
          and periodically retries opening the HID device.
        - If a write fails (USB cable unplugged), closes the HID device
          and continues polling; will reopen when USB returns.
        """
        while self._running:
            try:
                state = wiimote.state
            except Exception:
                # Wiimote disconnected or communication error
                logger.warning("[%s] Lost connection to Wiimote", self.player_label)
                break

            buttons = state.get("buttons", 0)

            # Handle special button combos (disconnect, recalibrate)
            action = self._handle_special_combos(wiimote, buttons)
            if action == "disconnect":
                break
            if action == "recalibrate":
                continue

            # Build HID report from Wiimote state
            report = self._build_report_from_state(state, buttons)

            # Try to send via USB HID — resilient to USB not being connected
            self._send_report(report)

            time.sleep(POLL_INTERVAL)

    def _handle_special_combos(self, wiimote, buttons):
        """Check for special button combos. Returns action string or None."""
        # Disconnect combo: + and - pressed together
        if (buttons & cwiid.BTN_PLUS) and (buttons & cwiid.BTN_MINUS):
            logger.info("[%s] Disconnect combo pressed (+/-)", self.player_label)
            try:
                wiimote.rumble = True
                time.sleep(DISCONNECT_RUMBLE_DURATION)
                wiimote.rumble = False
            except Exception:
                pass
            return "disconnect"

        # Recalibrate combo: Home button
        if buttons & cwiid.BTN_HOME:
            logger.info("[%s] Recalibrating accelerometer (Home pressed)", self.player_label)
            self._calibrate_accelerometer(wiimote)
            time.sleep(0.5)
            return "recalibrate"

        return None

    def _build_report_from_state(self, state, buttons):
        """Extract accelerometer + buttons from Wiimote state into an HID report."""
        acc = state.get("acc", DEFAULT_ACC_ZERO)
        x_axis = acc_to_axis(acc[0], self._acc_zero[0])
        y_axis = acc_to_axis(acc[1], self._acc_zero[1])
        hat = encode_hat_switch(buttons)
        hid_buttons = encode_buttons(buttons)
        return build_report(x_axis, y_axis, hat, hid_buttons)

    def _send_report(self, report):
        """Send an HID report, handling USB connect/disconnect transitions."""
        if not self.hid.is_open:
            if self.hid.try_open():
                self._log_usb_state(connected=True)
                self._usb_was_connected = True
        if self.hid.is_open:
            self.hid.write(report)
            # If write failed, HIDWriter closes itself; detect on next call
            if not self.hid.is_open and self._usb_was_connected:
                self._log_usb_state(connected=False)
                self._usb_was_connected = False

    def _log_usb_state(self, connected):
        """Log USB HID connection state changes (avoids log spam)."""
        if connected:
            logger.info("[%s] USB connected, forwarding to %s", self.player_label, self.hidg_path)
        else:
            logger.info("[%s] USB disconnected, buffering Wiimote (will resume on reconnect)", self.player_label)

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

        # Log USB HID gadget status (informational, not a hard requirement)
        for i in range(self.num_players):
            hidg = f"/dev/hidg{i}"
            if os.path.exists(hidg):
                logger.info("%s available", hidg)
            else:
                logger.info(
                    "%s not found yet — Bluetooth scanning will start "
                    "anyway; USB forwarding begins when cable is connected",
                    hidg,
                )

        # Ensure adapter is pairable for classic Bluetooth (Wiimotes)
        try:
            os.system("bluetoothctl pairable on 2>/dev/null")
            logger.info("Bluetooth adapter set to pairable")
        except Exception:
            pass

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
