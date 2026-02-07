#!/bin/bash
# install.sh — Install the Wiimote-to-USB Gamepad Bridge on a Raspberry Pi Zero W
#
# This script:
#   1. Installs required system packages
#   2. Configures boot settings for USB OTG gadget mode
#   3. Copies bridge files to /opt/wiimote-bridge/
#   4. Installs and enables systemd services
#   5. Sets up udev rules
#   6. Prompts for reboot
#
# Run as root:
#   sudo bash install.sh

set -euo pipefail

INSTALL_DIR="/opt/wiimote-bridge"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

# Detect Pi model
if [ -f /proc/device-tree/model ]; then
    PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
    log_info "Detected board: ${PI_MODEL}"
else
    log_warn "Cannot detect board model. Proceeding anyway..."
    PI_MODEL="unknown"
fi

# ---------------------------------------------------------------------------
# Step 1: Install system packages
# ---------------------------------------------------------------------------

log_info "Installing required system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 \
    python3-cwiid \
    bluetooth \
    bluez \
    libbluetooth-dev \
    rfkill \
    xxd

log_info "System packages installed."

# ---------------------------------------------------------------------------
# Step 2: Configure boot for USB OTG gadget mode
# ---------------------------------------------------------------------------

# Determine config.txt location (Bookworm+ uses /boot/firmware/)
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG="/boot/config.txt"
else
    log_error "Cannot find boot config.txt"
    exit 1
fi

log_info "Boot config: ${BOOT_CONFIG}"

# Add dtoverlay=dwc2 under [all] section if not already there.
# We must check section-aware: the overlay may exist under [cm5] or another
# section but NOT under [all], which is where we need it.
if awk '/^\[all\]/,/^\[/' "${BOOT_CONFIG}" | grep -q "^dtoverlay=dwc2"; then
    log_info "dtoverlay=dwc2 already in [all] section of ${BOOT_CONFIG}"
else
    log_info "Adding dtoverlay=dwc2 under [all] in ${BOOT_CONFIG}"
    sed -i '/^\[all\]/a dtoverlay=dwc2' "${BOOT_CONFIG}"
fi

# Add dwc2 and libcomposite to /etc/modules if not present
MODULES_FILE="/etc/modules"
for module in dwc2 libcomposite; do
    if grep -q "^${module}$" "${MODULES_FILE}"; then
        log_info "Module '${module}' already in ${MODULES_FILE}"
    else
        log_info "Adding '${module}' to ${MODULES_FILE}"
        echo "${module}" >> "${MODULES_FILE}"
    fi
done

# ---------------------------------------------------------------------------
# Step 3: Copy bridge files to /opt/wiimote-bridge/
# ---------------------------------------------------------------------------

log_info "Installing bridge files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

# Copy main Python daemon
cp "${SCRIPT_DIR}/wiimote_bridge.py" "${INSTALL_DIR}/wiimote_bridge.py"
chmod 755 "${INSTALL_DIR}/wiimote_bridge.py"

# Copy setup/teardown scripts
cp "${SCRIPT_DIR}/scripts/setup_usb_gadget.sh" "${INSTALL_DIR}/setup_usb_gadget.sh"
cp "${SCRIPT_DIR}/scripts/teardown_usb_gadget.sh" "${INSTALL_DIR}/teardown_usb_gadget.sh"
chmod 755 "${INSTALL_DIR}/setup_usb_gadget.sh"
chmod 755 "${INSTALL_DIR}/teardown_usb_gadget.sh"

log_info "Bridge files installed."

# ---------------------------------------------------------------------------
# Step 4: Install and enable systemd services
# ---------------------------------------------------------------------------

log_info "Installing systemd services..."

cp "${SCRIPT_DIR}/systemd/wiimote-gadget.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/systemd/wiimote-bridge.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable wiimote-gadget.service
systemctl enable wiimote-bridge.service

log_info "Systemd services enabled."

# ---------------------------------------------------------------------------
# Step 5: Install udev rules
# ---------------------------------------------------------------------------

log_info "Installing udev rules..."
cp "${SCRIPT_DIR}/udev/99-hidg.rules" /etc/udev/rules.d/99-hidg.rules
udevadm control --reload-rules

log_info "Udev rules installed."

# ---------------------------------------------------------------------------
# Step 6: Enable Bluetooth service and unblock rfkill
# ---------------------------------------------------------------------------

log_info "Enabling Bluetooth service..."
systemctl enable bluetooth.service
systemctl start bluetooth.service 2>/dev/null || true

# Ensure Bluetooth is not soft-blocked by rfkill (common on Pi Zero W)
log_info "Unblocking Bluetooth via rfkill..."
rfkill unblock bluetooth 2>/dev/null || true
hciconfig hci0 up piscan 2>/dev/null || true

# Allow unbonded classic Bluetooth devices (required for Wiimote pairing via cwiid)
BT_INPUT_CONF="/etc/bluetooth/input.conf"
if [ -f "${BT_INPUT_CONF}" ]; then
    if grep -q "^#ClassicBondedOnly=true" "${BT_INPUT_CONF}"; then
        log_info "Setting ClassicBondedOnly=false in ${BT_INPUT_CONF}"
        sed -i 's/^#ClassicBondedOnly=true/ClassicBondedOnly=false/' "${BT_INPUT_CONF}"
    elif ! grep -q "^ClassicBondedOnly=false" "${BT_INPUT_CONF}"; then
        log_info "Adding ClassicBondedOnly=false to ${BT_INPUT_CONF}"
        sed -i '/^\[General\]/a ClassicBondedOnly=false' "${BT_INPUT_CONF}"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Wiimote Bridge installation complete!"
echo "=============================================="
echo ""
echo "  Files installed to:  ${INSTALL_DIR}/"
echo ""
echo "  Services:"
echo "    wiimote-gadget.service  (USB HID gadget setup)"
echo "    wiimote-bridge.service  (Wiimote scanner + forwarder)"
echo ""
echo "  After reboot:"
echo "    1. Connect Pi Zero W to Android via USB data cable"
echo "    2. Press 1+2 on a Wiimote to pair"
echo "    3. LED will show player number, brief rumble confirms"
echo "    4. Wiimote inputs appear as a USB gamepad on Android"
echo ""
echo "  Logs:  journalctl -u wiimote-bridge.service -f"
echo "  Status: systemctl status wiimote-bridge.service"
echo ""

read -rp "Reboot now to activate? [y/N] " answer
if [[ "${answer}" =~ ^[Yy]$ ]]; then
    log_info "Rebooting..."
    reboot
else
    log_warn "Please reboot manually before using the bridge."
fi
