"""Native Linux BlueZ discovery, explicit pairing and an SPP byte transport.

The system Bluetooth daemon owns bonding and SDP channel discovery. Each print
connection has its own D-Bus client/profile; no shell commands, RFCOMM device
nodes, global default pairing agent, or guessed channel numbers are used.
"""

import asyncio
import os
import re
import socket
import sys
from concurrent.futures import Future
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import Event, Lock, Thread

from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import BusType, MessageType
from dbus_next.errors import DBusError
from dbus_next.message import Message as BusMessage
from dbus_next.service import ServiceInterface, method
from dbus_next.signature import Variant

from app.i18n import Message

SPP = "00001101-0000-1000-8000-00805f9b34fb"
DEVICE = "org.bluez.Device1"
ADAPTER = "org.bluez.Adapter1"


def address(value: str) -> str:
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
        raise ValueError(Message("printerMessages.bluetoothAddressInvalid"))
    return value.upper()


def failure(key: str = "bluetoothUnavailable") -> OSError:
    return OSError(Message(f"printerMessages.{key}"))


async def call(bus, path, interface, member, signature="", body=None, timeout=8):
    reply = await asyncio.wait_for(
        bus.call(
            BusMessage(
                destination="org.bluez",
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        ),
        timeout,
    )
    if reply is None or reply.message_type == MessageType.ERROR:
        raise failure()
    return reply.body


@asynccontextmanager
async def connection():
    if sys.platform != "linux" or Path("/.dockerenv").exists():
        raise failure("bluetoothNativeLinuxOnly")
    bus = MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True)
    try:
        await asyncio.wait_for(bus.connect(), 5)
        yield bus
    finally:
        bus.disconnect()
        with suppress(Exception):
            await asyncio.wait_for(bus.wait_for_disconnect(), 2)


