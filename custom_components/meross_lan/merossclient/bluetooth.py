import asyncio
import binascii
import logging
from typing import TYPE_CHECKING

from bleak import BleakClient, uuids
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus

from . import MerossDeviceDescriptor
from .protocol import MerossError, const as mc, namespaces as mn
from .protocol.message import MerossRequest, MerossResponse, check_message_strict

if TYPE_CHECKING:
    from typing import (
        Final,
        Iterable,
        NotRequired,
        Self,
        TypedDict,
        Unpack,
    )

    from bleak.args.winrt import WinRTClientArgs
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.client import BaseBleakClient
    from bleak.backends.device import BLEDevice
    from bleak.backends.service import BleakGATTService

    from . import LoggerT
    from .protocol.namespaces import Namespace
    from .protocol.types import MerossPayloadType, MerossRequestType


BL_SERVICE_UUID = "0000a00a-0000-1000-8000-00805f9b34fb"
BL_SERVICE_CHAR_NOTIFY_UUID = "0000b003-0000-1000-8000-00805f9b34fb"
BL_SERVICE_CHAR_NOTIFY_DESCR_ENABLE_UUID = "00002902-0000-1000-8000-00805f9b34fb"
BL_SERVICE_CHAR_WRITE_UUID = "0000b002-0000-1000-8000-00805f9b34fb"

uuids.register_uuids(
    {
        BL_SERVICE_UUID: "Meross Protocol",
        BL_SERVICE_CHAR_NOTIFY_UUID: "Meross Protocol notify",
        BL_SERVICE_CHAR_WRITE_UUID: "Meross Protocol write",
    }
)


class BluetoothError(MerossError):
    pass


