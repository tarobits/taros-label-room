import asyncio
import os
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app_client import TestClient
from dbus_next.constants import MessageType
from dbus_next.errors import DBusError
from dbus_next.signature import Variant
from test_access_security import accounts as accounts
from test_companion_station import local_station, unlock
from test_web_printers import create_app, setup

from app.models import Batch, LabelCopy, PrinterDevice, PrinterState, State
from app.printer import bluetooth as bt
from app.printer.brother import BrotherPTP710BT
from app.printer.factory import DeviceConfiguration, configured_printer
from app.printer.registry import BluetoothConnection, UsbConnection
from companion.web import create_app as companion_app

MAC = "AA:BB:CC:DD:EE:FF"
PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class Bluez:
    def __init__(self):
        self.paired = False
        self.connected = True
        self.calls = []
        self.exports = {}
        self.peers = []
        self.fail = None

    def export(self, path, interface):
        self.exports[path] = interface

    def unexport(self, path):
        self.exports.pop(path, None)

    async def call(self, message):
        self.calls.append(message.member)
        if self.fail == message.member:
            return SimpleNamespace(message_type=MessageType.ERROR, body=["private OS detail"])
        if message.member == "GetManagedObjects":
            body = [
                {
                    "/org/bluez/hci0": {bt.ADAPTER: {"Powered": Variant("b", True)}},
                    PATH: {
                        bt.DEVICE: {
                            "Address": Variant("s", MAC),
                            "Name": Variant("s", "PT-P710BT1234"),
                            "Adapter": Variant("o", "/org/bluez/hci0"),
                            "Paired": Variant("b", self.paired),
                            "UUIDs": Variant("as", [bt.SPP]),
                        }
                    },
                    "/org/bluez/hci0/dev_00_00_00_00_00_00": {
                        bt.DEVICE: {
                            "Address": Variant("s", "00:00:00:00:00:00"),
                            "Name": Variant("s", "Unrelated headphones"),
                            "Adapter": Variant("o", "/org/bluez/hci0"),
                        }
                    },
                }
            ]
        else:
            body = []
        if message.member == "Pair":
            agent = self.exports["/labelroom/pairing"]
            assert agent.RequestPinCode.__wrapped__(agent, PATH) == "0000"
            self.paired = True
        if message.member == "ConnectProfile":
            left, right = socket.socketpair()
            self.peers.append(right)
            self.exports["/labelroom/serial"].NewConnection(PATH, left.detach(), {})
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=body)


@pytest.fixture
def bluez(monkeypatch):
    fake = Bluez()

    @asynccontextmanager
    async def connection():
        fake.connected = True
        try:
            yield fake
        finally:
            fake.connected = False
            fake.exports.clear()

    monkeypatch.setattr(bt, "connection", connection)
    sleep = asyncio.sleep

    async def fast_scan(delay):
        await sleep(0 if delay == 5 else delay)

    monkeypatch.setattr(bt.asyncio, "sleep", fast_scan)
    yield fake
    for peer in fake.peers:
        peer.close()


def test_discovery_filters_printer_model_and_pairing_is_explicit_and_retryable(bluez):
    service = bt.BluetoothService()
    assert service.devices(True) == [{"address": MAC, "name": "PT-P710BT1234", "paired": False}]
    assert bluez.calls[-2:] == ["GetManagedObjects", "StopDiscovery"]
    assert "Pair" not in bluez.calls
    bluez.fail = "Pair"
    with pytest.raises(OSError, match="Bluetooth is unavailable"):
        service.pair(MAC)
    assert bluez.calls[-2:] == ["CancelPairing", "UnregisterAgent"]
    assert not bluez.exports
    bluez.fail = None
    assert service.pair(MAC)["paired"]
    assert not bluez.exports
    service.pair(MAC)
    assert bluez.calls.count("Pair") == 2


def test_management_is_serialized_and_unknown_targets_never_pair(bluez):
    service = bt.BluetoothService()
    service.lock.acquire()
    try:
        with pytest.raises(OSError, match="busy"):
            service.devices(True)
    finally:
        service.lock.release()
    with pytest.raises(OSError, match="not found"):
        service.pair("00:00:00:00:00:00")
    assert "Pair" not in bluez.calls
    assert service.devices()


def test_scan_cancellation_stops_discovery_and_releases_management_lock(bluez, monkeypatch):
    service = bt.BluetoothService()
    original = bt.asyncio.sleep

    async def cancel_scan(delay):
        if delay == 5:
            raise asyncio.CancelledError()
        await original(delay)

    monkeypatch.setattr(bt.asyncio, "sleep", cancel_scan)
    with pytest.raises(asyncio.CancelledError):
        service.devices(True)
    assert bluez.calls[-1] == "StopDiscovery"
    assert not bluez.exports
    monkeypatch.setattr(bt.asyncio, "sleep", original)
    assert service.devices(True)


