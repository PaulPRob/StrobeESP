#!/usr/bin/env python3
"""LED Strobe Controller - GUI for the led_strobe firmware on a Waveshare ESP32-C3-Zero.

Sets flash frequency (0.5-200 Hz), duty cycle (1-99 %) and colour of the board's onboard
RGB LED, over the board's USB cable or wirelessly over Bluetooth LE (PIN pairing; the PIN is
shown in the "Bluetooth PIN" dialog while connected by USB). All three colour channels flash
together.

Colour model: a base colour (preset or picker) is mixed towards white by the "White mix"
slider and scaled by "Brightness". Mixing is done in linear light and channel sliders are
in perceived brightness (gamma 2.2), so steps look even. Per-channel white-balance trims
correct for the LED's unequal red/green/blue output and are saved between sessions.

Run with:  uv run strobe_controller.py
"""
import math
import sys

from PyQt6.QtCore import QIODeviceBase, QRegularExpression, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QRegularExpressionValidator
from PyQt6.QtSerialPort import QSerialPort, QSerialPortInfo
from PyQt6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from ble_transport import BleTransport

APP_NAME = "LED Strobe Controller"
ESPRESSIF_VID = 0x303A
FW_REQUIRED = 2                   # major firmware version this GUI speaks to

FREQ_MIN, FREQ_MAX = 0.5, 200.0
DUTY_MIN, DUTY_MAX = 1.0, 99.0
GAMMA = 2.2
MIN_PULSE_MS = 0.3                # firmware's minimum on/off phase (WS2812 latch time)
DIM_FLICKER_MS = 10.0             # on-times shorter than this may beat with the LED's own PWM
SEND_DELAY_MS = 40                # coalesce rapid slider movement into one command
HANDSHAKE_INTERVAL_MS = 300
HANDSHAKE_TIMEOUT_MS = 5000       # board reboots when the USB port is opened
LINK_USB, LINK_BLE = "usb", "ble"

PRESETS = [("Red", (1, 0, 0)), ("Orange", (1, 0.5, 0)), ("Yellow", (1, 1, 0)), ("Green", (0, 1, 0)),
           ("Cyan", (0, 1, 1)), ("Blue", (0, 0, 1)), ("Magenta", (1, 0, 1)), ("White", (1, 1, 1))]
CHANNELS = (("Red", "#d32f2f"), ("Green", "#2e7d32"), ("Blue", "#1565c0"))


def to_linear(p: float) -> float:
    return max(0.0, p) ** GAMMA


def to_perceived(lin: float) -> float:
    return max(0.0, lin) ** (1 / GAMMA)


def css(rgb_perceived) -> str:
    r, g, b = (round(255 * min(1.0, c)) for c in rgb_perceived)
    return f"rgb({r},{g},{b})"


def parse_status(line: str) -> dict:
    """'OK F=2.000 D=50.00 RGB=255,0,0 ...' -> {'F': '2.000', 'D': '50.00', 'RGB': '255,0,0', ...}"""
    return dict(tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)


class SliderSpin(QWidget):
    """Slider and number box kept in sync. The slider can use a non-linear mapping."""
    valueChanged = pyqtSignal(float)

    def __init__(self, lo, hi, decimals, suffix, steps=1000, to_frac=None, from_frac=None, step=1.0):
        super().__init__()
        self.lo, self.hi, self.steps = lo, hi, steps
        self.to_frac = to_frac or (lambda v: (v - lo) / (hi - lo))
        self.from_frac = from_frac or (lambda f: lo + f * (hi - lo))
        self.slider = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=steps)
        self.spin = QDoubleSpinBox(minimum=lo, maximum=hi, decimals=decimals, suffix=suffix,
                                   singleStep=step, keyboardTracking=False)
        self.spin.setMinimumWidth(105)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._slider_moved)
        self.spin.valueChanged.connect(self._spin_changed)

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, v: float):
        """Set without emitting valueChanged."""
        for w, x in ((self.spin, v), (self.slider, round(self.steps * self.to_frac(v)))):
            w.blockSignals(True)
            w.setValue(x)
            w.blockSignals(False)

    def _slider_moved(self, s: int):
        v = self.from_frac(s / self.steps)
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self.valueChanged.emit(self.spin.value())

    def _spin_changed(self, v: float):
        self.slider.blockSignals(True)
        self.slider.setValue(round(self.steps * self.to_frac(v)))
        self.slider.blockSignals(False)
        self.valueChanged.emit(v)


