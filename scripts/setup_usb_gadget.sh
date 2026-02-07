#!/bin/bash
# setup_usb_gadget.sh — Create USB HID composite gadget with 2 gamepad functions
# This script configures the Pi Zero W as a USB HID gamepad adapter via configfs.
# It creates 2 HID endpoints (/dev/hidg0, /dev/hidg1) for 2 Wiimote players.
#
# Each gamepad HID report is 3 bytes:
#   Byte 0: X axis (signed, -127..127) — accelerometer tilt left/right
#   Byte 1: Y axis (signed, -127..127) — accelerometer tilt forward/back
#   Byte 2: Buttons (8-bit bitmask)
#       bit 0: D-Pad Left
#       bit 1: D-Pad Right
#       bit 2: D-Pad Up
#       bit 3: D-Pad Down
#       bit 4: A
#       bit 5: B
#       bit 6: Button 1
#       bit 7: Button 2

set -euo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/wiimote_gamepad"
NUM_PLAYERS=2

# HID Report Descriptor for a gamepad: 2 axes (signed byte each) + 8 buttons
# Decoded:
#   0x05, 0x01        Usage Page (Generic Desktop)
#   0x09, 0x05        Usage (Game Pad)
#   0xA1, 0x01        Collection (Application)
#   0x15, 0x81          Logical Minimum (-127)
#   0x25, 0x7F          Logical Maximum (127)
#   0x09, 0x01          Usage (Pointer)
#   0xA1, 0x00          Collection (Physical)
#   0x09, 0x30            Usage (X)
#   0x09, 0x31            Usage (Y)
#   0x75, 0x08            Report Size (8)
#   0x95, 0x02            Report Count (2)
#   0x81, 0x02            Input (Data, Var, Abs)
#   0xC0                End Collection
#   0x05, 0x09          Usage Page (Button)
#   0x19, 0x01          Usage Minimum (Button 1)
#   0x29, 0x08          Usage Maximum (Button 8)
#   0x15, 0x00          Logical Minimum (0)
#   0x25, 0x01          Logical Maximum (1)
#   0x75, 0x01          Report Size (1)
#   0x95, 0x08          Report Count (8)
#   0x81, 0x02          Input (Data, Var, Abs)
#   0xC0              End Collection
REPORT_DESC_HEX="05010905A1011581257F0901A10009300931750895028102C005091901290815002501750195088102C0"

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

log "Creating USB HID gamepad gadget..."

mkdir -p "${GADGET_DIR}"
cd "${GADGET_DIR}"

# USB device descriptor
echo 0x1d6b > idVendor        # Linux Foundation
echo 0x0104 > idProduct       # Multifunction Composite Gadget
echo 0x0100 > bcdDevice       # v1.0.0
echo 0x0200 > bcdUSB          # USB 2.0
echo 0xEF   > bDeviceClass    # Miscellaneous
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
    echo 3 > "${func}/report_length"
    echo "${REPORT_DESC_HEX}" | xxd -r -ps > "${func}/report_desc"
    ln -s "${func}" configs/c.1/
    log "Created HID function hid.usb${i}"
done

# Activate the gadget by binding to the UDC (USB Device Controller)
UDC_NAME=$(ls /sys/class/udc 2>/dev/null | head -1)
if [ -z "${UDC_NAME}" ]; then
    log "ERROR: No USB Device Controller found. Is dwc2 overlay enabled?"
    exit 1
fi
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