def test_pair_timeout_cancels_only_selected_pairing_and_can_retry(bluez, monkeypatch):
    original = bluez.call

    async def timeout(message):
        if message.member == "Pair":
            raise TimeoutError()
        return await original(message)

    monkeypatch.setattr(bluez, "call", timeout)
    service = bt.BluetoothService()
    with pytest.raises(OSError):
        service.pair(MAC)
    assert bluez.calls[-2:] == ["CancelPairing", "UnregisterAgent"]
    assert not bluez.exports
    monkeypatch.setattr(bluez, "call", original)
    assert service.pair(MAC)["paired"]


def test_pairing_agent_rejects_other_devices_and_profiles():
    agent = bt.PairingAgent(PATH)
    with pytest.raises(DBusError):
        agent.RequestPinCode("/org/bluez/other")
    with pytest.raises(DBusError):
        agent.RequestConfirmation("/org/bluez/other", 123456)
    with pytest.raises(DBusError):
        agent.AuthorizeService(PATH, "unrelated-profile")


def test_bluetooth_stream_fragmentation_timeout_disconnect_and_cleanup(bluez):
    bluez.paired = True
    transport = bt.BluetoothTransport(MAC)
    transport.open()
    peer = bluez.peers[-1]
    transport.write(b"raster")
    assert peer.recv(20) == b"raster"
    peer.sendall(b"status")
    assert transport.read(2, 0.1) == b"st"
    assert transport.read(20, 0.1) == b"atus"
    with pytest.raises(TimeoutError):
        transport.read(20, 0.01)
    peer.close()
    with pytest.raises(OSError):
        transport.read(20, 0.1)
    with pytest.raises(OSError):
        transport.write(b"never replay this")
    assert bluez.calls.count("ConnectProfile") == 1
    transport.close()
    assert transport.thread is not None and not transport.thread.is_alive()
    assert not bluez.exports
    transport.open()
    transport.close()
    assert bluez.calls.count("ConnectProfile") == 2


def test_failed_connect_does_not_disconnect_another_client_and_can_retry(bluez):
    bluez.paired = True
    bluez.fail = "ConnectProfile"
    transport = bt.BluetoothTransport(MAC)
    with pytest.raises(OSError):
        transport.open()
    assert "DisconnectProfile" not in bluez.calls
    assert transport.thread is not None and not transport.thread.is_alive()
    bluez.fail = None
    transport.open()
    transport.close()


def test_unexpected_profile_connection_closes_received_descriptor():
    async def exercise():
        profile = bt.SerialProfile(PATH, asyncio.get_running_loop().create_future())
        left, right = socket.socketpair()
        fd = left.detach()
        try:
            with pytest.raises(DBusError):
                profile.NewConnection("/org/bluez/other", fd, {})
            with pytest.raises(OSError):
                os.fstat(fd)
            assert not profile.ready.done()
        finally:
            right.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "config",
    [
        {"transport": "bluetooth"},
        {"transport": "bluetooth", "bluetooth_address": "../wrong"},
        {"transport": "wifi"},
    ],
)
def test_invalid_connection_settings_fail_before_opening_hardware(config):
    with pytest.raises(ValueError):
        configured_printer("brother-pt-p710bt", config)


def test_usb_remains_default_and_bluetooth_uses_same_capabilities():
    usb, _ = configured_printer("brother-pt-p710bt", {})
    wireless, backend = configured_printer(
        "brother-pt-p710bt",
        {
            "transport": "bluetooth",
            "bluetooth_address": MAC.lower(),
        },
    )
    assert isinstance(usb.connection, UsbConnection)
    assert isinstance(wireless.connection, BluetoothConnection)
    assert wireless.connection.address == MAC
    assert usb.capabilities == wireless.capabilities
    assert isinstance(backend, BrotherPTP710BT)
    assert isinstance(backend.transport, bt.BluetoothTransport)
    assert backend.transport.thread is None
    assert DeviceConfiguration(bluetooth_address="").bluetooth_address is None


def test_main_pairing_permissions_csrf_and_malformed_targets(accounts, bluez):
    admin, alice, _ = accounts
    for endpoint, method in [("", "get"), ("/scan", "post"), ("/pair", "post")]:
        kwargs = {"json": {"address": MAC}} if endpoint == "/pair" else {}
        assert (
            getattr(alice, method)("/api/printer-bluetooth" + endpoint, **kwargs).status_code == 403
        )
    assert not bluez.calls
    assert admin.post("/api/printer-bluetooth/pair", json={"address": "bad"}).status_code == 422
    assert (
        admin.post(
            "/api/printer-bluetooth/pair", json={"address": MAC}, headers={"X-CSRF-Token": "bad"}
        ).status_code
        == 403
    )
    assert not bluez.calls
    assert admin.post("/api/printer-bluetooth/scan").status_code == 200
    assert admin.post("/api/printer-bluetooth/pair", json={"address": MAC}).json()["paired"]