async def objects(bus):
    result = await call(bus, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
    return {
        path: {
            interface: {key: value.value for key, value in properties.items()}
            for interface, properties in interfaces.items()
        }
        for path, interfaces in result[0].items()
    }


def is_printer(properties):
    return str(properties.get("Name", "")).startswith("PT-P710BT")


async def device(bus, wanted, *, paired=False):
    matches = [
        (path, entry[DEVICE])
        for path, entry in (await objects(bus)).items()
        if DEVICE in entry
        and entry[DEVICE].get("Address", "").upper() == wanted
        and is_printer(entry[DEVICE])
    ]
    matches.sort(key=lambda item: (not item[1].get("Paired", False), item[0]))
    if not matches or paired and not matches[0][1].get("Paired"):
        raise failure("bluetoothNotPaired" if paired else "bluetoothNotFound")
    return matches[0]


class PairingAgent(ServiceInterface):
    # Exported methods use D-Bus signatures, not Python forward-reference types.
    def __init__(self, path):
        super().__init__("org.bluez.Agent1")
        self.path = path

    def check(self, device_path):
        if device_path != self.path:
            raise DBusError("org.bluez.Error.Rejected", "Unexpected device")

    @method()
    def Release(self):
        pass

    @method()
    def Cancel(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore # noqa: F821
        self.check(device)
        # Brother's PT-P710BT user guide specifies this fixed legacy PIN.
        return "0000"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore # noqa: F821
        self.check(device)
        return 0

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # type: ignore # noqa: F821
        self.check(device)

    @method()
    def RequestAuthorization(self, device: "o"):  # type: ignore # noqa: F821
        self.check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # type: ignore # noqa: F821
        self.check(device)
        if uuid != SPP:
            raise DBusError("org.bluez.Error.Rejected", "Unexpected profile")


class BluetoothService:
    def __init__(self):
        self.lock = Lock()

    def run(self, operation, *args):
        if not self.lock.acquire(blocking=False):
            raise failure("bluetoothBusy")
        try:
            return asyncio.run(operation(*args))
        except OSError as error:
            if isinstance(error.args[0] if error.args else None, Message):
                raise
            raise failure() from error
        except (TimeoutError, DBusError, EOFError) as error:
            raise failure() from error
        finally:
            self.lock.release()

    def devices(self, scan=False):
        return self.run(self._devices, scan)

    async def _devices(self, scan):
        async with connection() as bus:
            entries = await objects(bus)
            adapters = sorted(
                path for path, entry in entries.items() if entry.get(ADAPTER, {}).get("Powered")
            )
            if not adapters:
                raise failure("bluetoothAdapterOff")
            if scan:
                started = []
                try:
                    for adapter in adapters:
                        await call(
                            bus,
                            adapter,
                            ADAPTER,
                            "SetDiscoveryFilter",
                            "a{sv}",
                            [{"Transport": Variant("s", "bredr")}],
                        )
                        await call(bus, adapter, ADAPTER, "StartDiscovery")
                        started.append(adapter)
                    await asyncio.sleep(5)
                    entries = await objects(bus)
                finally:
                    for adapter in started:
                        with suppress(Exception):
                            await call(bus, adapter, ADAPTER, "StopDiscovery", timeout=2)
            found = {}
            for entry in entries.values():
                info = entry.get(DEVICE, {})
                if not is_printer(info) or info.get("Adapter") not in adapters:
                    continue
                mac = address(info["Address"])
                item = {"address": mac, "name": info["Name"], "paired": bool(info.get("Paired"))}
                if mac not in found or item["paired"]:
                    found[mac] = item
            return sorted(found.values(), key=lambda item: (item["name"], item["address"]))

    def pair(self, wanted):
        return self.run(self._pair, address(wanted))

    async def _pair(self, wanted):
        async with connection() as bus:
            path, info = await device(bus, wanted)
            if not info.get("Paired"):
                agent_path = "/labelroom/pairing"
                bus.export(agent_path, PairingAgent(path))
                await call(
                    bus,
                    "/org/bluez",
                    "org.bluez.AgentManager1",
                    "RegisterAgent",
                    "os",
                    [agent_path, "NoInputNoOutput"],
                )
                try:
                    await call(bus, path, DEVICE, "Pair", timeout=20)
                except BaseException:
                    with suppress(Exception):
                        await call(bus, path, DEVICE, "CancelPairing", timeout=2)
                    raise
                finally:
                    with suppress(Exception):
                        await call(
                            bus,
                            "/org/bluez",
                            "org.bluez.AgentManager1",
                            "UnregisterAgent",
                            "o",
                            [agent_path],
                            timeout=2,
                        )
                    bus.unexport(agent_path)
            _, info = await device(bus, wanted, paired=True)
            if SPP not in info.get("UUIDs", []):
                raise failure("bluetoothProfileMissing")
            return {"address": wanted, "name": info["Name"], "paired": True}


class SerialProfile(ServiceInterface):
    def __init__(self, device_path, ready):
        super().__init__("org.bluez.Profile1")
        self.device_path, self.ready = device_path, ready
        self.stream = None

    def close(self):
        if self.stream is not None:
            with suppress(OSError):
                self.stream.shutdown(socket.SHUT_RDWR)
            self.stream.close()
            self.stream = None

    @method()
    def Release(self):
        self.close()

    @method()
    def RequestDisconnection(self, device: "o"):  # type: ignore # noqa: F821
        if device == self.device_path:
            self.close()

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):  # type: ignore # noqa: F821, F722
        if device != self.device_path or self.ready.done():
            os.close(fd)
            raise DBusError("org.bluez.Error.Rejected", "Unexpected connection")
        self.stream = socket.socket(fileno=fd)
        self.ready.set_result(self.stream)


class BluetoothTransport:
    def __init__(self, mac):
        self.address = address(mac)
        self.stream = None
        self.thread = None
        self.stop = Event()

    def open(self):
        if self.stream is not None:
            return
        # Never start a second owner while a timed-out connection is closing.
        if self.thread is not None and self.thread.is_alive():
            raise failure("bluetoothBusy")
        self.stop.clear()
        ready = Future()
        self.thread = Thread(target=self._run, args=(ready,), name="printer-bluetooth", daemon=True)
        self.thread.start()
        try:
            self.stream = ready.result(timeout=30)
        except BaseException:
            self.close()
            raise

    def _run(self, ready):
        try:
            asyncio.run(self._session(ready))
        except BaseException as error:
            if not ready.done():
                ready.set_exception(
                    error
                    if isinstance(error, OSError)
                    and isinstance(error.args[0] if error.args else None, Message)
                    else failure()
                )

    async def _session(self, ready):
        async with connection() as bus:
            path, _ = await device(bus, self.address, paired=True)
            connected = asyncio.get_running_loop().create_future()
            profile = SerialProfile(path, connected)
            profile_path = "/labelroom/serial"
            bus.export(profile_path, profile)
            try:
                await call(
                    bus,
                    "/org/bluez",
                    "org.bluez.ProfileManager1",
                    "RegisterProfile",
                    "osa{sv}",
                    [
                        profile_path,
                        SPP,
                        {
                            "Role": Variant("s", "client"),
                            "RequireAuthentication": Variant("b", True),
                        },
                    ],
                )
                await call(bus, path, DEVICE, "ConnectProfile", "s", [SPP], timeout=15)
                stream = await asyncio.wait_for(connected, 3)
                if not self.stop.is_set():
                    ready.set_result(stream)
                while not self.stop.is_set() and bus.connected:
                    await asyncio.sleep(0.05)
            finally:
                owned = profile.stream is not None
                profile.close()
                if owned:
                    with suppress(Exception):
                        await call(bus, path, DEVICE, "DisconnectProfile", "s", [SPP], timeout=2)
                bus.unexport(profile_path)

    def write(self, data: bytes) -> None:
        if self.stream is None:
            raise failure("bluetoothDisconnected")
        self.stream.settimeout(5)
        # A failed send is uncertain. Never reconnect or replay bytes here.
        self.stream.sendall(data)

    def read(self, size: int, timeout_seconds: float) -> bytes:
        if self.stream is None:
            raise failure("bluetoothDisconnected")
        self.stream.settimeout(timeout_seconds)
        data = self.stream.recv(size)
        if not data:
            raise failure("bluetoothDisconnected")
        return data

    def close(self) -> None:
        self.stop.set()
        stream, self.stream = self.stream, None
        if stream is not None:
            with suppress(OSError):
                stream.shutdown(socket.SHUT_RDWR)
            stream.close()
        if self.thread is not None:
            self.thread.join(timeout=3)
