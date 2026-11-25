"""Local Bluetooth setup routes, shared by the server and companion UI."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from app.i18n import error_detail
from app.printer.bluetooth import BluetoothService, address
from app.schemas import Input


class PairInput(Input):
    address: str = Field(min_length=17, max_length=17)

    @field_validator("address")
    @classmethod
    def valid_address(cls, value):
        return address(value)


def bluetooth_router(prefix, authorize):
    router = APIRouter(prefix=prefix)

    def execute(request, operation, *arguments):
        authorize(request)
        service: BluetoothService = request.app.state.bluetooth
        try:
            return getattr(service, operation)(*arguments)
        except OSError as error:
            raise HTTPException(503, error_detail(error)) from error

    @router.get("")
    def devices(request: Request):
        return execute(request, "devices")

    @router.post("/scan")
    def scan(request: Request):
        return execute(request, "devices", True)

    @router.post("/pair")
    def pair(body: PairInput, request: Request):
        return execute(request, "pair", body.address)

    return router