def test_companion_pairing_requires_unlocked_current_operator(accounts, tmp_path, bluez):
    admin, _, _ = accounts
    settings, agent = local_station(admin, tmp_path)
    with TestClient(companion_app(settings, agent, start_agent=False)) as client:
        assert client.get("/api/station/bluetooth").status_code == 401
        unlock(client)
        assert (
            client.post(
                "/api/station/bluetooth/pair",
                json={"address": MAC},
                headers={"X-CSRF-Token": "bad"},
            ).status_code
            == 403
        )
        assert not bluez.calls
        assert client.post("/api/station/bluetooth/scan").status_code == 200
        assert client.post("/api/station/bluetooth/pair", json={"address": MAC}).json()["paired"]
        client.post("/api/station/lock")
        assert client.get("/api/station/bluetooth").status_code == 401


def test_companion_pairing_rechecks_permissions_and_requires_online_server(
    accounts, tmp_path, bluez
):
    from app.models import Role

    admin, _, _ = accounts
    settings, agent = local_station(admin, tmp_path, user="alice")
    with TestClient(companion_app(settings, agent, start_agent=False)) as client:
        unlock(client)
        assert client.post("/api/station/bluetooth/scan").status_code == 403
        assert not bluez.calls
        with admin.app.state.factory.begin() as session:
            role = session.get(Role, "alice-access")
            assert role is not None
            role.permissions = [
                *role.permissions,
                {"kind": "application", "permission": "printers.manage"},
            ]
        assert client.get("/api/station/bluetooth").status_code == 200
        bluez.calls.clear()
        last_contact = agent.last_contact
        agent.last_contact = 0
        assert client.post("/api/station/bluetooth/scan").status_code == 503
        agent.last_contact = last_contact
        with admin.app.state.factory.begin() as session:
            role = session.get(Role, "alice-access")
            assert role is not None
            role.permissions = [
                item for item in role.permissions if item.get("permission") != "printers.manage"
            ]
        assert client.post("/api/station/bluetooth/pair", json={"address": MAC}).status_code == 403
        assert not bluez.calls


def test_transport_switch_preserves_queued_work_and_rejects_active_or_changed_geometry(tmp_path):
    settings, _ = setup(tmp_path)
    with TestClient(create_app(settings, start_worker=False)) as client:
        printer = client.post("/api/printers", json={"name": "Desk", "usb_serial": "ONE"}).json()
        identifier = printer["id"]
        profile = client.post("/api/tape-profiles", json={"name": "White", "width_mm": 24}).json()
        cartridge = client.post(
            "/api/cartridges",
            json={
                "profile_id": profile["id"],
                "name": "Loaded",
                "nominal_length_mm": 8000,
                "remaining_mm": 8000,
            },
        ).json()
        with client.app.state.factory.begin() as session:
            state = session.query(PrinterState).filter_by(printer_id=identifier).one()
            state.active_cartridge_id = cartridge["id"]
            # Legacy USB configurations did not contain transport fields.
            legacy = session.get(PrinterDevice, identifier)
            legacy.config = {
                key: value
                for key, value in legacy.config.items()
                if key not in {"transport", "bluetooth_address"}
            }
        job = client.post(
            "/api/jobs",
            json={
                "destination_id": identifier,
                "tape_profile": profile["id"],
                "data": {"text": "Keep me"},
            },
        )
        assert job.status_code == 201, job.text
        config = {
            "name": "Desk",
            **printer["config"],
            "transport": "bluetooth",
            "bluetooth_address": MAC,
        }
        assert (
            client.put(
                f"/api/printers/{identifier}", json={"name": "Desk", **printer["config"]}
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/printers/{identifier}", json={**config, "resolution": "high"}
            ).status_code
            == 409
        )
        response = client.put(f"/api/printers/{identifier}", json=config)
        assert response.status_code == 200, response.text
        assert response.json()["id"] == identifier
        with client.app.state.factory() as session:
            assert session.query(LabelCopy).one().state == State.QUEUED
            assert session.get(PrinterDevice, identifier).owner_id == printer["owner_id"]
            assert (
                session.query(PrinterState)
                .filter_by(printer_id=identifier)
                .one()
                .active_cartridge_id
                == cartridge["id"]
            )
        config["transport"] = "usb"
        assert client.put(f"/api/printers/{identifier}", json=config).status_code == 200
        worker = client.app.state.fleet.workers[identifier]
        worker.printer_lock.acquire()
        try:
            config["transport"] = "bluetooth"
            assert client.put(f"/api/printers/{identifier}", json=config).status_code == 409
        finally:
            worker.printer_lock.release()
        assert client.put(f"/api/printers/{identifier}", json=config).status_code == 200
        with client.app.state.factory.begin() as session:
            session.add(
                Batch(
                    printer_id=identifier,
                    cartridge_id=cartridge["id"],
                    estimated_mm=60,
                    state=State.NEEDS_REVIEW,
                )
            )
        assert (
            client.put(
                f"/api/printers/{identifier}", json={**config, "transport": "usb"}
            ).status_code
            == 409
        )


