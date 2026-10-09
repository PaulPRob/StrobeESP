"""Bluetooth LE transport for the LED Strobe Controller.

Talks to the led_strobe firmware's Nordic UART Service (same text protocol as USB) using
Bleak. Bleak runs on its own asyncio event loop in a background thread; results come back
to the Qt GUI thread through signals.

Pairing needs the board's 6-digit PIN (shown in the GUI's Bluetooth PIN dialog over USB):
  * Linux:   a BlueZ pairing agent registered by this process asks the GUI for the PIN.
  * Windows: WinRT custom pairing asks the GUI for the PIN. (Not tested; if it fails,
             pair the board once in Windows Bluetooth settings and connect again.)
  * macOS:   the system shows its own PIN dialog the first time a command is sent.
"""
import asyncio
import sys
import threading

from bleak import BleakClient, BleakScanner
from PyQt6.QtCore import QObject, pyqtSignal

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"     # we write commands here
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"     # board notifies replies here
NAME_PREFIX = "LED-Strobe"
AGENT_PATH = "/com/ledstrobe/agent"
PIN_TIMEOUT_S = 120


def _is_auth_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(k in text for k in ("auth", "not paired", "insufficient", "encrypt", "pair"))


class BleTransport(QObject):
    scan_finished = pyqtSignal(list)        # [(name, address, rssi)]
    connected = pyqtSignal(str)             # device name
    disconnected = pyqtSignal(str)          # reason
    line_received = pyqtSignal(str)
    pin_requested = pyqtSignal(str)         # device name; answer with provide_pin()
    status = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = None
        self._name = ""
        self._rx_buf = b""
        self._pin_future = None
        self._closing = False
        self._agent_registered = False
        self._pin_asked = False

    # ---------- called from the GUI thread ----------
    def scan(self, timeout: float = 4.0):
        self._submit(self._scan(timeout))

    def open(self, address: str, name: str):
        self._closing = False
        self._submit(self._connect(address, name))

    def close(self):
        self._closing = True
        self._submit(self._disconnect())

    def write(self, line: str):
        self._submit(self._write(line))

    def provide_pin(self, pin):
        """Answer a pin_requested signal with an int PIN, or None to cancel pairing."""
        fut = self._pin_future
        if fut is not None and not fut.done():
            self._loop.call_soon_threadsafe(fut.set_result, pin)

    def shutdown(self):
        if self._client is not None:
            asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ---------- asyncio side ----------
    def _submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        fut.add_done_callback(self._report_crash)

    def _report_crash(self, fut):
        if not fut.cancelled() and fut.exception() is not None:
            self.status.emit(f"Bluetooth error: {fut.exception()}")

    async def _scan(self, timeout):
        found = []
        try:
            results = await BleakScanner.discover(timeout=timeout, return_adv=True,
                                                  service_uuids=[NUS_SERVICE])
            for device, adv in results.values():
                name = adv.local_name or device.name or ""
                if name.startswith(NAME_PREFIX):
                    found.append((name, device.address, adv.rssi))
        except Exception as e:
            self.status.emit(f"Bluetooth scan failed: {e}")
        found.sort(key=lambda t: -t[2])
        self.scan_finished.emit(found)

    async def _ask_pin(self):
        self._pin_asked = True
        self._pin_future = self._loop.create_future()
        self.pin_requested.emit(self._name)
        try:
            return await asyncio.wait_for(self._pin_future, PIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pin_future = None

    async def _connect(self, address, name, retry_pairing=True):
        self._name = name
        self._rx_buf = b""
        self._pin_asked = False
        try:
            self.status.emit(f"Connecting to {name}...")
            client = BleakClient(address, disconnected_callback=self._on_bleak_disconnect)
            await client.connect(timeout=20)
            self._client = client
            await self._pair(client)
            await client.start_notify(NUS_TX, self._on_notify)
            # First write proves the link is authenticated (the RX characteristic requires it)
            await client.write_gatt_char(NUS_RX, b"ID\n", response=True)
        except Exception as e:
            await self._drop_client()
            # If we used a stored pairing (no PIN asked) and the board rejected it, the board has
            # forgotten us (PIN changed / pairings removed): forget it here too and pair again.
            text = str(e).lower()
            stale = (retry_pairing and not self._pin_asked and
                     (_is_auth_error(e) or "disconnected" in text) and "not found" not in text)
            if stale and await self._forget(address):
                self.status.emit("Stored pairing was rejected by the board - pairing again")
                return await self._connect(address, name, retry_pairing=False)
            self.disconnected.emit(self._explain(e))
            return
        self.connected.emit(name)

    async def _forget(self, address) -> bool:
        """Remove this computer's stored pairing with the board."""
        if sys.platform == "darwin":
            return False        # macOS has no API for this; remove it in System Settings
        try:
            await BleakClient(address).unpair()
            return True
        except Exception as e:
            self.status.emit(f"Could not remove old pairing: {e}")
            return False

    def _explain(self, e: Exception) -> str:
        text = str(e) or type(e).__name__
        if "AuthenticationFailed" in text or "AuthenticationCanceled" in text or "PairingCanceled" in text:
            return "Pairing failed - wrong PIN or pairing cancelled"
        if "AuthenticationRejected" in text:
            return "Pairing rejected by the board"
        return f"Bluetooth connection failed: {text}"

    async def _pair(self, client):
        if sys.platform.startswith("linux"):
            await self._register_bluez_agent()
            await client.pair()             # no-op if already paired; agent supplies the PIN
        elif sys.platform == "win32":
            await self._pair_windows(client)
        # macOS: the OS pairs (and asks for the PIN) on the first authenticated write

    async def _register_bluez_agent(self):
        if self._agent_registered:
            return
        from dbus_fast import Message, MessageType
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        bus = manager._bus      # BlueZ asks the agent on the connection that calls Pair()
        bus.export(AGENT_PATH, _make_bluez_agent(self))
        reply = await bus.call(Message(destination="org.bluez", path="/org/bluez",
                                       interface="org.bluez.AgentManager1", member="RegisterAgent",
                                       signature="os", body=[AGENT_PATH, "KeyboardOnly"]))
        if reply.message_type == MessageType.ERROR and "AlreadyExists" not in (reply.error_name or ""):
            raise RuntimeError(f"Could not register Bluetooth pairing agent: {reply.body}")
        self._agent_registered = True

    async def _pair_windows(self, client):
        from winrt.windows.devices.enumeration import (
            DeviceInformation, DevicePairingKinds, DevicePairingResultStatus)

        info = await DeviceInformation.create_from_id_async(
            client._backend._requester.device_information.id)
        if info.pairing.is_paired:
            return
        loop = self._loop

        def on_pairing_requested(sender, args):
            # Runs on a WinRT thread: ask the GUI and wait for the answer
            deferral = args.get_deferral()
            pin = asyncio.run_coroutine_threadsafe(self._ask_pin(), loop).result()
            if pin is not None:
                args.accept_with_pin(f"{pin:06d}")
            deferral.complete()

        custom = info.pairing.custom
        token = custom.add_pairing_requested(on_pairing_requested)
        try:
            result = await custom.pair_async(DevicePairingKinds.PROVIDE_PIN)
        finally:
            custom.remove_pairing_requested(token)
        if result.status not in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED):
            raise RuntimeError(f"AuthenticationFailed (Windows pairing status {result.status})")

    async def _write(self, line):
        if self._client is None or not self._client.is_connected:
            return
        try:
            await self._client.write_gatt_char(NUS_RX, (line + "\n").encode(), response=True)
        except Exception as e:
            self.status.emit(f"Bluetooth write failed: {e}")

    def _on_notify(self, _char, data: bytearray):
        self._rx_buf += bytes(data)
        while b"\n" in self._rx_buf:
            raw, self._rx_buf = self._rx_buf.split(b"\n", 1)
            self.line_received.emit(raw.decode(errors="replace").strip())

    def _on_bleak_disconnect(self, _client):
        if self._client is not None and not self._closing:
            self._client = None
            self.disconnected.emit("Bluetooth connection lost")

    async def _drop_client(self):
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    async def _disconnect(self):
        await self._drop_client()


def _make_bluez_agent(transport: "BleTransport"):
    """org.bluez.Agent1 implementation that asks the GUI for the passkey."""
    from dbus_fast.service import ServiceInterface, method

    class PinAgent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @method()
        async def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
            pin = await transport._ask_pin()
            if pin is None:
                from dbus_fast import DBusError
                raise DBusError("org.bluez.Error.Canceled", "Pairing cancelled")
            return pin

        @method()
        async def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
            pin = await transport._ask_pin()
            if pin is None:
                from dbus_fast import DBusError
                raise DBusError("org.bluez.Error.Canceled", "Pairing cancelled")
            return f"{pin:06d}"

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
            pass

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
            pass

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
            pass

        @method()
        def Cancel(self):
            fut = transport._pin_future
            if fut is not None and not fut.done():
                transport._loop.call_soon_threadsafe(fut.set_result, None)

        @method()
        def Release(self):
            pass

    return PinAgent()
