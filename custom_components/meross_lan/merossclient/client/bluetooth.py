import asyncio
from binascii import crc32
from typing import TYPE_CHECKING, override

from bleak import BleakClient, uuids
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus

from . import AbstractClient
from ..exceptions import MerossTransportError

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

    from ..logging import LoggerType
    from ..protocol.message import MerossMessage

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


class BluetoothError(MerossTransportError):
    pass


class BluetoothFrameError(BluetoothError):
    pass


class BluetoothClient(AbstractClient, BleakClient):

    if TYPE_CHECKING:

        class Args(AbstractClient.Args):
            services: NotRequired[Iterable[str]]

        class ConnectArgs(AbstractClient.ConnectArgs):
            pass

        class RequestRawArgs(AbstractClient.RequestRawArgs):
            pass

        _connect_lock: Final[asyncio.Lock]
        _rx_frame_size: int
        _rx_future: asyncio.Future[bytearray] | None
        _tx_lock: Final[asyncio.Lock]
        _service: BleakGATTService | None
        _char_notify: BleakGATTCharacteristic
        _char_write: BleakGATTCharacteristic
        _mtu_size: int | None

        # patch method signatures typing
        async def __aenter__(self) -> Self:
            await super().__aenter__()
            return self

    TRANSPORT = AbstractClient.Transport.BLUETOOTH  # type: ignore[override]

    __slots__ = AbstractClient._calc_slots(
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
        parent: "LoggerType | None" = None,
        *,
        winrt: "WinRTClientArgs" = {},
        backend: "type[BaseBleakClient] | None" = None,
        **kwargs: "Unpack[Args]",
    ):
        BleakClient.__init__(
            self,
            address_or_ble_device,
            None,
            services=kwargs.pop("services", (BL_SERVICE_UUID,)),
            winrt=winrt,
            backend=backend,
        )
        AbstractClient.__init__(self, self.address, parent, **kwargs)
        self._connect_lock = asyncio.Lock()
        self._rx_frame_size = 0
        self._rx_future = None
        self._tx_lock = asyncio.Lock()
        self._service = None
        self._char_notify = None  # type: ignore
        self._char_write = None  # type: ignore
        self._mtu_size = None

    @override  # AbstractClient
    async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"):
        try:
            async with asyncio.Timeout(
                self.loop.time() + kwargs.get("timeout", self.timeout)
            ):
                async with self._connect_lock:
                    if self.is_connected:
                        return

                    # BEWARE: in HA the _backend is actively swapped and wrapped so
                    # we cannot rely on it being the same across reconnections.
                    await self.connect()
                    _backend = self._backend
                    # Bad patch for bluez mtu_size (bad code always needs bad approaches)
                    # We'll cache the mtu_size assuming it will not change across reconnections
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
                                self.log_exception(
                                    self.DEBUG,
                                    e,
                                    "BleakClientBlueZDBus._acquire_mtu(). Defaulting to %i",
                                    _backend._mtu_size,
                                )

                    if not self._service:
                        service = self.services.get_service(BL_SERVICE_UUID)
                        if not service:
                            raise BluetoothError(
                                self,
                                "Meross bluetooth service unavailable",
                                BL_SERVICE_UUID,
                            )
                        _char_write = service.get_characteristic(
                            BL_SERVICE_CHAR_WRITE_UUID
                        )
                        if not _char_write:
                            raise BluetoothError(
                                self,
                                "Meross bluetooth write characteristic unavailable",
                                BL_SERVICE_CHAR_WRITE_UUID,
                            )
                        _char_notify = service.get_characteristic(
                            BL_SERVICE_CHAR_NOTIFY_UUID
                        )
                        if not _char_notify:
                            raise BluetoothError(
                                self,
                                "Meross bluetooth notify characteristic unavailable",
                                BL_SERVICE_CHAR_NOTIFY_UUID,
                            )
                        # ENABLE NOTIFY CHAR
                        write_enable_descr = _char_notify.get_descriptor(
                            BL_SERVICE_CHAR_NOTIFY_DESCR_ENABLE_UUID
                        )
                        if not write_enable_descr:
                            raise BluetoothError(
                                self,
                                "Meross bluetooth notify enable descriptor unavailable",
                                BL_SERVICE_CHAR_NOTIFY_DESCR_ENABLE_UUID,
                            )
                        self._service = service
                        self._char_notify = _char_notify
                        self._char_write = _char_write

                    try:
                        await _backend.start_notify(
                            self._char_notify, self._packet_handler
                        )
                    except:
                        # nullify so next time we'll refresh
                        self._service = None
                        self._char_notify = None  # type: ignore
                        self._char_write = None  # type: ignore
                        raise

                    _backend.set_disconnected_callback(self.on_disconnect)

        except Exception as e:
            self.log_exception(self.WARNING, e, "async_connect")
            await self.disconnect()
            raise
        else:
            self.on_connect()

    @override  # AbstractClient
    async def async_disconnect(self):
        async with self._connect_lock:
            if self._backend:
                try:
                    await self._backend.stop_notify(self._char_notify)
                except Exception as e:
                    self.log_exception(self.DEBUG, e, "stop_notify")
                await self.disconnect()
            if self.is_connected:
                self.on_disconnect()

    @override  # AbstractClient
    async def async_request_raw(
        self, request: "MerossMessage", /, **kwargs: "Unpack[RequestRawArgs]"
    ):
        self.on_tx(request)
        try:
            async with asyncio.Timeout(
                self.loop.time() + kwargs.get("timeout", self.timeout)
            ):
                await self._tx_lock.acquire()

                if not self.is_connected:
                    await self.async_connect()

                tx_frame = request.json.encode()
                tx_frame_size = len(tx_frame)
                checksum = crc32(tx_frame)
                tx_frame = bytes(
                    (
                        0x55,
                        0xAA,
                        tx_frame_size // 256,
                        tx_frame_size % 256,
                        *tx_frame,
                        (checksum >> 24) & 0xFF,
                        (checksum >> 16) & 0xFF,
                        (checksum >> 8) & 0xFF,
                        checksum & 0xFF,
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
                # BEWARE: optimistic concurrency ?
                self._rx_frame_size = 0  # flush receive buffer
                self._rx_future = self.loop.create_future()
                self.log(self.VERBOSE, "Transmitted frame: %s", tx_frame)
                return self.on_rx_raw(await self._rx_future)

        except Exception as e:
            self.log_exception(self.WARNING, e, "async_request_raw")
            raise
        finally:
            self._rx_future = None
            try:
                self._tx_lock.release()
            except RuntimeError:
                pass  # lock was not acquired (unlikely), ignore

    def _packet_handler(self, data: bytearray, /):
        self.log(self.VERBOSE, "Received %s", data)
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
                    if crc32(rx_frame) == checksum:
                        self.log(self.VERBOSE, "Received frame %s", rx_frame)
                        if self._rx_future:
                            self._rx_future.set_result(rx_frame)
                    else:
                        self.log(self.DEBUG, "Frame error: invalid checksum")
                        if self._rx_future:
                            self._rx_future.set_exception(
                                BluetoothFrameError(self, "Received invalid checksum")
                            )
                    return

                if rx_frame_len > rx_frame_size:
                    self._rx_frame_size = 0
                    self.log(
                        self.DEBUG,
                        "Frame error: received size = %i - expected size = %i",
                        rx_frame_len,
                        rx_frame_size,
                    )
                    if self._rx_future:
                        self._rx_future.set_exception(
                            BluetoothFrameError(self, "Size mismatch")
                        )
                    return

                self._rx_buf = rx_frame
            else:
                try:
                    if (data[0] == 0x55) and (data[1] == 0xAA):
                        self._rx_frame_size = data[2] * 256 + data[3] + 10
                        self._rx_buf = data
                except IndexError:
                    self.log(self.DEBUG, "Frame error: packet too short")
                    if self._rx_future:
                        self._rx_future.set_exception(
                            BluetoothFrameError(self, "Packet too short")
                        )
        except Exception as e:
            self.log_exception(self.WARNING, e, "_packet_handler")