def test_bluetooth_duplicates_are_case_insensitive_but_usb_registrations_are_independent(tmp_path):
    settings, _ = setup(tmp_path)
    with TestClient(create_app(settings, start_worker=False)) as client:
        config = {"name": "Wireless", "transport": "bluetooth", "bluetooth_address": MAC}
        assert client.post("/api/printers", json=config).status_code == 201
        assert (
            client.post(
                "/api/printers", json={**config, "bluetooth_address": MAC.lower()}
            ).status_code
            == 409
        )
        assert client.post("/api/printers", json={"name": "USB"}).status_code == 201


def test_companion_registration_and_transport_changes_sync_to_persistent_local_configuration(
    accounts, tmp_path
):
    from mock_backend import MockPrinterBackend
    from printer_factory import test_printer as factory

    from app.models import CompanionPrinter

    admin, _, _ = accounts
    settings, agent = local_station(admin, tmp_path)

    def simulated(driver, config):
        definition, _ = factory(driver, config)
        return definition, MockPrinterBackend()

    agent.backend_factory = simulated
    with TestClient(companion_app(settings, agent, start_agent=False)) as local:
        unlock(local)
        body = {
            "local_id": "wireless",
            "name": "Wireless",
            "driver": "brother-pt-p710bt",
            "configuration": {
                "transport": "bluetooth",
                "bluetooth_address": MAC,
                "usb_serial": "SAME-PRINTER",
            },
        }
        response = local.post("/api/station/printers", json=body)
        assert response.status_code == 201, response.text
        identifier = agent.identifiers["wireless"]
        agent.sync()
        config = {"name": "Wireless", **agent.configurations["wireless"][1]}
        for transport in ["usb", "bluetooth"]:
            config["transport"] = transport
            with admin.app.state.factory.begin() as session:
                link = session.get(CompanionPrinter, identifier)
                assert link is not None
                link.blocked = True
            assert admin.put(f"/api/printers/{identifier}", json=config).status_code == 409
            with admin.app.state.factory.begin() as session:
                link = session.get(CompanionPrinter, identifier)
                assert link is not None
                link.blocked = False
            assert admin.put(f"/api/printers/{identifier}", json=config).status_code == 200
            agent.sync()
            assert agent.identifiers["wireless"] == identifier
            assert agent.configurations["wireless"][1]["transport"] == transport
            persisted = next(item for item in agent.devices if item.local_id == "wireless")
            assert persisted.configuration.transport == transport


def test_bluetooth_batches_require_transport_aware_companions(accounts, tmp_path):
    from typing import Literal

    from pydantic import BaseModel, ValidationError
    from test_companion_execution import authorize, queued_remote

    from app.companion.companion_protocol import BatchEnvelope, WireLabel
    from app.models import CompanionBatch
    from companion.journal import Journal

    admin, _, _ = accounts
    _, headers, identifier, worker, work = queued_remote(admin)
    assert work["payload"]["version"] == 1
    labels = [WireLabel.model_validate(label).rendered() for label in work["payload"]["labels"]]
    # Exercise envelope generation with the same fixture's raster and device identity.
    with admin.app.state.factory.begin() as session:
        device = session.get(PrinterDevice, identifier)
        assert device is not None
        device.config = {**device.config, "transport": "bluetooth", "bluetooth_address": MAC}
        session.delete(session.get(CompanionBatch, work["payload"]["batch_id"]))
    worker.backend.submit_batch(work["payload"]["batch_id"], labels)
    [wireless] = admin.get("/api/companion/work", headers=headers).json()
    assert wireless["payload"]["version"] == 2

    class LegacyEnvelope(BaseModel):
        # The version gate in the old executor, independent of the new validator.
        version: Literal[1] = 1

    assert LegacyEnvelope.model_validate(work["payload"]).version == 1
    with pytest.raises(ValidationError):
        LegacyEnvelope.model_validate(wireless["payload"])
    with pytest.raises(ValidationError, match="requires batch protocol version 2"):
        BatchEnvelope.model_validate({**wireless["payload"], "version": 1})
    journal = Journal(tmp_path / "bluetooth-journal.db")
    envelope = authorize(admin, headers, wireless, journal)
    assert envelope.version == 2
