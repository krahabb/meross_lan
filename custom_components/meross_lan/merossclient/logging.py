"""
Logging utilities for merossclient library.
"""

import abc
from contextlib import contextmanager
import logging
from time import time
from typing import TYPE_CHECKING, override

from .obfuscate import OBFUSCATE_KEYS

if TYPE_CHECKING:
    from typing import Any, Callable, Final, NotRequired, Protocol, TypedDict, Unpack

    from .obfuscate import JsonMapping

    NOTSET: Final
    VERBOSE: Final
    DEBUG: Final
    INFO: Final
    WARNING: Final
    CRITICAL: Final

    class LoggerArgs(TypedDict):
        timeout: NotRequired[int]
        obfuscate: NotRequired[bool]
        uuid: NotRequired[str | None]
        server: NotRequired[str | None]
        userid: NotRequired[str | int | None]
        key: NotRequired[str | None]
        _message: NotRequired[JsonMapping]
        _header: NotRequired[JsonMapping]
        _payload: NotRequired[JsonMapping]
        _any: NotRequired[Any]

    class LoggerType(Protocol):
        """Protocol definition for logger-like instances used in the library."""

        def getEffectiveLevel(self) -> int: ...
        def isEnabledFor(self, level: int) -> bool: ...
        def log(
            self, level: int, msg: str, *args, **kwargs: Unpack[LoggerArgs]
        ) -> None: ...


NOTSET = logging.NOTSET
VERBOSE = 5
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
CRITICAL = logging.CRITICAL

Logger = logging.Logger
getLevelName = logging.getLevelName


def getLogger(name) -> "_Logger":
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

    return logger  # type: ignore[return-value]


_EXCLUDED_LOG_KWARGS = {"timeout", "obfuscate"}


def extract_obfuscated_kwargs(obfuscate: bool, kwargs: "LoggerArgs") -> tuple:
    """ """

    if obfuscate:

        def _obfuscate(key, value):
            try:
                return OBFUSCATE_KEYS[key](value)
            except KeyError:
                return value

        return tuple(
            _obfuscate(_key, _value)
            for _key, _value in kwargs.items()
            if _key not in _EXCLUDED_LOG_KWARGS
        )
    else:
        return tuple(
            kwargs[_key] for _key in kwargs if _key not in _EXCLUDED_LOG_KWARGS
        )


class _Logger(logging.Logger if TYPE_CHECKING else object):
    """
    TODO: move this feature to standard logging/filter/formatter
    This wrapper will 'filter' log messages to:
    - avoid verbose over-logging for the same message by using a timeout
        to prevent repeating the very same log before the timeout expires.
    - obfuscate sensitive data in log messages by looking for specific keys
        in the log call kwargs and obfuscating their values based on the
        key value and the OBFUSCATE_KEYS rules.
    The implementation 'hacks' a standard Logger instance by mixin-ing
    """

    if TYPE_CHECKING:

        def log(
            self, level: int, msg: str, *args, **kwargs: Unpack[LoggerArgs]
        ) -> None: ...

    # default timeout: these can be overriden at the log call level
    # by passing in the 'timeout=' param
    # for example: LOGGER.error("This error will %s be logged again", "soon", timeout=5)
    # it can also be overriden at the 'Logger' instance level
    default_timeout = 60 * 60 * 8
    # cache of logged messages with relative last-thrown-epoch
    _LOGGER_TIMEOUTS = {}
    # cache of subclassing types: see getLogger
    _CLASS_HOOKS = {}

    @override
    def _log(self, level, msg, args, **kwargs: "Unpack[LoggerArgs]"):

        obfuscate = kwargs.pop("obfuscate", True)

        try:
            timeout = kwargs.pop("timeout")
            epoch = time()
            trap_key = (msg, args)
            if trap_key in _Logger._LOGGER_TIMEOUTS:
                if (epoch - _Logger._LOGGER_TIMEOUTS[trap_key]) < timeout:
                    if self.isEnabledFor(VERBOSE):
                        super()._log(
                            VERBOSE,
                            f"dropped log message for {msg}",
                            args + extract_obfuscated_kwargs(obfuscate, kwargs),
                        )
                    return
            _Logger._LOGGER_TIMEOUTS[trap_key] = epoch
        except KeyError:
            pass

        super()._log(
            level,
            msg,
            args + extract_obfuscated_kwargs(obfuscate, kwargs),
        )


class Loggable(metaclass=abc.ABCMeta):
    """
    Helper base class for logging instance name/id related info.
    Derived classes can customize this in different flavours:
    - basic way is to override 'logtag' to provide a custom name when
    logging.
    - custom way by overriding 'log' like in 'Device' we can
    intercept log messages.
    """

    if TYPE_CHECKING:
        id: Final[Any]
        parent: Final[LoggerType]

        time: Final[Callable[[], float]]

        class Args(TypedDict):
            pass

    VERBOSE = VERBOSE
    DEBUG = DEBUG
    INFO = INFO
    WARNING = WARNING
    CRITICAL = CRITICAL

    __SLOTS__ = ("id", "logtag", "parent", "time")

    @staticmethod
    def abstract(func):
        """Decorator to mark methods as abstract, without using ABCMeta."""
        func.__isabstractmethod__ = True
        # func.__call__ = lambda *args, **kwargs: NotImplemented
        return abc.abstractmethod(func)

    @staticmethod
    def virtual(func):
        """Decorator to mark methods as virtual, without using ABCMeta."""
        func.__call__ = lambda *args, **kwargs: None
        return func

    @classmethod
    def _calc_slots(cls, *slots: "Unpack[tuple[str, ...]]"):
        _slots = set(slots)
        for _base in cls.__mro__:
            try:
                _slots.update(_base.__SLOTS__)
            except AttributeError:
                pass
        return _slots

    def __init__(
        self, id, parent: "LoggerType | None" = None, /, **kwargs: "Unpack[Args]"
    ):
        self.id = id
        self.parent = parent or getLogger(
            self.__class__.__module__ + "." + self.__class__.__name__
        )
        self.time = time
        self.configure_logger()
        self.log(VERBOSE, "init")

    async def async_shutdown(self):
        # mostly useful for multiple inheritance patterns
        self.log(VERBOSE, "async_shutdown")

    def __repr__(self):
        return f"{self.__class__.__name__}({self.id})"

    def configure_logger(self, /):
        self.logtag = f"{self.__class__.__name__}({self.id})"

    def getEffectiveLevel(self):
        return self.parent.getEffectiveLevel()

    def isEnabledFor(self, level: int):
        return self.parent.isEnabledFor(level)

    def log(self, level: int, msg: str, *args, **kwargs: "Unpack[LoggerArgs]"):
        # TODO: use Logger.filter/formatter to accomplish this more elegantly
        self.parent.log(level, f"{self.logtag}: {msg}", *args, **kwargs)

    def log_exception(
        self,
        level: int,
        exception: BaseException,
        msg: str,
        *args,
        **kwargs: "Unpack[LoggerArgs]",
    ):
        self.log(
            level,
            f"{exception.__class__.__name__}({str(exception)}) in {msg}",
            *args,
            **kwargs,
        )

    @contextmanager
    def exception_warning(self, msg: str, *args, **kwargs: "Unpack[LoggerArgs]"):
        try:
            yield
        except Exception as exception:
            self.log_exception(self.WARNING, exception, msg, *args, **kwargs)

    def __del__(self):
        self.log(VERBOSE, "destroy")
