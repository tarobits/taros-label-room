"""Configured device construction; protocol choices remain inside printer adapters."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.i18n import Message
from app.printer.base import PrinterBackend
from app.printer.bluetooth import BluetoothTransport, address
from app.printer.brother import PRODUCT_ID, VENDOR_ID, BrotherPTP710BT, capabilities
from app.printer.registry import BluetoothConnection, PrinterDefinition, UsbConnection
from app.printer.usb import UsbTransport


class DeviceConfiguration(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    usb_serial: str | None = Field(default=None, min_length=1, max_length=128)
    transport: Literal["usb", "bluetooth"] = "usb"
    bluetooth_address: str | None = Field(default=None, max_length=17)
    resolution: Literal["normal", "high"] = "normal"
    timeout_seconds: float = Field(default=120, ge=5, le=600)
    feed_mm: float = Field(default=2, ge=2, le=127)
    leader_mm: float = Field(default=24.5, ge=0, le=200)
    cut_each: bool = True

    @model_validator(mode="after")
    def connection_settings(self):
        if self.bluetooth_address:
            self.bluetooth_address = address(self.bluetooth_address)
        else:
            self.bluetooth_address = None
        if self.transport == "bluetooth" and not self.bluetooth_address:
            raise ValueError(Message("printerMessages.bluetoothAddressRequired"))
        return self


def configuration_matches(driver: str, old: dict[str, Any], new: dict[str, Any]) -> bool:
    if driver != "brother-pt-p710bt":
        return old == new
    try:
        return DeviceConfiguration.model_validate(old) == DeviceConfiguration.model_validate(new)
    except ValueError:
        return False


def connection_only_change(driver: str, old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Whether switching links leaves all rendering and print settings unchanged."""
    if driver != "brother-pt-p710bt":
        return False
    try:
        previous = DeviceConfiguration.model_validate(old).model_dump()
        replacement = DeviceConfiguration.model_validate(new).model_dump()
    except ValueError:
        return False
    if previous["transport"] == replacement["transport"]:
        return False
    for key in ("transport", "usb_serial", "bluetooth_address"):
        previous.pop(key)
        replacement.pop(key)
    return previous == replacement


def configured_printer(
    driver: str, config: dict[str, Any]
) -> tuple[PrinterDefinition, PrinterBackend]:
    if driver != "brother-pt-p710bt":
        raise ValueError("Unsupported printer driver")
    config = DeviceConfiguration.model_validate(config).model_dump()
    connection = (
        BluetoothConnection(address=config["bluetooth_address"])
        if config["transport"] == "bluetooth"
        else UsbConnection(
            vendor_id=VENDOR_ID, product_id=PRODUCT_ID, serial=config.get("usb_serial")
        )
    )
    return (
        PrinterDefinition(
            "",
            "Brother PT-P710BT",
            "brother-pt-p710bt",
            connection,
            capabilities(high_resolution=config.get("resolution", "normal") == "high"),
        ),
        BrotherPTP710BT(
            BluetoothTransport(config["bluetooth_address"])
            if config["transport"] == "bluetooth"
            else UsbTransport(VENDOR_ID, PRODUCT_ID, config.get("usb_serial")),
            timeout=config.get("timeout_seconds", 120),
            feed_mm=config.get("feed_mm", 2),
            leader_mm=config.get("leader_mm", 24.5),
            cut_each=config.get("cut_each", True),
        ),
    )
