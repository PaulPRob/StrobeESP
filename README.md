# StrobeESP

LED strobe for the **Waveshare ESP32-C3-Zero**: flashes the onboard WS2812 RGB LED at
0.5–200 Hz with a 1–99 % duty cycle in any RGB colour, controlled from a PyQt6 desktop GUI
over the board's USB port.

| Folder | Contents |
|---|---|
| `led_strobe/` | ESP-IDF firmware (C). Text command protocol is documented at the top of `main/main.c`. |
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

## Hardware notes

- The C3-Zero's LED is a WS2812 on GPIO10 with **RGB** colour order (not the usual GRB).
- Each on/off phase must be ≥0.3 ms (WS2812 latch time), so at 200 Hz the usable duty range is 6–94 %.
- Channel levels between 0 and 255 are dimmed by the LED's internal PWM, which can flicker against
  short on-times; full-on/off channels are unaffected.
- Opening the USB serial port resets the board; settings are saved in flash and restored at boot.
