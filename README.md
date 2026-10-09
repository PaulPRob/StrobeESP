# StrobeESP

LED strobe for the **Waveshare ESP32-C3-Zero**: flashes the onboard WS2812 RGB LED at
0.5–200 Hz with a 1–99 % duty cycle in any RGB colour, controlled from a PyQt6 desktop GUI
over the board's USB cable or wirelessly over Bluetooth LE.

| Folder | Contents |
|---|---|
| `led_strobe/` | ESP-IDF firmware (C). `main/main.c` has the strobe and the text command protocol (documented at the top); `main/ble_link.c` carries the same protocol over Bluetooth LE. |
| `strobe_gui/` | **LED Strobe Controller** – PyQt6 GUI (`strobe_controller.py`). |

## Firmware

Requires [ESP-IDF](https://docs.espressif.com/projects/esp-idf/) **v6.0.x** (built and tested with v6.0.3).

```bash
. $HOME/esp/esp-idf-v6.0.3/export.sh       # activate ESP-IDF
cd led_strobe
idf.py build                                # target esp32c3 is set in sdkconfig.defaults
idf.py -p /dev/ttyACM0 flash
```

The `espressif/led_strip` component is downloaded automatically at build time
(version pinned in `dependencies.lock`). On Linux your user needs to be in the `dialout` group
to access `/dev/ttyACM0`.

## GUI

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 and PyQt6 from `uv.lock`).

```bash
cd strobe_gui
uv run strobe_controller.py
```

See `strobe_gui/README.md` for usage notes.

## Bluetooth

The board advertises as `LED-Strobe-XXXX` and uses the Nordic UART Service, carrying the same
text commands as USB. Commands are only accepted over an encrypted link that was paired with the
board's **6-digit PIN**:

- The PIN is generated randomly on first boot and stored on the board (it is never in this repo).
  Connect by USB and click **Bluetooth PIN...** in the GUI to see or change it, or to remove all
  pairings. These actions are refused over Bluetooth.
- In the GUI choose **Bluetooth**, press **Scan**, then **Connect**. The first time, you are asked
  for the PIN; after that the computer reconnects without it.
- Linux: tested (BlueZ 5.85). The GUI supplies the PIN to BlueZ itself.
- Windows 10/11: uses the WinRT pairing API; **not yet tested**. If pairing fails, pair
  `LED-Strobe-XXXX` once in Windows *Settings → Bluetooth & devices* (enter the PIN there) and
  connect from the GUI again.
- macOS: **not yet tested**. macOS shows its own PIN prompt on first connection. If the board's
  PIN is changed, remove the board in *System Settings → Bluetooth* before reconnecting.
- One computer can be connected at a time; typical range is 10–30 m indoors. The board still
  needs power (a USB charger or power bank is fine).

## Hardware notes

- The C3-Zero's LED is a WS2812 on GPIO10 with **RGB** colour order (not the usual GRB).
- Each on/off phase must be ≥0.3 ms (WS2812 latch time), so at 200 Hz the usable duty range is 6–94 %.
- Channel levels between 0 and 255 are dimmed by the LED's internal PWM, which can flicker against
  short on-times; full-on/off channels are unaffected.
- Opening the USB serial port resets the board (and drops any Bluetooth connection); settings are
  saved in flash and restored at boot. Connecting over Bluetooth does not reset it.
- LED edges lag the timer by a constant ~350 µs (WS2812 frame + latch); jitter measured on the
  board is <100 µs normally and up to ~200 µs while Bluetooth commands are streaming
  (`STATS` command).