def pin_line_edit() -> QLineEdit:
    edit = QLineEdit(maxLength=6, placeholderText="6 digits")
    edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}")))
    edit.setFont(QFont("monospace", 14))
    edit.setFixedWidth(130)
    return edit


class PinEntryDialog(QDialog):
    """Asks for the board's PIN when a computer pairs with it for the first time."""

    def __init__(self, parent, device_name):
        super().__init__(parent)
        self.setWindowTitle("Bluetooth pairing")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Enter the 6-digit PIN for <b>{device_name}</b>.<br>"
                             "<small>To see the PIN, connect by USB and click <i>Bluetooth PIN...</i></small>"))
        self.edit = pin_line_edit()
        lay.addWidget(self.edit, 0, Qt.AlignmentFlag.AlignHCenter)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setEnabled(False)
        self.edit.textChanged.connect(lambda t: self.ok.setEnabled(len(t) == 6))
        lay.addWidget(buttons)

    def pin(self):
        return int(self.edit.text()) if self.exec() == QDialog.DialogCode.Accepted else None


class PinManagerDialog(QDialog):
    """USB-only: show or change the board's Bluetooth PIN and remove pairings."""

    def __init__(self, parent, send):
        super().__init__(parent)
        self.send = send
        self.setWindowTitle("Bluetooth PIN")
        lay = QVBoxLayout(self)
        self.pin_label = QLabel("PIN: ...")
        self.pin_label.setFont(QFont("monospace", 16, QFont.Weight.Bold))
        self.pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.ble_label = QLabel("")
        self.ble_label.setWordWrap(True)
        lay.addWidget(QLabel("Enter this PIN on a computer the first time it connects over Bluetooth:"))
        lay.addWidget(self.pin_label)
        lay.addWidget(self.ble_label)
        row = QHBoxLayout()
        self.new_pin = pin_line_edit()
        self.set_btn = QPushButton("Set new PIN", enabled=False)
        self.new_pin.textChanged.connect(lambda t: self.set_btn.setEnabled(len(t) == 6))
        self.set_btn.clicked.connect(self._set_pin)
        row.addWidget(QLabel("New PIN:"))
        row.addWidget(self.new_pin)
        row.addWidget(self.set_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel("<small>Changing the PIN or removing pairings means every computer must "
                             "pair again with the PIN.</small>", wordWrap=True))
        bottom = QHBoxLayout()
        unpair = QPushButton("Remove all pairings")
        unpair.clicked.connect(self._unpair)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(unpair)
        bottom.addStretch(1)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        send("PIN")
        send("BLE")

    def _set_pin(self):
        self.send(f"PIN {self.new_pin.text()}")
        self.new_pin.clear()
        self.send("BLE")

    def _unpair(self):
        if QMessageBox.question(self, "Remove pairings",
                                "Remove all Bluetooth pairings from the board?") == QMessageBox.StandardButton.Yes:
            self.send("UNPAIR")

    def handle_line(self, line: str) -> bool:
        if line.startswith("PIN "):
            self.pin_label.setText(f"PIN: {line.split()[1]}")
            return True
        if line.startswith("BLE "):
            st = parse_status(line)
            self.ble_label.setText(f"Bluetooth name <b>{st.get('NAME', '?')}</b>, "
                                   f"{st.get('STATE', '?').replace('-', ', ')}, "
                                   f"{st.get('BONDS', '?')} paired computer(s)")
            return True
        return False


