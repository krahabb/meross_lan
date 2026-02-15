from typing import TYPE_CHECKING

from . import DeviceDescriptor
from .client import AbstractClient
from .protocol import (
    compute_wifix_password,
    const as mc,
    namespaces as mn,
)
from .protocol.message import MerossRequest

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Final,
        Generator,
        Iterable,
        Mapping,
        NotRequired,
        Protocol,
        TypedDict,
        Unpack,
    )

    from cloudapi import LatestVersionType

    from .logging import LoggerType
    from .protocol.message import MerossResponse
    from .protocol.namespaces import Namespace
    from .protocol.types import (
        JsonDict,
        JsonList,
        JsonMapping,
        MerossRequestType,
        VersionTupleType,
        config as mt_cf,
        control as mt_c,
        hub as mt_h,
        mcu as mt_m,
    )


class Device(AbstractClient):

    if TYPE_CHECKING:

        class Args(AbstractClient.Args):
            client: AbstractClient

        client: AbstractClient
        descriptor: DeviceDescriptor

    __SLOTS__ = ("descriptor", "client")

    def __init__(
        self, id, parent: "LoggerType | None" = None, **kwargs: "Unpack[Args]"
    ):
        self.client = kwargs.pop("client")  # type: ignore
        super().__init__(id, parent, **kwargs)
        self.async_request_raw = self.client.async_request_raw
