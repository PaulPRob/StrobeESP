# LED Strobe Controller

PyQt6 GUI for the `led_strobe` firmware (`../led_strobe`) on a Waveshare ESP32-C3-Zero.
Controls the onboard RGB LED: frequency 0.5–200 Hz, duty cycle 1–99 %, and any RGB colour
(all channels flash together). Pick a base colour, fade it towards white with **White mix**,
scale with **Brightness**, or set the Red/Green/Blue channels directly. Channel sliders are in
perceived brightness (gamma 2.2). The **White balance trim** (saved between sessions) evens out
the LED's red/green/blue output: choose White and lower whichever channel makes it look tinted.

    uv run strobe_controller.py

Notes
- Opening the port reboots the board (~0.5 s); settings are saved on the board and restored.
- The WS2812 LED needs ≥0.3 ms per on/off phase, so at high frequencies the achievable duty
  is limited (6–94 % at 200 Hz). The "Board reports" panel shows the duty actually produced.
- Channels below full level are dimmed by the LED's own PWM (~0.4–2 kHz), which can flicker
  against short on-times (<10 ms); the GUI warns when that applies.
- The firmware also accepts plain text commands (e.g. `SET 10 25 255 64 64 1`, `RGB 255 0 0`, `?`); see the header of
  `../led_strobe/main/main.c`.
