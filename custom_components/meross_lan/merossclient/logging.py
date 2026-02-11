"""
Logging utilities for merossclient library.
"""

import abc
from contextlib import contextmanager
import logging
from time import time
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from typing import Any, Callable, Final, Protocol, TypedDict, Unpack

    NOTSET: Final
    VERBOSE: Final
    DEBUG: Final
    INFO: Final
    WARNING: Final
    CRITICAL: Final

    class LoggerType(Protocol):
        """Protocol definition for logger-like instances used in the library."""

        def getEffectiveLevel(self) -> int: ...
        def isEnabledFor(self, level: int) -> bool: ...
        def log(self, level: int, msg: str, *args, **kwargs) -> None: ...


NOTSET = logging.NOTSET
VERBOSE = 5
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
CRITICAL = logging.CRITICAL

Logger = logging.Logger
getLevelName = logging.getLevelName


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


class _Logger(logging.Logger if TYPE_CHECKING else object):
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

    @override
    def _log(self, level, msg, args, **kwargs):
        if "timeout" in kwargs:
            timeout = kwargs.pop("timeout")
            epoch = time()
            trap_key = (msg, args)
            if trap_key in _Logger._LOGGER_TIMEOUTS:
                if (epoch - _Logger._LOGGER_TIMEOUTS[trap_key]) < timeout:
                    if self.isEnabledFor(VERBOSE):
                        super()._log(
                            VERBOSE,
                            f"dropped log message for {msg}",
                            args,
                            **kwargs,
                        )
                    return
            _Logger._LOGGER_TIMEOUTS[trap_key] = epoch

        super()._log(level, msg, args, **kwargs)


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

    def log(self, level: int, msg: str, *args, **kwargs):
        # TODO: use Logger.filter/formatter to accomplish this more elegantly
        self.parent.log(level, f"{self.logtag}: {msg}", *args, **kwargs)

    def log_exception(
        self, level: int, exception: BaseException, msg: str, *args, **kwargs
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
        self.log(VERBOSE, "destroy")
