"""
Helpers!
"""

from datetime import UTC, datetime
from enum import StrEnum
from time import gmtime
import typing

try:
    # HA core compatibility patch (these were likely introduced in 2024.9)
    from homeassistant.util.ssl import (
        get_default_context as get_default_ssl_context,
        get_default_no_verify_context as get_default_no_verify_ssl_context,
    )
except:

    def get_default_ssl_context() -> "ssl.SSLContext | None":
        """Return the default SSL context."""
        return None

    def get_default_no_verify_ssl_context() -> "ssl.SSLContext | None":
        """Return the default SSL context that does not verify the server certificate."""
        return None


from .. import const as mlc

if typing.TYPE_CHECKING:
    from datetime import tzinfo
    import ssl
    from typing import Any, Final, NotRequired, TypedDict, Unpack


def clamp(_value, _min, _max):
    """
    saturate _value between _min and _max
    """
    if _value >= _max:
        return _max
    elif _value <= _min:
        return _min
    else:
        return _value


def reverse_lookup(_dict: dict, value):
    """
    lookup the values in map (dict) and return
    the corresponding key
    """
    for _key, _value in _dict.items():
        if _value == value:
            return _key
    return None


def datetime_from_epoch(epoch, tz: "tzinfo | None"):
    """
    converts an epoch (UTC seconds) in a datetime.
    Faster than datetime.fromtimestamp with less checks
    and no care for milliseconds.
    If tz is None it'll return a naive datetime in UTC coordinates
    """
    y, m, d, hh, mm, ss, weekday, jday, dst = gmtime(epoch)
    if tz is UTC:
        return datetime(y, m, d, hh, mm, min(ss, 59), 0, tz)
    elif tz is None:
        return datetime(y, m, d, hh, mm, min(ss, 59), 0, UTC).replace(tzinfo=None)
    else:
        return datetime(y, m, d, hh, mm, min(ss, 59), 0, UTC).astimezone(tz)


class ConfigEntryType(StrEnum):
    UNKNOWN = "unknown"
    DEVICE = "device"
    PROFILE = "profile"
    HUB = "hub"

    @staticmethod
    def get_type_and_id(unique_id: str | None):
        match (unique_id or ".").split("."):
            case (mlc.DOMAIN,):
                return (ConfigEntryType.HUB, None)
            case (device_id,):
                return (ConfigEntryType.DEVICE, device_id)
            case ("profile", profile_id):
                return (ConfigEntryType.PROFILE, profile_id)
            case _:
                return (ConfigEntryType.UNKNOWN, None)
