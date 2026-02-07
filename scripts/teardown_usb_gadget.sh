#!/bin/bash
# teardown_usb_gadget.sh — Cleanly remove the USB HID composite gadget
# Run this before shutdown or when reconfiguring the gadget.

set -euo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/wiimote_gamepad"
NUM_PLAYERS=2

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

if [ ! -d "${GADGET_DIR}" ]; then
    log "No gadget found at ${GADGET_DIR}, nothing to tear down."
    exit 0
fi

cd "${GADGET_DIR}"

log "Tearing down USB HID gamepad gadget..."

# Deactivate: unbind from UDC
if [ -s UDC ]; then
    echo "" > UDC
    log "Unbound from UDC"
fi

# Remove symlinks from configuration
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    link="configs/c.1/hid.usb${i}"
    if [ -L "${link}" ]; then
        rm "${link}"
        log "Removed config link for hid.usb${i}"
    fi
done

# Remove configuration strings
rmdir configs/c.1/strings/0x409 2>/dev/null || true
rmdir configs/c.1 2>/dev/null || true

# Remove HID functions
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    func="functions/hid.usb${i}"
    if [ -d "${func}" ]; then
        rmdir "${func}"
        log "Removed function hid.usb${i}"
    fi
done

# Remove device strings and gadget directory
rmdir strings/0x409 2>/dev/null || true
cd /
rmdir "${GADGET_DIR}" 2>/dev/null || true

log "Gadget teardown complete"
