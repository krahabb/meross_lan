"""
Logging utilities for merossclient library.
"""

import abc
import asyncio
from contextlib import contextmanager
import logging
from time import time
from typing import TYPE_CHECKING, override

from . import async_load_zoneinfo, broadcast
from .obfuscate import OBFUSCATE_KEYS

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Coroutine,
        Final,
        Iterable,
        NotRequired,
        Protocol,
        TypedDict,
        Unpack,
    )

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
    This class also adds a 'shutdown_broadcast' behavior that
    can be listened to by any client class to be notified when the
    Loggable is shutdown, so that they can perform cleanup if needed.
    """

    class Broadcast[_ret, *_argsT](broadcast.Broadcast[_ret, *_argsT]):
        """create a broadcast object with auto-shutdown support when the parent Loggable is shutdown."""

        def __init__(self, loggable: "Loggable", /):
            loggable.shutdown_broadcast.add(self.clear)

    if TYPE_CHECKING:
        __SLOTS__: ClassVar[tuple[str, ...]]
        SLOTS_AUTO_INIT: ClassVar[tuple[str, ...]]
        """Provides a list of slotted attributes which need to be initialized during construction.
        Some of these attributes might be retrieved from kwargs. If not available these need to be retrieved from
        class instance defaults or set to None as last resort.
        This attribute will be read during class construction in order to update the internal cache (__SLOTS_AUTO_INIT)
        of all the inherited 'auto init slots' (See __init_subclass__).
        Attributes listed here will also be automatically added to the class __SLOTS__ so they'd not need to be defined twice."""
        __SLOTS_AUTO_INIT: Final[tuple[str, ...]]
        """Auto-built cache of all the 'SLOTS_AUTO_INIT' defined for the full class hierarchy so that
        the constructor doesn't have to traverse the mro every time in order to apply initialization."""

        id: Final[Any]
        parent: Final[LoggerType]
        loop: Final[asyncio.AbstractEventLoop]
        shutdown_broadcast: broadcast.Broadcast[None]
        async_shutdown_broadcast: broadcast.Broadcast[Coroutine[Any, Any, None]]

        _tasks: set[asyncio.Future]  # dynamic
        _timers: dict[Callable, asyncio.TimerHandle]  # dynamic

        @staticmethod
        def time() -> float: ...
        class Args(TypedDict):
            loop: NotRequired[asyncio.AbstractEventLoop]

    VERBOSE = VERBOSE
    DEBUG = DEBUG
    INFO = INFO
    WARNING = WARNING
    CRITICAL = CRITICAL

    __slots__ = (
        "id",
        "logtag",
        "parent",
        "loop",
        "time",
        "shutdown_broadcast",
        "async_shutdown_broadcast",
        "_tasks",
        "_timers",
        "__dict__",
    )
    __SLOTS__ = ()
    __SLOTS_AUTO_INIT = ()

    def __init_subclass__(cls, *args, **kwargs):
        slots_auto_init = set()

        for _base in cls.__bases__:
            try:
                slots_auto_init.update(_base.__SLOTS_AUTO_INIT)
            except AttributeError:
                pass
        try:
            _slots_auto_init = cls.__dict__["SLOTS_AUTO_INIT"]
            slots_auto_init.update(_slots_auto_init)
            try:
                # if class defines its own slots then we update it so that it is not needed to duplicate typing
                existing_slots = set(cls.__dict__["__slots__"])
                existing_slots.update(_slots_auto_init)
                cls.__slots__ = tuple(existing_slots)
            except KeyError:
                pass
        except KeyError:
            pass  # no SLOTS_AUTO_INIT defined in this class, just inherit from parent

        if len(slots_auto_init) > len(cls.__SLOTS_AUTO_INIT):
            cls.__SLOTS_AUTO_INIT = tuple(slots_auto_init)  # type: ignore

    @classmethod
    def _calc_slots(cls, *slots: "Unpack[tuple[str, ...]]"):
        _added_slots = set(slots)
        _existing_slots = set()
        # TODO: we need to see if it's feasible to cache results in
        # cls.__dict__ to avoid repeating this on every subclass since our
        # mro could be highly repetitive and this method is called on every subclass
        for _base in cls.__mro__:
            try:
                _added_slots.update(_base.__dict__["__SLOTS__"])
            except KeyError:
                pass
            try:
                _added_slots.update(_base.__dict__["SLOTS_AUTO_INIT"])
            except KeyError:
                pass
            try:
                _existing_slots.update(_base.__dict__["__slots__"])
            except KeyError:
                pass

        return _added_slots - _existing_slots

    def __init__(
        self, id, parent: "LoggerType | None" = None, /, **kwargs: "Unpack[Args]"
    ):
        self.id = id
        self.parent = parent or getLogger(
            self.__class__.__module__ + "." + self.__class__.__name__
        )
        self.loop = (
            kwargs.pop("loop", None)
            or getattr(parent, "loop", None)
            or asyncio.get_event_loop()
        )
        _cls = self.__class__
        for _attr in _cls.__SLOTS_AUTO_INIT:
            try:
                # extract from kwargs
                setattr(self, _attr, kwargs.pop(_attr))
            except KeyError:
                # else retrieve from class attr or just 'None'
                setattr(self, _attr, getattr(_cls, f"init_{_attr}", None))

        self.shutdown_broadcast = broadcast.Broadcast()
        self.async_shutdown_broadcast = broadcast.Broadcast()
        self.time = time
        self.configure_logger()
        self.log(VERBOSE, "init")

    async def async_shutdown(self):
        """
        Shutdown the Loggable instance by cancelling pending timers and tasks and broadcasting the shutdown event.
        This method should be the preferered way to orderly clean-up instance state and resources,
        especially when async operations are involved, since it will wait for pending tasks to be
        cancelled or completed before proceeding to synchronous shutdown by invoking the shutdown() method.
        Subclasses can override either or both depending on their cleanup needs where the sync version should
        be preferred for performance reasons, while the async version should be used when async operations
        might be critical in the cleanup sequence.
        """
        self.log(VERBOSE, "async_shutdown")
        try:
            # remove timers first so that they'll not eventually be triggered while awaiting shutdown
            for _timer in self._timers.values():
                self.log(self.DEBUG, "Cancelling pending timer %r", _timer)
                _timer.cancel()
            self._timers.clear()
        except AttributeError:
            pass  # might be not initialized ...

        try:
            for task in tuple(self._tasks):
                if task.done():
                    continue
                self.log(self.DEBUG, "Shutting down pending task %r", task)
                task.cancel(f"{self} shutdown")
                try:
                    async with asyncio.timeout(0.5):
                        await task
                except asyncio.CancelledError:
                    continue
                except Exception as exception:
                    self.log_exception(
                        self.WARNING,
                        exception,
                        "cancelling task %r during shutdown",
                        task,
                    )

            if self._tasks:
                self.log(
                    self.WARNING,
                    "Some tasks were not properly shutdown (%r)",
                    self._tasks,
                )

        except AttributeError:
            pass  # might be not initialized ...

        if self.async_shutdown_broadcast:
            # create a copy since the shutdown callbacks would typically
            # remove themselves from the broadcast
            for _listener in tuple(self.async_shutdown_broadcast):
                await _listener()
            self.async_shutdown_broadcast.clear()

        self.shutdown()

    def shutdown(self):
        self.log(VERBOSE, "shutdown")
        if self.shutdown_broadcast:
            for _listener in tuple(self.shutdown_broadcast):
                _listener()
            self.shutdown_broadcast.clear()

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

    def create_task[_T](
        self, coro: "Coroutine[Any, Any, _T]", name: str, eager_start: bool = False
    ):
        if eager_start:
            # WARNING: direct Task creation should be avoided in favor of asyncio dedicated apis.
            # In 3.14 this will be possible with create_task(..., eager_start=True)
            task = asyncio.Task(
                coro, loop=self.loop, name=f"{self.logtag}{name}", eager_start=True
            )
            if task.done():
                return task
        else:
            task = self.loop.create_task(coro, name=f"{self.logtag}{name}")
        try:
            self._tasks.add(task)
        except AttributeError:
            self._tasks = {task}
        task.add_done_callback(self._done_task_callback)
        return task

    def _done_task_callback(self, task: asyncio.Future):
        self._tasks.remove(task)
        try:
            task.result()
        except (asyncio.CancelledError, Exception) as e:
            self.log_exception(
                self.DEBUG,
                e,
                "Task %r",
                task,
            )

    def schedule_async_callback(
        self, delay: float, target: "Callable[..., Coroutine]", *args
    ):
        """Schedules an async callback to be called after a delay by calling loop.call_later.
        TimerHandles (and Tasks) are cached and automatically cancelled on async_shutdown.
        The 'target' argument is used as a key to ensure that only one timer per target is
        active at a time, so that if the same callback is already scheduled it'll be rescheduled
        with the new delay. Also, scheduled timers can be cancelled by using the 'cancel_callback' method
        with the same target."""
        timer = self.loop.call_later(delay, self._async_callback_wrapper, target, *args)
        try:
            # drop if already scheduled (re-schedule)
            self._timers[target].cancel()
            self._timers[target] = timer
        except AttributeError:
            self._timers = {target: timer}
        except KeyError:
            self._timers[target] = timer
        return timer

    def schedule_callback(self, delay: float, target: "Callable", *args):
        """Schedules a sync callback to be called after a delay by calling loop.call_later.
        See schedule_async_callback for more details."""
        timer = self.loop.call_later(delay, self._callback_wrapper, target, *args)
        try:
            # drop if already scheduled (re-schedule)
            self._timers[target].cancel()
            self._timers[target] = timer
        except AttributeError:
            self._timers = {target: timer}
        except KeyError:
            self._timers[target] = timer
        return timer

    def cancel_callback(self, target: "Callable"):
        try:
            self._timers[target].cancel()
            del self._timers[target]
        except (AttributeError, KeyError):
            pass

    def _callback_wrapper(self, target: "Callable", *args):
        del self._timers[target]
        try:
            target(*args)
        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "running callback %r with args %r",
                target,
                args,
            )

    def _async_callback_wrapper(self, target: "Callable[..., Coroutine]", *args):
        del self._timers[target]
        self.create_task(target(*args), "._callback_async_wrapper", eager_start=True)

    async def async_load_zoneinfo(self, tzname: str, /):
        try:
            return await async_load_zoneinfo(tzname)
        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "loading timezone(%s) - check your python environment",
                tzname,
                timeout=14400,
            )
            raise

    def __del__(self):
        self.log(VERBOSE, "destroy")
