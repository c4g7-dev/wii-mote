# Wiimote-to-Android USB Gamepad Bridge

Turn a **Raspberry Pi Zero W** into a wireless Wiimote-to-USB gamepad adapter.  
The Pi connects to up to **2 Wiimotes** via Bluetooth and presents them to an Android phone as standard **USB HID gamepads** over the OTG port — no app or driver needed on Android.

```
Wiimote(s) ──[Bluetooth]──▶ Pi Zero W ──[USB OTG]──▶ Android Phone
                              (bridge)              (sees USB gamepads)
```

## Features

- **Headless operation** — starts automatically on boot via systemd
- **Auto-scanning** — continuously looks for Wiimotes; reconnects on disconnect
- **2-player support** — each Wiimote gets its own player LED and HID device
- **Accelerometer** — tilt is mapped to analog stick axes (X/Y)
- **Instant Android support** — Android natively recognizes USB HID gamepads
- **Recalibrate on the fly** — press Home to recalibrate the accelerometer zero-point
- **Clean disconnect** — press + and − together on the Wiimote

## Button Mapping

| Wiimote       | Gamepad Output   | HID Bit |
|---------------|------------------|---------|
| D-Pad Left    | Button 1         | bit 0   |
| D-Pad Right   | Button 2         | bit 1   |
| D-Pad Up      | Button 3         | bit 2   |
| D-Pad Down    | Button 4         | bit 3   |
| A             | Button 5         | bit 4   |
| B             | Button 6         | bit 5   |
| 1             | Button 7         | bit 6   |
| 2             | Button 8         | bit 7   |
| Tilt left/right | X Axis (analog)| byte 0  |
| Tilt fwd/back | Y Axis (analog)  | byte 1  |
| Home          | Recalibrate accel| —       |
| + and − together | Disconnect    | —       |

## Requirements

- Raspberry Pi Zero W (with Bluetooth)
- Raspberry Pi OS (Bookworm or Bullseye)
- Micro-USB **data** cable (not power-only)
- One or two Nintendo Wiimotes

## Installation

1. **Clone** this repo onto the Pi (or copy the files via SCP):

   ```bash
   git clone https://github.com/YOUR_USER/wii-mote.git
   cd wii-mote
   ```

2. **Run the installer** as root:

   ```bash
   sudo bash install.sh
   ```

   This will:
   - Install `python3-cwiid`, `bluetooth`, `bluez`, and other dependencies
   - Add `dtoverlay=dwc2` to boot config
   - Add `dwc2` and `libcomposite` kernel modules
   - Copy files to `/opt/wiimote-bridge/`
   - Install and enable systemd services
   - Set up udev rules for `/dev/hidg*`

3. **Reboot** the Pi (the installer will prompt you).

## Usage

1. Connect the Pi Zero W to your Android phone using a micro-USB data cable into the **data port** (the one closer to the center of the board, NOT the power-only port on the edge).

2. The bridge service starts automatically. Check status:

   ```bash
   systemctl status wiimote-bridge.service
   ```

3. **Press 1 + 2** on a Wiimote. The Pi will connect it:
   - Player 1: LED 1 lights up
   - Player 2: LED 2 lights up
   - A brief rumble confirms the connection

4. Open any game or gamepad tester app on Android — the Wiimote inputs will appear as a standard USB gamepad.

5. To **disconnect** a Wiimote: press **+ and −** together (brief rumble, then the slot starts scanning again).

6. To **recalibrate** the accelerometer: hold the Wiimote flat/still and press **Home**.

## Logs & Debugging

```bash
# Live logs
journalctl -u wiimote-bridge.service -f

# Check if HID gadget devices exist
ls -la /dev/hidg*

# Check gadget setup service
systemctl status wiimote-gadget.service

# Check Bluetooth
bluetoothctl show
```

## File Structure

```
wii-mote/
├── install.sh                          # One-step installer
├── wiimote_bridge.py                   # Main bridge daemon
├── scripts/
│   ├── setup_usb_gadget.sh            # Creates USB HID gadget (configfs)
│   └── teardown_usb_gadget.sh         # Removes USB HID gadget
├── systemd/
│   ├── wiimote-gadget.service         # Gadget setup on boot
│   └── wiimote-bridge.service         # Bridge daemon service
└── udev/
    └── 99-hidg.rules                  # /dev/hidg* permissions
```

## Uninstall

```bash
sudo systemctl stop wiimote-bridge.service
sudo systemctl stop wiimote-gadget.service
sudo systemctl disable wiimote-bridge.service
sudo systemctl disable wiimote-gadget.service
sudo rm /etc/systemd/system/wiimote-gadget.service
sudo rm /etc/systemd/system/wiimote-bridge.service
sudo rm /etc/udev/rules.d/99-hidg.rules
sudo rm -rf /opt/wiimote-bridge
sudo systemctl daemon-reload
```

Remove `dtoverlay=dwc2` from `/boot/firmware/config.txt` and `dwc2`/`libcomposite` from `/etc/modules` if no longer needed, then reboot.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `/dev/hidg0` doesn't exist | Check `systemctl status wiimote-gadget.service`. Ensure `dtoverlay=dwc2` is in boot config and you've rebooted. |
| Wiimote won't connect | Make sure Bluetooth is on (`bluetoothctl show`). Press 1+2 on Wiimote within a few seconds. Try moving closer to the Pi. |
| Android doesn't detect gamepad | Use the **data port** (inner micro-USB), not the power port. Use a data-capable cable. |
| High latency | Disable WiFi to free the shared BT/WiFi radio: `sudo rfkill block wifi` |
| Bridge keeps restarting | Check logs: `journalctl -u wiimote-bridge.service -e`. Usually a missing `python3-cwiid` package. |

## License

MIT