class BluetoothClient(BleakClient):

    if TYPE_CHECKING:

        class ConnectArgs(TypedDict, total=False):
            dangerous_use_bleak_cache: NotRequired[bool]
            timeout: NotRequired[float]

        class RequestArgs(TypedDict, total=False):
            timeout: NotRequired[float]

        loop: Final[asyncio.AbstractEventLoop]
        logger: LoggerT | None
        timeout: float | None

        # uuid: Final[str] # TODO: decide about uuid
        _connect_lock: Final[asyncio.Lock]
        _rx_frame_size: int
        _rx_future: asyncio.Future[MerossResponse] | None
        _tx_lock: Final[asyncio.Lock]
        _service: BleakGATTService | None
        _char_notify: BleakGATTCharacteristic
        _char_write: BleakGATTCharacteristic
        _mtu_size: int | None

        # patch method signatures typing
        async def __aenter__(self) -> Self:
            await super().__aenter__()
            return self

    __slots__ = (
        "loop",
        "logger",
        "timeout",
        "uuid",
        "_connect_lock",
        "_rx_buf",
        "_rx_frame_size",
        "_rx_future",
        "_tx_lock",
        "_service",
        "_char_notify",
        "_char_write",
        "_mtu_size",
    )

    def __init__(
        self,
        address_or_ble_device: "BLEDevice | str",
        services: "Iterable[str] | None" = (BL_SERVICE_UUID,),
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        logger: "LoggerT | None" = None,
        timeout: float = 20.0,
        winrt: "WinRTClientArgs" = {},
        backend: "type[BaseBleakClient] | None" = None,
        **kwargs,
    ):
        super().__init__(
            address_or_ble_device,
            self._disconnected_callback,
            services,
            timeout=timeout,
            winrt=winrt,
            backend=backend,
            **kwargs,
        )
        self.loop = loop or asyncio.get_running_loop()
        self.logger = logger
        self.timeout = timeout
        self._connect_lock = asyncio.Lock()
        self._rx_frame_size = 0
        self._rx_future = None
        self._tx_lock = asyncio.Lock()
        self._service = None
        self._char_notify = None  # type: ignore
        self._char_write = None  # type: ignore
        self._mtu_size = None

    # interface: BleakClient
    async def connect(self, **kwargs: "Unpack[ConnectArgs]") -> bool:

        async with asyncio.timeout(kwargs.get("timeout", self.timeout)):

            await self._connect_lock.acquire()
            try:
                if self.is_connected:
                    return True

                logger = self.logger

                await super().connect(**kwargs)
                # Bad patch for bluez mtu_size (bad code always needs bad approaches)
                # We'll cache the mtu_size assuming it will not change across reconnections
                _backend = self._backend
                if (
                    isinstance(_backend, BleakClientBlueZDBus)
                    and _backend._mtu_size is None
                ):
                    if self._mtu_size:
                        _backend._mtu_size = self._mtu_size
                    else:
                        try:
                            await _backend._acquire_mtu()
                            self._mtu_size = _backend._mtu_size
                        except Exception as e:
                            _backend._mtu_size = 128
                            if logger:
                                logger.log(
                                    logging.DEBUG,
                                    "%s(%s) in BleakClientBlueZDBus._acquire_mtu(). Defaulting to %i",
                                    type(e),
                                    str(e),
                                    _backend._mtu_size,
                                )

                if not self._service:
                    service = self.services.get_service(BL_SERVICE_UUID)
                    if not service:
                        raise BluetoothError(
                            "Meross bluetooth service unavailable", BL_SERVICE_UUID
                        )

                    _char_write = service.get_characteristic(BL_SERVICE_CHAR_WRITE_UUID)
                    if not _char_write:
                        raise BluetoothError(
                            "Meross bluetooth write characteristic unavailable",
                            BL_SERVICE_CHAR_WRITE_UUID,
                        )

                    _char_notify = service.get_characteristic(
                        BL_SERVICE_CHAR_NOTIFY_UUID
                    )
                    if not _char_notify:
                        raise BluetoothError(
                            "Meross bluetooth notify characteristic unavailable",
                            BL_SERVICE_CHAR_NOTIFY_UUID,
                        )

                    # ENABLE NOTIFY CHAR
                    write_enable_descr = _char_notify.get_descriptor(
                        BL_SERVICE_CHAR_NOTIFY_DESCR_ENABLE_UUID
                    )
                    if not write_enable_descr:
                        raise BluetoothError(
                            "Meross bluetooth notify enable descriptor unavailable",
                            BL_SERVICE_CHAR_NOTIFY_DESCR_ENABLE_UUID,
                        )

                    try:
                        # TODO: this might be not needed though (see https://github.com/kennedn/meross)
                        await self.write_gatt_descriptor(
                            write_enable_descr.handle, bytes([0x01, 0x00])
                        )
                    except Exception as e:
                        if logger:
                            logger.log(
                                logging.DEBUG,
                                "%s(%s) in write_gatt_descriptor to enable notify char",
                                type(e),
                                str(e),
                            )

                    self._service = service
                    self._char_notify = _char_notify
                    self._char_write = _char_write

                try:
                    await self._backend.start_notify(
                        self._char_notify, self._packet_handler
                    )
                except:
                    # nullify so next time we'll refresh
                    self._service = None
                    self._char_notify = None  # type: ignore
                    self._char_write = None  # type: ignore
                    raise

                return True
            except Exception as e:
                if logger:
                    logger.log(logging.DEBUG, "%s(%s) in connect", type(e), str(e))
                if self.is_connected:
                    await super().disconnect()
                raise e
            finally:
                self._connect_lock.release()

    async def disconnect(self) -> bool:
        async with self._connect_lock:
            if self.is_connected:
                try:
                    await self._backend.stop_notify(self._char_notify)
                except Exception as e:
                    if self.logger:
                        self.logger.log(
                            logging.DEBUG, "%s(%s) in stop_notify", type(e), str(e)
                        )
                await super().disconnect()
            return True

    # interface: self
    async def async_request_raw(
        self, request: str, /, **kwargs: "Unpack[RequestArgs]"
    ) -> MerossResponse:

        # TODO: maybe add a retry loop
        async with asyncio.timeout(kwargs.get("timeout", self.timeout)):

            await self._tx_lock.acquire()

            try:
                if not self.is_connected:
                    await self.connect(dangerous_use_bleak_cache=True, **kwargs)

                self._rx_future = self.loop.create_future()

                tx_frame = request.encode()
                tx_frame_size = len(tx_frame)
                crc32 = binascii.crc32(tx_frame)
                tx_frame = bytes(
                    (
                        0x55,
                        0xAA,
                        tx_frame_size // 256,
                        tx_frame_size % 256,
                        *tx_frame,
                        (crc32 >> 24) & 0xFF,
                        (crc32 >> 16) & 0xFF,
                        (crc32 >> 8) & 0xFF,
                        crc32 & 0xFF,
                        0xAA,
                        0x55,
                    )
                )
                chunk_size = self.mtu_size - 3
                for chunk in (
                    tx_frame[i : i + chunk_size]
                    for i in range(0, len(tx_frame), chunk_size)
                ):
                    await self.write_gatt_char(self._char_write, chunk, response=False)

                if logger := self.logger:
                    logger.log(logging.DEBUG, "Transmitted frame: %s", tx_frame)

                return await self._rx_future

            finally:
                self._rx_future = None
                self._tx_lock.release()

    async def async_request(
        self, *request: "Unpack[MerossRequestType]", **kwargs: "Unpack[RequestArgs]"
    ) -> MerossResponse:
        return await self.async_request_raw(
            MerossRequest(*request, "").json(), **kwargs
        )

    async def async_request_ns(
        self, ns: "Namespace", /, **kwargs: "Unpack[RequestArgs]"
    ) -> MerossResponse:
        return await self.async_request_raw(
            MerossRequest(*ns.request_default, "").json(), **kwargs
        )

    async def async_identify_device(self, *args, **kwargs: "Unpack[RequestArgs]"):
        ns_all_response = check_message_strict(
            await self.async_request_ns(mn.Appliance_System_All, **kwargs)
        )
        ns_ability_response = check_message_strict(
            await self.async_request_ns(mn.Appliance_System_Ability, **kwargs)
        )
        return MerossDeviceDescriptor(
            ns_all_response[mc.KEY_PAYLOAD] | ns_ability_response[mc.KEY_PAYLOAD]
        )

    def _disconnected_callback(self, client: BleakClient, /):
        a = self.logger and self.logger.log(
            logging.DEBUG, "Bluetooth device %s disconnected", self.address
        )

    def _packet_handler(self, data: bytearray, /):
        if logger := self.logger:
            logger.log(logging.DEBUG, "Received: %s", data)

        try:
            # TODO: improve framer resiliency ?
            if rx_frame_size := self._rx_frame_size:

                rx_frame = self._rx_buf + data
                rx_frame_len = len(rx_frame)

                if (
                    (rx_frame[-1] == 0x55)
                    and (rx_frame[-2] == 0xAA)
                    and (rx_frame_len == rx_frame_size)
                ):
                    self._rx_frame_size = 0
                    checksum = (
                        rx_frame[-3]
                        + (rx_frame[-4] << 8)
                        + (rx_frame[-5] << 16)
                        + (rx_frame[-6] << 24)
                    )
                    rx_frame = rx_frame[4:-6]
                    crc32 = binascii.crc32(rx_frame)
                    if crc32 == checksum:
                        self._frame_handler(rx_frame)
                    else:
                        if logger:
                            logger.log(logging.DEBUG, "Frame error: invalid checksum")
                    return

                if rx_frame_len > rx_frame_size:
                    self._rx_frame_size = 0
                    if logger:
                        logger.log(
                            logging.DEBUG,
                            "Frame error: received size = %i - expected size = %i",
                            rx_frame_len,
                            rx_frame_size,
                        )
                    return

                self._rx_buf = rx_frame
            else:
                try:
                    if (data[0] == 0x55) and (data[1] == 0xAA):
                        self._rx_frame_size = data[2] * 256 + data[3] + 10
                        self._rx_buf = data
                except IndexError:
                    if logger:
                        logger.log(logging.DEBUG, "Frame error: packet too short")
        except Exception as e:
            if logger:
                logger.log(
                    logging.WARNING, "%s(%s) in _packet_handler", type(e), str(e)
                )

    def _frame_handler(self, rx_frame: bytearray, /):
        if logger := self.logger:
            logger.log(logging.DEBUG, "Received frame: %s", rx_frame)

        if self._rx_future:
            self._rx_future.set_result(MerossResponse(rx_frame.decode()))
