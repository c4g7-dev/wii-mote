#!/bin/bash
# setup_usb_gadget.sh — Create USB HID composite gadget with 2 gamepad functions
# This script configures the Pi Zero W as a USB HID gamepad adapter via configfs.
# It creates 2 HID endpoints (/dev/hidg0, /dev/hidg1) for 2 Wiimote players.
#
# The script waits for the dwc2 USB Device Controller to become available
# (handles boot race conditions where the module hasn't loaded yet).
#
# Each gamepad HID report is 4 bytes:
#   Byte 0: X axis (signed, -127..127) — accelerometer tilt left/right
#   Byte 1: Y axis (signed, -127..127) — accelerometer tilt forward/back
#   Byte 2: Hat switch (low nibble, 0-7=direction, 8=null) + padding
#   Byte 3: Buttons (low nibble: bit0=A, bit1=B, bit2=1, bit3=2) + padding

set -euo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/wiimote_gamepad"
NUM_PLAYERS=2
UDC_WAIT_TIMEOUT=120  # seconds to wait for UDC to appear
UDC_POLL_INTERVAL=2   # seconds between UDC checks

# HID Report Descriptor for a gamepad:
#   2 axes (signed byte each) + hat switch (D-pad) + 4 buttons
#
# Report format (4 bytes):
#   Byte 0: X axis (signed, -127..127) — accelerometer tilt left/right
#   Byte 1: Y axis (signed, -127..127) — accelerometer tilt forward/back
#   Byte 2: Hat switch (low nibble, 0-7=direction, 8+=null) + padding
#   Byte 3: Buttons (low nibble: bit0=A, bit1=B, bit2=1, bit3=2) + padding
#
# Decoded:
#   0x05, 0x01        Usage Page (Generic Desktop)
#   0x09, 0x05        Usage (Game Pad)
#   0xA1, 0x01        Collection (Application)
#     0xA1, 0x00        Collection (Physical)
#       0x05, 0x01        Usage Page (Generic Desktop)
#       0x09, 0x30        Usage (X)
#       0x09, 0x31        Usage (Y)
#       0x15, 0x81        Logical Minimum (-127)
#       0x25, 0x7F        Logical Maximum (127)
#       0x75, 0x08        Report Size (8)
#       0x95, 0x02        Report Count (2)
#       0x81, 0x02        Input (Data, Var, Abs)
#     0xC0              End Collection
#     0x05, 0x01        Usage Page (Generic Desktop)
#     0x09, 0x39        Usage (Hat switch)
#     0x15, 0x00        Logical Minimum (0)
#     0x25, 0x07        Logical Maximum (7)
#     0x35, 0x00        Physical Minimum (0)
#     0x46, 0x3B, 0x01  Physical Maximum (315)
#     0x65, 0x14        Unit (Degrees)
#     0x75, 0x04        Report Size (4)
#     0x95, 0x01        Report Count (1)
#     0x81, 0x42        Input (Data, Var, Abs, Null)
#     0x75, 0x04        Report Size (4)   — padding
#     0x95, 0x01        Report Count (1)
#     0x81, 0x01        Input (Constant)
#     0x05, 0x09        Usage Page (Button)
#     0x19, 0x01        Usage Minimum (Button 1)
#     0x29, 0x04        Usage Maximum (Button 4)
#     0x15, 0x00        Logical Minimum (0)
#     0x25, 0x01        Logical Maximum (1)
#     0x75, 0x01        Report Size (1)
#     0x95, 0x04        Report Count (4)
#     0x81, 0x02        Input (Data, Var, Abs)
#     0x75, 0x01        Report Size (1)   — padding
#     0x95, 0x04        Report Count (4)
#     0x81, 0x01        Input (Constant)
#   0xC0              End Collection
REPORT_DESC_HEX="05010905A101A1000501093009311581257F750895028102C005010939150025073500463B01651475049501814275049501810105091901290415002501750195048102750195048101C0"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Check if gadget already exists
if [ -d "${GADGET_DIR}" ]; then
    log "Gadget already configured at ${GADGET_DIR}, skipping setup."
    exit 0
fi

# Ensure configfs is mounted
if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
fi

# Try to load kernel modules if not loaded yet
modprobe dwc2 2>/dev/null || true
modprobe libcomposite 2>/dev/null || true

# Wait for UDC to appear (dwc2 module may still be loading at boot)
log "Waiting for USB Device Controller (UDC) to appear..."
elapsed=0
UDC_NAME=""
while [ -z "${UDC_NAME}" ] && [ "${elapsed}" -lt "${UDC_WAIT_TIMEOUT}" ]; do
    UDC_NAME=$(ls /sys/class/udc 2>/dev/null | head -1)
    if [ -z "${UDC_NAME}" ]; then
        sleep "${UDC_POLL_INTERVAL}"
        elapsed=$((elapsed + UDC_POLL_INTERVAL))
    fi
done

if [ -z "${UDC_NAME}" ]; then
    log "ERROR: No USB Device Controller found after ${UDC_WAIT_TIMEOUT}s."
    log "  Check: dtoverlay=dwc2 in /boot/firmware/config.txt"
    log "  Check: dwc2 in /etc/modules"
    log "  Check: dmesg | grep dwc2"
    exit 1
fi

log "UDC found: ${UDC_NAME} (after ${elapsed}s)"

log "Creating USB HID gamepad gadget..."

mkdir -p "${GADGET_DIR}"
cd "${GADGET_DIR}"

# USB device descriptor
echo 0x1d6b > idVendor        # Linux Foundation
echo 0x0104 > idProduct       # Multifunction Composite Gadget
echo 0x0100 > bcdDevice       # v1.0.0
echo 0x0200 > bcdUSB          # USB 2.0
echo 0xEF   > bDeviceClass    # Misc Device (composite)
echo 0x02   > bDeviceSubClass # Common Class
echo 0x01   > bDeviceProtocol # Interface Association Descriptor

# Device strings
mkdir -p strings/0x409
echo "WiimoteBridge001"      > strings/0x409/serialnumber
echo "Raspberry Pi"          > strings/0x409/manufacturer
echo "Wiimote Gamepad Adapter" > strings/0x409/product

# Configuration
mkdir -p configs/c.1/strings/0x409
echo "Gamepad Configuration" > configs/c.1/strings/0x409/configuration
echo 0x80 > configs/c.1/bmAttributes  # Bus powered
echo 250  > configs/c.1/MaxPower      # 500mA (value is in 2mA units)

# Create HID functions for each player
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    func="functions/hid.usb${i}"
    mkdir -p "${func}"
    echo 0 > "${func}/protocol"
    echo 0 > "${func}/subclass"
    echo 4 > "${func}/report_length"
    echo "${REPORT_DESC_HEX}" | xxd -r -ps > "${func}/report_desc"
    ln -s "${func}" configs/c.1/
    log "Created HID function hid.usb${i}"
done

# Activate the gadget by binding to the UDC
echo "${UDC_NAME}" > UDC

log "Gadget activated on UDC: ${UDC_NAME}"

# Set permissions on /dev/hidgX devices
sleep 1
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    if [ -e "/dev/hidg${i}" ]; then
        chmod 666 "/dev/hidg${i}"
        log "/dev/hidg${i} ready (permissions set)"
    else
        log "WARNING: /dev/hidg${i} not found"
    fi
done

log "USB HID gamepad gadget setup complete (${NUM_PLAYERS} players)"
