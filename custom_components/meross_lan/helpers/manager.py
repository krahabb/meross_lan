from abc import abstractmethod
import asyncio
import os
from time import localtime, strftime
from typing import TYPE_CHECKING, final, override
import weakref

from homeassistant.components import persistent_notification as pn
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .. import const as mlc
from ..const import (
    CONF_CREATE_DIAGNOSTIC_ENTITIES,
    CONF_KEY,
    CONF_OBFUSCATE,
    DOMAIN,
)
from ..merossclient import logging
from ..merossclient.client import Transport
from ..merossclient.obfuscate import (
    OBFUSCATE_ANY,
    OBFUSCATE_DICT,
    OBFUSCATE_HOST_MAP,
    OBFUSCATE_KEY_MAP,
    OBFUSCATE_KEYS,
    OBFUSCATE_SERVER_MAP,
    OBFUSCATE_USERID_MAP,
    OBFUSCATE_UUID_MAP,
    ObfuscateMap,
)
from ..merossclient.protocol.message import json_dumps

if TYPE_CHECKING:
    import io
    from typing import (
        Any,
        Callable,
        ClassVar,
        Coroutine,
        Final,
        Mapping,
        NotRequired,
        TypedDict,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from ..merossclient import HostAddress
    from ..merossclient.client import Direction
    from ..merossclient.logging import LoggerArgs
    from ..merossclient.protocol.message import MerossMessage
    from ..merossclient.protocol.types import MerossPayloadType
    from .component_api import ComponentApi
    from .entity import Entity

OBFUSCATE_KEYS |= {
    # ConfigEntries keys
    mlc.CONF_DEVICE_ID: OBFUSCATE_UUID_MAP,
    mlc.CONF_HOST: OBFUSCATE_HOST_MAP,
    # mlc.CONF_KEY: OBFUSCATE_KEY_MAP,
    mlc.CONF_CLOUD_KEY: OBFUSCATE_KEY_MAP,
    mlc.CONF_PASSWORD: OBFUSCATE_ANY,
    #
    # MerossProfile keys
    "appId": ObfuscateMap({}),
}


class EntityManager(logging.Loggable):
    """
    This is an abstraction of an actual (device or other) container
    for Entity(s). This container is very 'hybrid', end its main purpose
    is to provide interfaces to their owned MerossEntities.
    It could represent a Device, a SubDevice or an ApiProfile
    and manages the relation(s) with the ConfigEntry (config, life-cycle).
    This is a 'partial' base class for ConfigEntryManager which definitely establishes
    the relationship with the ConfigEntry. This is in turn needed to better establish
    an isolation level between SubDevice and a ConfigEntry
    """

    if TYPE_CHECKING:

        class DeviceEntryIdType(TypedDict):
            identifiers: set[tuple[str, str]]

        type PlatformsType = dict[str, Callable | None]

        id: Final[str]  # type: ignore[override]
        parent: Final[EntityManager]  # type: ignore[override]
        api: Final[ComponentApi]
        is_connected: Final[
            bool
        ]  # BEWARE: this property could mixin with AbstractClient in Device
        """Indicates if the manager is 'online' i.e. active (connected to device/cloud)."""
        device_entry: Final[dr.DeviceEntry | None]
        """Link to optional DeviceRegistry entry info."""

        platforms: PlatformsType  # init in derived
        entities: Final[dict[object, Entity]]

        class Args(logging.Loggable.Args):
            device_entry: NotRequired[dr.DeviceEntry | None]

    ROOT_LOGGER = logging.getLogger(__name__[:-16])
    """Root meross_lan logger"""

    IssueSeverity = ir.IssueSeverity

    _attr_is_connected: bool = True

    # slots for ConfigEntryManager are defined here since we would have some
    # multiple inheritance conflicts in Device

    __SLOTS__ = (
        "api",
        "is_connected",
        "device_entry",
        "platforms",
        "entities",
    )

    def __init__(self, id: str, manager: "EntityManager", **kwargs: "Unpack[Args]"):
        self.api = manager.api
        self.is_connected = self._attr_is_connected
        self.device_entry = kwargs.get("device_entry")
        assert hasattr(self, "platforms"), "platforms must be set in derived classes"
        self.entities = {}
        super().__init__(id, manager, **kwargs)

    @property
    def display_name(self) -> str:
        """
        returns a proper (friendly) name for logging purposes
        """
        de = self.device_entry
        return (de and (de.name_by_user or de.name)) or self.logtag

    def managed_entities(self, platform, /):
        """entities list for platform setup"""
        return [
            entity for entity in self.entities.values() if entity.PLATFORM is platform
        ]

    def generate_unique_id(self, entity: "Entity", /):
        """
        flexible policy in order to generate unique_ids for entities:
        This is an helper needed to better control migrations in code
        which could/would lead to a unique_id change.
        We could put here code checks in order to avoid entity_registry
        migrations
        """
        return f"{self.id}_{entity.id}"

    def get_device_entry(self, channel, /):
        """
        Return the DeviceRegistry entry for a given channel (if any).
        By default this returns self.device_entry but derived classes
        (like Hub) could override this to return different entries
        for different channels.
        """
        return self.device_entry

    @override
    def create_task[_T](
        self, target: "Coroutine[Any, Any, _T]", name: str, eager_start: bool = False
    ):
        task = self.api.hass.async_create_task(
            target, f"{self.logtag}{name}", eager_start=eager_start
        )
        if eager_start and task.done():
            return task
        try:
            self._tasks.add(task)
        except AttributeError:
            self._tasks = {task}
        task.add_done_callback(self._done_task_callback)
        return task


class ConfigEntryManager(EntityManager):
    """
    This class manages the relationships with an actual ConfigEntry and its managed
    device(s) and entities. A typical Meross device inherits from this but also
    A MerossCloudProfile and the 'MQTTHub'.
    """

    if TYPE_CHECKING:

        DEFAULT_PLATFORMS: ClassVar[EntityManager.PlatformsType]

        parent: Final[ComponentApi]  # type: ignore[override]
        config_entry: Final[ConfigEntry | None]
        config: Mapping[str, Any]
        key: str
        logger: logging._Logger
        _issues: set[str]  # BEWARE: on demand attribute
        _trace_file: io.TextIOWrapper | None
        _trace_future: asyncio.Future | None
        _trace_data: list | None
        _entry_update_listener_unsub: CALLBACK_TYPE

        class Args(EntityManager.Args):
            pass

    DEFAULT_PLATFORMS = {}
    """Defined at the class level to preset a list of domains for entities
    which could be dynamically added after ConfigEntry loading."""

    __slots__ = EntityManager._calc_slots(
        "config_entry",
        "config",
        "key",
        "obfuscate",
        "_issues",
        "_trace_file",
        "_trace_future",
        "_trace_data",
        "_entry_update_listener_unsub",
    )

    def __init__(
        self,
        id: str,
        api: "ComponentApi",
        config_entry: "ConfigEntry | None" = None,
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.config_entry = config_entry
        try:
            self.config = config = config_entry.data  # type: ignore
            self.key = config.get(CONF_KEY) or ""
            self.obfuscate = config.get(CONF_OBFUSCATE, True)
        except AttributeError:
            # this is the ComponentApi: ConfigEntry not configured..
            assert id == mlc.CONF_PROFILE_ID_LOCAL
            self.config = {}
            self.key = mlc.PARAM_DEFAULT_KEY
            self.obfuscate = True
        # when we build an entity we also add the relative platform name here
        # so that the async_setup_entry for this integration will be able to forward
        # the setup to the appropriate platform(s).
        # The item value here will be set to the async_add_entities callback
        # during the corresponding platform async_setup_entry so to be able
        # to dynamically add more entities should they 'pop-up' (Hub only?)
        self.platforms = self.DEFAULT_PLATFORMS.copy()
        self._trace_file = None
        self._trace_future = None
        self._trace_data = None
        kwargs.setdefault("loop", api.hass.loop)
        super().__init__(id, api, **kwargs)

    async def async_shutdown(self):
        """
        Cleanup code called when the config entry is unloaded.
        Beware, when a derived class owns some direct member pointers to entities,
        be sure to invalidate them after calling the super() implementation.
        This is especially true for Device(s) classes which need to stop
        their async polling before invalidating the member pointers (which are
        usually referred to inside the polling /parsing code)
        """
        await super().async_shutdown()
        # TODO: register trace_close in shutdown callback
        if self.is_tracing:
            self.trace_close()

    # interface: Loggable
    def configure_logger(self, /):
        """
        Configure a 'logger' and a 'logtag' based off current config for every ConfigEntry.
        We'll need this updated when CONF_OBFUSCATE changes since
        the name might depend on it. We're then using this call during
        __init__ for the first setup and subsequently when ConfigEntry changes
        """
        self.logtag = self.get_logger_name()
        self.logger = logger = logging.getLogger(
            f"{self.ROOT_LOGGER.name}.{self.logtag}"
        )
        try:
            logger.setLevel(self.config.get(mlc.CONF_LOGGING_LEVEL, logging.NOTSET))
        except Exception as exception:
            # do not use self Loggable interface since we might be not set yet
            self.ROOT_LOGGER.warning(
                "error (%s) setting log level: likely a corrupted configuration entry",
                str(exception),
            )
        self.getEffectiveLevel = logger.getEffectiveLevel
        self.isEnabledFor = logger.isEnabledFor

    def log(self, level: int, msg: str, *args, **kwargs: "Unpack[LoggerArgs]"):
        if self.isEnabledFor(level):
            kwargs["obfuscate"] = self.obfuscate
            self.logger._log(level, msg, args, **kwargs)

        if self.is_tracing:
            self.trace_log(
                level,
                msg
                % (args + logging.extract_obfuscated_kwargs(self.obfuscate, kwargs)),
            )

    # interface: EntityManager
    @property
    def display_name(self) -> str:
        de = self.device_entry
        return (de and (de.name_by_user or de.name)) or (
            self.config_entry.title if self.config_entry else self.logtag
        )

    # interface: self
    @property
    def create_diagnostic_entities(self):
        return self.config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES)

    async def async_setup_entry(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry", /
    ):
        assert self.config_entry == config_entry
        config_entry.runtime_data = self
        api = self.api
        # open the (eventual) trace before adding the entities
        # so we could catch logs in this phase too. See
        # OptionsFlow.async_step_diagnostics for the mechanic.
        try:
            await self.async_trace_open(
                api.managers_transient_state[config_entry.entry_id].pop(mlc.CONF_TRACE)
            )
        except KeyError:
            # no CONF_TRACE key and/or no config_entry.entry_id...no tracing configured
            pass

        if self.config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES):
            await self.async_create_diagnostic_entities()

        await hass.config_entries.async_forward_entry_setups(
            config_entry, self.platforms.keys()
        )
        self._entry_update_listener_unsub = config_entry.add_update_listener(
            self.entry_update_listener
        )

    async def async_unload_entry(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry", /
    ):
        if not await hass.config_entries.async_unload_platforms(
            config_entry, self.platforms.keys()
        ):
            return False
        self._entry_update_listener_unsub()
        self.platforms.clear()
        self.config = {}
        await self.async_shutdown()
        return True

    def schedule_reload(self, delay: float = 0, /):
        """
        Schedule the reload in a delayed task (using 'call_later').
        config_entries.async_schedule_reload is now 'eager' and
        it might execute synchronously leading to unintended semantics.
        """
        assert self.config_entry
        self.schedule_callback(
            delay,
            self.api.config_entries.async_schedule_reload,
            self.config_entry.entry_id,
        )

    async def entry_update_listener(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry", /
    ):
        old_config = self.config
        config = self.config = config_entry.data
        self.key = config.get(CONF_KEY) or ""
        self.obfuscate = config.get(CONF_OBFUSCATE, True)
        self.configure_logger()
        if self.isEnabledFor(self.VERBOSE):
            args = (old_config, config)
            self.logger._log(
                self.VERBOSE,
                "Config updated: old=%s new=%s",
                (
                    tuple(str(OBFUSCATE_DICT(_c)) for _c in args)
                    if self.obfuscate
                    else args
                ),
            )
        if config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES):
            await self.async_create_diagnostic_entities()
        else:
            await self.async_destroy_diagnostic_entities(True)

    async def async_create_diagnostic_entities(self, /):
        """Dynamically create some diagnostic entities depending on configuration"""
        pass

    async def async_destroy_diagnostic_entities(self, remove: bool = False, /):
        """Cleanup diagnostic entities, when the entry is unloaded. If 'remove' is True
        it will be removed from the entity registry as well."""
        ent_reg = self.api.entity_registry if remove else None
        for entity in self.managed_entities(SENSOR_DOMAIN):
            if entity.is_diagnostic:
                if entity.hass_connected:
                    await entity.async_remove()
                await entity.async_shutdown()
                if ent_reg:
                    ent_reg.async_remove(entity.entity_id)

    def create_issue(
        self,
        issue_key: str,
        issue_subkey: str = "",
        *,
        data: dict[str, str | int | float | None] | None = None,
        severity: ir.IssueSeverity = ir.IssueSeverity.CRITICAL,
        translation_placeholders: dict[str, str] | None = None,
    ):
        issue_id = f"{issue_key}.{self.id}.{issue_subkey}"
        try:
            issues = self._issues
            if issue_id in issues:
                return
        except AttributeError:
            issues = self._issues = set()
        self.api.issue_registry.async_get_or_create(
            mlc.DOMAIN,
            issue_id,
            data=data,
            is_fixable=True,
            is_persistent=False,
            severity=severity,
            translation_key=issue_key,
            translation_placeholders=translation_placeholders,
        )
        issues.add(issue_id)

    def remove_issue_id(self, issue_id: str, /):
        try:
            self._issues.remove(issue_id)
            self.api.issue_registry.async_delete(mlc.DOMAIN, issue_id)
        except (AttributeError, KeyError):
            # either no _issues attr or issue_id not in set
            return

    def remove_issue(self, issue_key: str, issue_subkey: str = "", /):
        self.remove_issue_id(f"{issue_key}.{self.id}.{issue_subkey}")

    @abstractmethod
    def get_logger_name(self) -> str:
        raise NotImplementedError()

    @final
    def loggable_config(self, /):
        """Return a 'loggable' version of the entry config (for diagnostic/logging purposes)"""
        return OBFUSCATE_DICT(self.config) if self.obfuscate else dict(self.config)

    def loggable_diagnostic_state(self, /):
        """Return a 'loggable' version of the entry state (for diagnostic/logging purposes)"""
        return {}

    @final
    def loggable_broker(self, broker: "HostAddress | str", /):
        """Conditionally obfuscate the connection_id (which is a broker address host:port) to send to logging/tracing"""
        return OBFUSCATE_SERVER_MAP(str(broker)) if self.obfuscate else str(broker)

    @final
    def loggable_device_id(self, device_id: str, /):
        """Conditionally obfuscate the device_id to send to logging/tracing"""
        return OBFUSCATE_UUID_MAP(device_id) if self.obfuscate else device_id

    @final
    def loggable_profile_id(self, profile_id: str | int, /):
        """Conditionally obfuscate the profile_id (which is the Meross account userId) to send to logging/tracing"""
        return OBFUSCATE_USERID_MAP(profile_id) if self.obfuscate else profile_id

    @property
    def is_tracing(self):
        return self._trace_file or self._trace_data

    async def async_trace_open(self, p_trace_data: dict | None = None, /):
        """
        This method could be called either when activating 'tracing' in OptionsFlow so
        that it opens the (tab separated) file or when 'download diagnostic' is unable
        to produce an output 'in sync' (async_get_diagnostics). A Device object could
        fail to produce an immediate result and so fallback to a kind of hybrid tracing
        with both a file and a json struct (_trace_data) being built in memory.
        """
        try:
            self.log(self.DEBUG, "Tracing start")
            epoch = self.time()
            hass = self.api.hass

            def _trace_open():
                tracedir = hass.config.path(
                    "custom_components", DOMAIN, mlc.CONF_TRACE_DIRECTORY
                )
                os.makedirs(tracedir, exist_ok=True)
                return open(
                    os.path.join(
                        tracedir,
                        f"{strftime('%Y-%m-%d_%H-%M-%S', localtime(epoch))}_{self.logtag}.csv",
                    ),
                    mode="w",
                    encoding="utf8",
                )

            self._trace_file = _t = await hass.async_add_executor_job(_trace_open)

            self.schedule_callback(
                self.config.get(mlc.CONF_TRACE_TIMEOUT)
                or mlc.CONF_TRACE_TIMEOUT_DEFAULT,
                self.trace_close,
            )

            if p_trace_data is not None:
                # p_trace_data is a fragile indication we're being called to
                # output a 'debug trace' and not a 'diagnostic'. We'll
                # then add here the same data that are usually output
                # to the diagnostics platform.
                _t.write("\t".join(mlc.CONF_TRACE_COLUMNS) + "\r\n")
                self.trace(
                    epoch,
                    {
                        "version": mlc.CONF_TRACE_VERSION,
                        "config": self.loggable_config(),
                        "state": p_trace_data,
                    },
                    "",
                    "HEADER",
                    Transport.AUTO,
                    "",
                )

            self._trace_opened(epoch)
            pn.async_create(
                self.api.hass,
                f"Device: {self.display_name}\nFile: {_t.name}",  # type: ignore
                "meross_lan tracing started",
                f"{DOMAIN}.{self.id}.tracing",
            )

        except Exception as exception:
            self.trace_close(exception, "creating file")

    def _trace_opened(self, epoch: float, /):
        """
        Virtual placeholder called when a new trace is opened.
        Allows derived EntityManagers to log some preamble in the trace.
        """
        pass

    def trace_close(
        self, exception: Exception | None = None, error_context: str | None = None
    ):
        notify_message = "Data not available"
        if self._trace_file:
            try:
                notify_message = f"Data available in {self._trace_file.name}"
                self._trace_file.close()
            except Exception as e:
                if not exception:
                    exception = e
                    error_context = "closing file"
            self._trace_file = None
            self.log(self.DEBUG, "Tracing end")

        # safely check/cancel any pending timer in case trace_close is
        # being called outside the normal timeout.
        self.cancel_callback(self.trace_close)
        if self._trace_future:
            self._trace_future.set_result(self._trace_data)
            self._trace_future = None
        self._trace_data = None
        if exception:
            self.log_exception(
                self.WARNING, exception, "tracing operation (%s)", error_context
            )
            notify_title = "Tracing error"
            notify_message = f"{exception} in {error_context}\n{notify_message}"
        else:
            notify_title = "Tracing terminated"
        pn.async_create(
            self.api.hass,
            f"Device: {self.display_name}\n{notify_message}",
            notify_title,
            f"{DOMAIN}.{self.id}.tracing",
        )

    def trace(
        self,
        epoch: float,
        payload: "MerossPayloadType",
        namespace: str,
        method: str,
        transport: Transport,
        rxtx: str,
        /,
    ):
        try:
            data = OBFUSCATE_DICT(payload) if self.obfuscate else payload
            columns = [
                strftime("%Y/%m/%d - %H:%M:%S", localtime(epoch)),
                rxtx,
                transport,
                method,
                namespace,
                data,
            ]
            if self._trace_data:
                self._trace_data.append(columns)
            if self._trace_file:
                columns[5] = json_dumps(data)
                self._trace_file.write("\t".join(columns) + "\r\n")
                columns[5] = data  # restore the (eventual) _trace_data ref
                if self._trace_file.tell() > mlc.CONF_TRACE_MAXSIZE:
                    self.trace_close()

        except Exception as exception:
            self.trace_close(exception, "appending data")

    def trace_msg(
        self,
        epoch: float,
        msg: "MerossMessage",
        transport: Transport,
        dir: "Direction",
        /,
    ):
        try:
            data = OBFUSCATE_DICT(msg.payload) if self.obfuscate else msg.payload
            columns = [
                strftime("%Y/%m/%d - %H:%M:%S", localtime(epoch)),
                dir,
                transport,
                msg.method,
                msg.namespace,
                data,
            ]
            if self._trace_data:
                self._trace_data.append(columns)
            if self._trace_file:
                columns[5] = json_dumps(data)
                self._trace_file.write("\t".join(columns) + "\r\n")
                columns[5] = data  # restore the (eventual) _trace_data ref
                if self._trace_file.tell() > mlc.CONF_TRACE_MAXSIZE:
                    self.trace_close()

        except Exception as exception:
            self.trace_close(exception, "appending data")

    def trace_log(self, level: int, msg: str, /):
        try:
            columns = [
                strftime("%Y/%m/%d - %H:%M:%S", localtime(self.time())),
                "",  # rxtx
                Transport.AUTO,  # protocol
                "LOG",  # method
                mlc.CONF_LOGGING_LEVEL_OPTIONS.get(level)
                or logging.getLevelName(level),  # namespace
                msg,  # data
            ]
            if self._trace_data:
                self._trace_data.append(columns)
            if self._trace_file:
                self._trace_file.write("\t".join(columns) + "\r\n")
                if self._trace_file.tell() > mlc.CONF_TRACE_MAXSIZE:
                    self.trace_close()

        except Exception as exception:
            self.trace_close(exception, "appending log")

    async def async_get_diagnostics(self, /) -> "mlc.TracingHeaderType":
        # used to return diagnostic data for this ConfigEntry (see diagnostics.py)
        return {
            "version": mlc.CONF_TRACE_VERSION,
            "config": self.loggable_config(),
            "state": self.loggable_diagnostic_state(),
        }
