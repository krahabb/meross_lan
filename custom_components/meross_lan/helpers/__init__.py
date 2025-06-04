"""
Helpers!
"""

import abc
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
import logging
from time import gmtime, time
import typing

from homeassistant import const as hac

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
    from typing import (
        Any,
        Final,
        NotRequired,
        TypedDict,
        Unpack,
    )


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


def versiontuple(version: str):
    """
    helper for version checking, comparisons, etc
    """
    return tuple(map(int, (version.split("."))))


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


def getLogger(name):
    """
    Replaces the default Logger with our wrapped implementation:
    replace your logging.getLogger with helpers.getLogger et voilà
    """
    logger = logging.getLogger(name)
    # watchout: getLogger could return an instance already
    # subclassed if we previously asked for the same name
    # for example when we reload a config entry
    _class = logger.__class__
    if _class not in _Logger._CLASS_HOOKS.values():
        # getLogger returned a 'virgin' class
        if _class in _Logger._CLASS_HOOKS.keys():
            # we've alread subclassed this type, so we reuse it
            logger.__class__ = _Logger._CLASS_HOOKS[_class]
        else:
            logger.__class__ = _Logger._CLASS_HOOKS[_class] = type(
                "Logger",
                (
                    _Logger,
                    logger.__class__,
                ),
                {},
            )

    return logger


class _Logger(logging.Logger if typing.TYPE_CHECKING else object):
    """
    This wrapper will 'filter' log messages and avoid
    verbose over-logging for the same message by using a timeout
    to prevent repeating the very same log before the timeout expires.
    The implementation 'hacks' a standard Logger instance by mixin-ing
    """

    # default timeout: these can be overriden at the log call level
    # by passing in the 'timeout=' param
    # for example: LOGGER.error("This error will %s be logged again", "soon", timeout=5)
    # it can also be overriden at the 'Logger' instance level
    default_timeout = 60 * 60 * 8
    # cache of logged messages with relative last-thrown-epoch
    _LOGGER_TIMEOUTS = {}
    # cache of subclassing types: see getLogger
    _CLASS_HOOKS = {}

    def _log(self, level, msg, args, **kwargs):
        if "timeout" in kwargs:
            timeout = kwargs.pop("timeout")
            epoch = time()
            trap_key = (msg, args)
            if trap_key in _Logger._LOGGER_TIMEOUTS:
                if (epoch - _Logger._LOGGER_TIMEOUTS[trap_key]) < timeout:
                    if self.isEnabledFor(mlc.CONF_LOGGING_VERBOSE):
                        super()._log(
                            mlc.CONF_LOGGING_VERBOSE,
                            f"dropped log message for {msg}",
                            args,
                            **kwargs,
                        )
                    return
            _Logger._LOGGER_TIMEOUTS[trap_key] = epoch

        super()._log(level, msg, args, **kwargs)


LOGGER = getLogger(__name__[:-8])  # get base custom_component name for logging
"""Root meross_lan logger"""


class Loggable(abc.ABC):
    """
    Helper base class for logging instance name/id related info.
    Derived classes can customize this in different flavours:
    - basic way is to override 'logtag' to provide a custom name when
    logging.
    - custom way by overriding 'log' like in 'Device' we can
    intercept log messages.
    """

    if typing.TYPE_CHECKING:
        id: Final[Any]
        logger: "Loggable | logging.Logger"

        class Args(TypedDict):
            logger: NotRequired["Loggable | logging.Logger"]

    hac = hac

    VERBOSE = mlc.CONF_LOGGING_VERBOSE
    DEBUG = mlc.CONF_LOGGING_DEBUG
    INFO = mlc.CONF_LOGGING_INFO
    WARNING = mlc.CONF_LOGGING_WARNING
    CRITICAL = mlc.CONF_LOGGING_CRITICAL

    __slots__ = ("id", "logtag", "logger")

    def __init__(self, id, **kwargs: "Unpack[Args]"):
        self.id = id
        self.logger = kwargs.get("logger", LOGGER)
        self.configure_logger()
        self.log(self.DEBUG, "init")

    def __repr__(self):
        return f"{self.__class__.__name__}({self.id})"

    def configure_logger(self):
        self.logtag = f"{self.__class__.__name__}({self.id})"

    def isEnabledFor(self, level: int):
        return self.logger.isEnabledFor(level)

    def log(self, level: int, msg: str, *args, **kwargs):
        self.logger.log(level, f"{self.logtag}: {msg}", *args, **kwargs)

    def log_exception(
        self, level: int, exception: Exception, msg: str, *args, **kwargs
    ):
        self.log(
            level,
            f"{exception.__class__.__name__}({str(exception)}) in {msg}",
            *args,
            **kwargs,
        )

    @contextmanager
    def exception_warning(self, msg: str, *args, **kwargs):
        try:
            yield
        except Exception as exception:
            self.log_exception(self.WARNING, exception, msg, *args, **kwargs)

    def __del__(self):
        self.log(self.DEBUG, "destroy")