class StrobeController(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.settings = QSettings("esp32-tools", "led-strobe-controller")
        self.port = QSerialPort(self)
        self.port.readyRead.connect(self._on_ready_read)
        self.port.errorOccurred.connect(self._on_port_error)
        self.rx_buf = b""
        self.connected = False      # True once the firmware has answered ID
        self.link = None            # LINK_USB / LINK_BLE while a connection is open
        self.pin_dialog = None

        self.ble = BleTransport(self)
        self.ble.scan_finished.connect(self._ble_scan_finished)
        self.ble.connected.connect(self._ble_connected)
        self.ble.disconnected.connect(self._ble_disconnected)
        self.ble.line_received.connect(self._handle_line)
        self.ble.pin_requested.connect(self._ble_pin_requested)
        self.ble.status.connect(self.statusBar().showMessage)

        # colour state (perceived 0..1): base colour with max channel 1, white mix, brightness
        self.base = [1.0, 0.0, 0.0]

        self.send_timer = QTimer(self, singleShot=True, interval=SEND_DELAY_MS)
        self.send_timer.timeout.connect(self._send_settings)
        self.handshake_timer = QTimer(self, interval=HANDSHAKE_INTERVAL_MS)
        self.handshake_timer.timeout.connect(self._handshake_tick)
        self.handshake_elapsed = 0

        self._build_ui()
        self._link_changed()
        self._set_controls_enabled(False)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # Connection
        conn = QGroupBox("Connection")
        cl = QHBoxLayout(conn)
        self.link_combo = QComboBox()
        self.link_combo.addItem("USB cable", LINK_USB)
        self.link_combo.addItem("Bluetooth", LINK_BLE)
        self.link_combo.setCurrentIndex(max(0, self.link_combo.findData(self.settings.value("link", LINK_USB))))
        self.link_combo.currentIndexChanged.connect(self._link_changed)
        self.port_combo = QComboBox(minimumWidth=200)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.pin_btn = QPushButton("Bluetooth PIN...", enabled=False)
        self.pin_btn.setToolTip("Show or change the board's Bluetooth PIN (USB connection only)")
        self.pin_btn.clicked.connect(self._open_pin_manager)
        cl.addWidget(self.link_combo)
        cl.addWidget(self.port_combo, 1)
        cl.addWidget(self.refresh_btn)
        cl.addWidget(self.connect_btn)
        cl.addWidget(self.pin_btn)
        root.addWidget(conn)

        # Timing
        flash = QGroupBox("Flash timing")
        g = QGridLayout(flash)
        g.setColumnStretch(1, 1)
        log_span = math.log(FREQ_MAX / FREQ_MIN)
        self.freq = SliderSpin(FREQ_MIN, FREQ_MAX, 2, " Hz", step=0.5,
                               to_frac=lambda f: math.log(f / FREQ_MIN) / log_span,
                               from_frac=lambda x: (lambda f: round(f, 1) if f >= 10 else round(f, 2))(
                                   FREQ_MIN * math.exp(x * log_span)))
        self.duty = SliderSpin(DUTY_MIN, DUTY_MAX, 1, " %", steps=980)
        g.addWidget(QLabel("Frequency"), 0, 0)
        g.addWidget(self.freq, 0, 1)
        g.addLayout(self._scale_labels(["0.5", "2", "10", "50", "200 Hz"],
                                       [math.log(f / FREQ_MIN) / log_span for f in (0.5, 2, 10, 50, 200)]), 1, 1)
        g.addWidget(QLabel("Duty cycle"), 2, 0)
        g.addWidget(self.duty, 2, 1)
        g.addLayout(self._scale_labels(["1", "25", "50", "75", "99 %"],
                                       [(d - 1) / 98 for d in (1, 25, 50, 75, 99)]), 3, 1)
        self.run_btn = QPushButton("Flashing", checkable=True, minimumHeight=32)
        g.addWidget(self.run_btn, 4, 1)
        root.addWidget(flash)
        self.freq.valueChanged.connect(self._schedule_send)
        self.duty.valueChanged.connect(self._schedule_send)
        self.run_btn.toggled.connect(self._run_toggled)

        # Colour
        colour = QGroupBox("Colour")
        cg = QGridLayout(colour)
        cg.setColumnStretch(1, 1)

        presets = QHBoxLayout()
        presets.setSpacing(4)
        self.preset_btns = []
        for name, rgb in PRESETS:
            b = QPushButton(name, minimumHeight=28)
            fg = "black" if 0.3 * rgb[0] + 0.6 * rgb[1] + 0.1 * rgb[2] > 0.45 else "white"
            b.setStyleSheet(f"QPushButton {{ background: {css(rgb)}; color: {fg}; border: 1px solid #888;"
                            f" border-radius: 4px; padding: 2px 6px; }}"
                            f"QPushButton:disabled {{ background: #ddd; color: #999; }}")
            b.clicked.connect(lambda _=False, c=rgb: self._set_base(c))
            presets.addWidget(b)
            self.preset_btns.append(b)
        self.pick_btn = QPushButton("Pick…", minimumHeight=28)
        self.pick_btn.clicked.connect(self._pick_colour)
        presets.addWidget(self.pick_btn)
        cg.addWidget(QLabel("Base colour"), 0, 0)
        cg.addLayout(presets, 0, 1, 1, 2)

        self.mix = SliderSpin(0, 100, 0, " %", steps=100)
        self.mix_bar = QFrame(minimumHeight=10, maximumHeight=10)
        cg.addWidget(QLabel("White mix"), 1, 0)
        cg.addWidget(self.mix, 1, 1, 1, 2)
        mixbar_row = QHBoxLayout()
        mixbar_row.setContentsMargins(0, 0, 0, 0)
        mixbar_row.addWidget(self.mix_bar, 1)
        mixbar_row.addSpacing(111)
        cg.addLayout(mixbar_row, 2, 1, 1, 2)
        self.bright = SliderSpin(0, 100, 0, " %", steps=100)
        cg.addWidget(QLabel("Brightness"), 3, 0)
        cg.addWidget(self.bright, 3, 1, 1, 2)
        self.mix.valueChanged.connect(self._generators_changed)
        self.bright.valueChanged.connect(self._generators_changed)

        line = QFrame(frameShape=QFrame.Shape.HLine, frameShadow=QFrame.Shadow.Sunken)
        cg.addWidget(line, 4, 0, 1, 3)

        self.chan = []
        for i, (name, hexcol) in enumerate(CHANNELS):
            lbl = QLabel(name)
            lbl.setStyleSheet(f"color: {hexcol}; font-weight: bold;")
            ss = SliderSpin(0, 100, 0, " %", steps=100)
            ss.slider.setStyleSheet(f"QSlider::sub-page:horizontal {{ background: {hexcol}; }}")
            ss.valueChanged.connect(self._channel_changed)
            cg.addWidget(lbl, 5 + i, 0)
            cg.addWidget(ss, 5 + i, 1)
            self.chan.append(ss)
        self.swatch = QFrame()
        self.swatch.setFixedSize(70, 70)
        cg.addWidget(self.swatch, 5, 2, 3, 1, Qt.AlignmentFlag.AlignCenter)
        root.addWidget(colour)

        # White balance (persisted)
        wb = QGroupBox("White balance trim - lower the channel(s) that make White look tinted")
        wl = QHBoxLayout(wb)
        self.wb = []
        for i, (name, hexcol) in enumerate(CHANNELS):
            sp = QDoubleSpinBox(minimum=10, maximum=100, decimals=0, suffix=" %", singleStep=1,
                                keyboardTracking=False)
            sp.setValue(float(self.settings.value(f"wb/{i}", 100.0)))
            sp.valueChanged.connect(self._wb_changed)
            lbl = QLabel(name)
            lbl.setStyleSheet(f"color: {hexcol}; font-weight: bold;")
            wl.addWidget(lbl)
            wl.addWidget(sp)
            wl.addSpacing(12)
            self.wb.append(sp)
        wl.addStretch(1)
        root.addWidget(wb)

        # Board readback
        rb = QGroupBox("Board reports")
        rl = QVBoxLayout(rb)
        self.readback = QLabel("Not connected")
        self.readback.setFont(QFont("monospace"))
        self.readback.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #b26a00;")
        rl.addWidget(self.readback)
        rl.addWidget(self.note)
        root.addWidget(rb)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Select the board's port and press Connect")
        self.resize(640, 0)

        # sensible defaults before the board's state is known
        self.freq.setValue(2.0)
        self.duty.setValue(50.0)
        self.mix.setValue(0)
        self.bright.setValue(100)
        self.run_btn.setChecked(True)
        self._update_colour_widgets()

    @staticmethod
    def _scale_labels(texts, positions):
        """Row of small labels under a slider (approximate positions)."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 111, 0)
        prev = 0.0
        for text, pos in zip(texts, positions):
            row.addStretch(max(1, int((pos - prev) * 100)))
            lbl = QLabel(text)
            lbl.setStyleSheet("color: gray; font-size: 9pt;")
            row.addWidget(lbl)
            prev = pos
        return row

    def _set_controls_enabled(self, on: bool):
        for w in (self.freq, self.duty, self.run_btn, self.mix, self.bright, self.pick_btn,
                  *self.preset_btns, *self.chan):
            w.setEnabled(on)

    # ---------- colour model ----------
    def _channels_from_generators(self):
        """Base colour + white mix + brightness -> perceived channel levels 0..1."""
        m, b = self.mix.value() / 100, to_linear(self.bright.value() / 100)
        return [to_perceived(((1 - m) * to_linear(c) + m) * b) for c in self.base]

    def _update_colour_widgets(self, channels=None):
        if channels is None:
            channels = self._channels_from_generators()
        for ss, p in zip(self.chan, channels):
            ss.setValue(round(100 * p))
        self.swatch.setStyleSheet(f"background: {css(channels)}; border: 1px solid #888; border-radius: 6px;")
        pure, white = css(self.base), css((1, 1, 1))
        self.mix_bar.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {pure}, stop:1 {white});"
            f" border: 1px solid #aaa; border-radius: 3px;")

    def _set_base(self, rgb):
        top = max(rgb)
        if top <= 0:
            return
        self.base = [c / top for c in rgb]
        self.mix.setValue(0)
        if self.bright.value() == 0:
            self.bright.setValue(100)
        self._update_colour_widgets()
        self._schedule_send()

    def _pick_colour(self):
        start = QColor.fromRgbF(*self.base)
        c = QColorDialog.getColor(start, self, "Base colour")
        if c.isValid():
            self._set_base((c.redF(), c.greenF(), c.blueF()))

    def _generators_changed(self, _v=None):
        self._update_colour_widgets()
        self._schedule_send()

    def _channel_changed(self, _v=None):
        """Direct channel edit: it becomes the base colour, at that brightness, with no white mix."""
        self._adopt_channels([ss.value() / 100 for ss in self.chan])
        self._schedule_send()

    def _adopt_channels(self, p):
        top = max(p)
        if top > 0:
            self.base = [c / top for c in p]
        self.mix.setValue(0)
        self.bright.setValue(round(100 * top))
        self._update_colour_widgets(p)

    def _wb_changed(self, _v=None):
        for i, sp in enumerate(self.wb):
            self.settings.setValue(f"wb/{i}", sp.value())
        self._schedule_send()

    def _levels(self):
        """Perceived channel levels -> 0-255 values sent to the LED (linear, white-balanced)."""
        return [round(255 * to_linear(ss.value() / 100) * wb.value() / 100) for ss, wb in zip(self.chan, self.wb)]

    def _levels_to_perceived(self, levels):
        out = []
        for v, wb in zip(levels, self.wb):
            out.append(min(1.0, to_perceived(v / 255 / (wb.value() / 100))))
        return out

    # ---------- connection ----------
    def _selected_link(self):
        return self.link_combo.currentData()

    def _link_changed(self, _i=None):
        self.settings.setValue("link", self._selected_link())
        self.port_combo.clear()
        self.refresh_btn.setEnabled(True)
        self.connect_btn.setEnabled(True)
        self.refresh_btn.setText("Scan" if self._selected_link() == LINK_BLE else "Refresh")
        self._refresh_ports()

    def _refresh_ports(self):
        if self._selected_link() == LINK_BLE:
            self.refresh_btn.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.statusBar().showMessage("Scanning for LED-Strobe boards over Bluetooth...")
            self.ble.scan()
            return
        current = self.port_combo.currentData()
        self.port_combo.clear()
        ports = [p for p in QSerialPortInfo.availablePorts() if p.hasVendorIdentifier()]
        ports.sort(key=lambda p: p.vendorIdentifier() != ESPRESSIF_VID)   # Espressif boards first
        for p in ports:
            label = p.portName()
            if p.vendorIdentifier() == ESPRESSIF_VID:
                label += "  (ESP32 USB)"
            elif p.description():
                label += f"  ({p.description()})"
            self.port_combo.addItem(label, p.systemLocation())
        if current:
            idx = self.port_combo.findData(current)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
        if not ports:
            self.statusBar().showMessage("No USB serial ports found - is the board plugged in?")

    def _ble_scan_finished(self, found):
        if self._selected_link() != LINK_BLE or self.link is not None:
            return
        self.port_combo.clear()
        for name, address, rssi in found:
            self.port_combo.addItem(f"{name}  ({rssi} dBm)", (address, name))
        self.refresh_btn.setEnabled(True)
        self.connect_btn.setEnabled(True)
        self.statusBar().showMessage(f"Found {len(found)} board(s)" if found else
                                     "No LED-Strobe boards found - is the board powered and in range?")

    def _toggle_connection(self):
        if self.link is not None:
            self._disconnect("Disconnected")
            return
        target = self.port_combo.currentData()
        if not target:
            self.statusBar().showMessage("Nothing selected - press Scan" if self._selected_link() == LINK_BLE
                                         else "No port selected")
            return
        self.rx_buf = b""
        self.connected = False
        self.handshake_elapsed = 0
        self._set_connecting_ui(True)
        if self._selected_link() == LINK_BLE:
            self.link = LINK_BLE
            address, name = target
            self.ble.open(address, name)
            return
        path = target
        self.port.setPortName(path)
        self.port.setBaudRate(115200)    # ignored by USB-Serial/JTAG, but required by Qt
        if not self.port.open(QIODeviceBase.OpenModeFlag.ReadWrite):
            self._set_connecting_ui(False)
            self.statusBar().showMessage(f"Could not open {path}: {self.port.errorString()}")
            return
        self.link = LINK_USB
        self.port.setDataTerminalReady(False)
        self.port.setRequestToSend(False)
        self.handshake_timer.start()
        self.statusBar().showMessage(f"Opened {path}, waiting for the board...")

    def _set_connecting_ui(self, busy: bool):
        self.connect_btn.setText("Disconnect" if busy else "Connect")
        for w in (self.link_combo, self.port_combo, self.refresh_btn):
            w.setEnabled(not busy)

    def _ble_connected(self, name):
        if self.link == LINK_BLE and not self.connected:
            self.statusBar().showMessage(f"Bluetooth link to {name} established, waiting for the board...")
            self.handshake_timer.start()

    def _ble_disconnected(self, reason):
        if self.link == LINK_BLE:
            self._disconnect(reason)

    def _ble_pin_requested(self, name):
        self.ble.provide_pin(PinEntryDialog(self, name).pin())

    def _open_pin_manager(self):
        self.pin_dialog = PinManagerDialog(self, self._write)
        self.pin_dialog.exec()
        self.pin_dialog = None

    def _disconnect(self, message: str):
        self.handshake_timer.stop()
        self.send_timer.stop()
        if self.link == LINK_BLE:
            self.ble.close()
        if self.port.isOpen():
            self.port.close()
        self.link = None
        self.connected = False
        self._set_controls_enabled(False)
        self.pin_btn.setEnabled(False)
        self._set_connecting_ui(False)
        self.readback.setText("Not connected")
        self.note.setText("")
        self.statusBar().showMessage(message)

    def _handshake_tick(self):
        self.handshake_elapsed += HANDSHAKE_INTERVAL_MS
        if self.handshake_elapsed > HANDSHAKE_TIMEOUT_MS:
            self._disconnect("No reply - is the led_strobe firmware on this board?")
            return
        self._write("ID")

    def _write(self, line: str):
        if self.link == LINK_BLE:
            self.ble.write(line)
        elif self.port.isOpen():
            self.port.write((line + "\n").encode())

    def _run_toggled(self, on: bool):
        self.run_btn.setText("Flashing - click to stop" if on else "Stopped - click to start")
        self._schedule_send()

    def _schedule_send(self, _v=None):
        if self.connected:
            self.send_timer.start()

    def _send_settings(self):
        r, g, b = self._levels()
        self._write(f"SET {self.freq.value():.3f} {self.duty.value():.2f} {r} {g} {b} "
                    f"{int(self.run_btn.isChecked())}")

    def _on_ready_read(self):
        self.rx_buf += bytes(self.port.readAll())
        while b"\n" in self.rx_buf:
            raw, self.rx_buf = self.rx_buf.split(b"\n", 1)
            self._handle_line(raw.decode(errors="replace").strip())

    def _handle_line(self, line: str):
        if line.startswith("ID LEDSTROBE"):
            if self.connected:
                return
            self.handshake_timer.stop()
            version = line.split()[-1]
            if not version.split(".")[0].isdigit() or int(version.split(".")[0]) != FW_REQUIRED:
                self._disconnect(f"Board has firmware {version}; this GUI needs {FW_REQUIRED}.x")
                return
            self.connected = True
            where = (f"{self.port_combo.currentData()[1]} over Bluetooth" if self.link == LINK_BLE
                     else f"{self.port.portName()} over USB")
            self.statusBar().showMessage(f"Connected to {where} - firmware {version}")
            self.pin_btn.setEnabled(self.link == LINK_USB)
            self._write("?")     # adopt whatever the board is currently doing
        elif line.startswith("OK "):
            self._show_status(parse_status(line), adopt=not self.freq.isEnabled())
        elif self.pin_dialog is not None and self.pin_dialog.handle_line(line):
            pass
        elif line.startswith("ERR "):
            self.statusBar().showMessage(f"Board error: {line[4:]}")
            if self.pin_dialog is not None:
                QMessageBox.warning(self.pin_dialog, "Bluetooth PIN", line[4:])
        # anything else is boot/log output from the board; ignore it

    def _show_status(self, st: dict, adopt: bool):
        try:
            f, d, run = float(st["F"]), float(st["D"]), st["RUN"] == "1"
            levels = [int(x) for x in st["RGB"].split(",")]
            on_us, off_us, deff = int(st["ON_US"]), int(st["OFF_US"]), float(st["DEFF"])
        except (KeyError, ValueError):
            return
        if adopt:   # first status after connecting: load the board's settings into the controls
            self.freq.setValue(f)
            self.duty.setValue(d)
            self.run_btn.blockSignals(True)
            self.run_btn.setChecked(run)
            self.run_btn.blockSignals(False)
            self.run_btn.setText("Flashing - click to stop" if run else "Stopped - click to start")
            for w in (self.mix, self.bright):
                w.blockSignals(True)
            self._adopt_channels(self._levels_to_perceived(levels))
            for w in (self.mix, self.bright):
                w.blockSignals(False)
            self._set_controls_enabled(True)

        self.readback.setText(
            f"{f:8.3f} Hz   LED RGB {levels[0]:3d},{levels[1]:3d},{levels[2]:3d}   "
            f"{'running' if run else 'stopped'}\n"
            f"on {on_us / 1000:9.3f} ms   off {off_us / 1000:9.3f} ms   duty {deff:.2f} %")
        notes = []
        if abs(deff - d) > 0.05:
            lo = 100 * MIN_PULSE_MS / (1000 / f)
            notes.append(f"Duty limited to {deff:.1f} %: the WS2812 LED needs at least {MIN_PULSE_MS} ms per "
                         f"on/off phase, so at {f:g} Hz the duty range is {lo:.1f}-{100 - lo:.1f} %.")
        if any(0 < v < 255 for v in levels) and on_us / 1000 < DIM_FLICKER_MS:
            notes.append(f"On-time is {on_us / 1000:.2f} ms: channels below full level are dimmed by the "
                         f"LED's own PWM and may flicker or shift colour between flashes. Use longer "
                         f"on-times, or only 0 %/100 % channels, for clean short flashes.")
        self.note.setText("\n".join(notes))

    def _on_port_error(self, err):
        if err in (QSerialPort.SerialPortError.ResourceError, QSerialPort.SerialPortError.PermissionError) \
                and self.port.isOpen() and self.link == LINK_USB:
            self._disconnect(f"Connection lost: {self.port.errorString()}")

    def closeEvent(self, event):
        if self.port.isOpen():
            self.port.close()
        self.ble.shutdown()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = StrobeController()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
