from __future__ import annotations
import asyncio
import logging
import os
import socket
import math
from typing import  Callable, Dict, List
from time import strftime, time
from io import TextIOWrapper
from json import (
    dumps as json_dumps,
    loads as json_loads,
)
import voluptuous as vol
from enum import Enum

from datetime import datetime

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time

from .merossclient import (
    const as mc,  # mEROSS cONST
    MerossDeviceDescriptor, MerossHttpClient,
    get_replykey, build_default_payload_get,
)
from .meross_entity import MerossFakeEntity
from .helpers import LOGGER, LOGGER_trap, mqtt_is_connected
from .const import (
    DOMAIN, DND_ID,
    CONF_DEVICE_ID, CONF_KEY, CONF_PAYLOAD, CONF_HOST, CONF_TIMESTAMP,
    CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT, CONF_POLLING_PERIOD_MIN,
    CONF_PROTOCOL, CONF_OPTION_AUTO, CONF_OPTION_HTTP, CONF_OPTION_MQTT,
    CONF_TRACE, CONF_TRACE_DIRECTORY, CONF_TRACE_FILENAME, CONF_TRACE_MAXSIZE,
    PARAM_HEARTBEAT_PERIOD, PARAM_TIMEZONE_CHECK_PERIOD, PARAM_TIMESTAMP_TOLERANCE,
)

# these are dynamically created MerossDevice attributes in a sort of a dumb optimization
VOLATILE_ATTR_HTTPCLIENT = '_httpclient'

# when tracing we enumerate appliance abilities to get insights on payload structures
# this list will be excluded from enumeration since it's redundant/exposing sensitive info
# or simply crashes/hangs the device
TRACE_ABILITY_EXCLUDE = (
    mc.NS_APPLIANCE_SYSTEM_ALL,
    mc.NS_APPLIANCE_SYSTEM_ABILITY,
    mc.NS_APPLIANCE_SYSTEM_DNDMODE,
    mc.NS_APPLIANCE_SYSTEM_TIME,
    mc.NS_APPLIANCE_SYSTEM_HARDWARE,
    mc.NS_APPLIANCE_SYSTEM_FIRMWARE,
    mc.NS_APPLIANCE_SYSTEM_ONLINE,
    mc.NS_APPLIANCE_SYSTEM_REPORT,
    mc.NS_APPLIANCE_SYSTEM_DEBUG,
    mc.NS_APPLIANCE_SYSTEM_CLOCK,
    mc.NS_APPLIANCE_CONFIG_KEY,
    mc.NS_APPLIANCE_CONFIG_WIFI,
    mc.NS_APPLIANCE_CONFIG_WIFIX, # disconnects
    mc.NS_APPLIANCE_CONFIG_WIFILIST,
    mc.NS_APPLIANCE_CONFIG_TRACE,
    mc.NS_APPLIANCE_CONTROL_BIND,
    mc.NS_APPLIANCE_CONTROL_UNBIND,
    mc.NS_APPLIANCE_CONTROL_MULTIPLE,
    mc.NS_APPLIANCE_CONTROL_UPGRADE, # disconnects
    mc.NS_APPLIANCE_HUB_EXCEPTION, # disconnects
    mc.NS_APPLIANCE_HUB_REPORT, # disconnects
    mc.NS_APPLIANCE_HUB_SUBDEVICELIST, # disconnects
    mc.NS_APPLIANCE_MCU_UPGRADE, # disconnects
    mc.NS_APPLIANCE_MCU_HP110_PREVIEW # disconnects
)

TRACE_KEYS_OBFUSCATE = (
    mc.KEY_UUID, mc.KEY_MACADDRESS, mc.KEY_WIFIMAC, mc.KEY_INNERIP,
    mc.KEY_SERVER, mc.KEY_PORT, mc.KEY_USERID, mc.KEY_TOKEN
)

TRACE_DIRECTION_RX = 'RX'
TRACE_DIRECTION_TX = 'TX'

def _obfuscate(payload: dict) -> dict:
    """
    payload: input-output gets modified by blanking sensistive keys
    returns: a dict with the original mapped obfuscated keys
    parses the input payload and 'hides' (obfuscates) some sensitive keys.
    returns the mapping of the obfuscated keys in 'obfuscated' so to re-set them in _deobfuscate
    this function is recursive
    """
    obfuscated = dict()
    for key, value in payload.items():
        if isinstance(value, dict):
            o = _obfuscate(value)
            if o:
                obfuscated[key] = o
        elif key in TRACE_KEYS_OBFUSCATE:
            obfuscated[key] = value
            payload[key] = '#' * len(str(value))

    return obfuscated

def _deobfuscate(payload: dict, obfuscated: dict):
    for key, value in obfuscated.items():
        if isinstance(value, dict):
            _deobfuscate(payload[key], value)
        else:
            payload[key] = value


TIMEZONES_SET = None

class Protocol(Enum):
    """
    Describes the protocol selection behaviour in order to connect to devices
    """
    AUTO = 0 # 'best effort' behaviour
    MQTT = 1
    HTTP = 2


MAP_CONF_PROTOCOL = {
    CONF_OPTION_AUTO: Protocol.AUTO,
    CONF_OPTION_MQTT: Protocol.MQTT,
    CONF_OPTION_HTTP: Protocol.HTTP
}


class MerossDevice:

    def __init__(
        self,
        api: object,
        descriptor: MerossDeviceDescriptor,
        entry: ConfigEntry
    ):
        self.device_id = entry.data.get(CONF_DEVICE_ID)
        LOGGER.debug("MerossDevice(%s) init", self.device_id)
        self.api = api
        self.descriptor = descriptor
        self.entry_id = entry.entry_id
        self.replykey = None
        self._online = False
        self.needsave = False # while parsing ns.ALL code signals to persist ConfigEntry
        self._retry_period = 0 # used to try reconnect when falling offline
        self.entity_dnd = MerossFakeEntity
        self.device_timestamp: int = 0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.lastpoll = 0
        self.lastrequest = 0
        self.lastupdate = 0
        self.lastmqtt = 0 # means we recently received an mqtt message
        self.hasmqtt = False # hasmqtt means it is somehow available to communicate over mqtt
        self._trace_file: TextIOWrapper = None
        self._trace_endtime = 0
        self._trace_ability_iter = None
        """
        self.entities: dict()
        is a collection of all of the instanced entities
        they're generally built here during __init__ and will be registered
        in platforms(s) async_setup_entry with HA
        """
        self.entities: Dict[object, '_MerossEntity'] = dict()  # pylint: disable=undefined-variable

        """
        This is mainly for HTTP based devices: we build a dictionary of what we think could be
        useful to asynchronously poll so the actual polling cycle doesnt waste time in checks
        TL:DR we'll try to solve everything with just NS_SYS_ALL since it usually carries the full state
        in a single transaction. Also (see #33) the multiplug mss425 doesnt publish the full switch list state
        through NS_CNTRL_TOGGLEX (not sure if it's the firmware or the dialect)
        Even if some devices don't carry significant state in NS_ALL we'll poll it anyway even if bulky
        since it carries also timing informations and whatever
        As far as we know rollershutter digest doesnt report state..so we'll add requests for that
        For Hub(s) too NS_ALL is very 'partial' (at least MTS100 state is not fully exposed)
        """
        self.polling_period = CONF_POLLING_PERIOD_DEFAULT
        self.polling_dictionary: List[str] = list()
        self.polling_dictionary.append(mc.NS_APPLIANCE_SYSTEM_ALL)
        """
        self.platforms: dict()
        when we build an entity we also add the relative platform name here
        so that the async_setup_entry for the integration will be able to forward
        the setup to the appropriate platform.
        The item value here will be set to the async_add_entities callback
        during the corresponding platform async_setup_entry so to be able
        to dynamically add more entities should they 'pop-up' (Hub only?)
        """
        self.platforms: Dict[str, Callable] = {}
        """
        misc callbacks
        """
        self.unsub_entry_update_listener: Callable = None
        self.unsub_updatecoordinator_listener: Callable = None

        self._set_config_entry(entry.data)

        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in self.descriptor.ability:
            #from .switch import MerossLanDND
            from .light import MerossLanDNDLight
            #self.entity_dnd = MerossLanDND(self)
            self.entity_dnd = MerossLanDNDLight(self)

        """
        warning: would the response be processed after this object is fully init?
        It should if I get all of this async stuff right
        also: !! IMPORTANT !! don't send any other message during init process
        else the responses could overlap and 'fuck' a bit the offline -> online transition
        causing that code to request a new NS_APPLIANCE_SYSTEM_ALL
        """
        self.request_get(mc.NS_APPLIANCE_SYSTEM_ALL)


    def __del__(self):
        LOGGER.debug("MerossDevice(%s) destroy", self.device_id)
        return


    @property
    def host(self) -> str:
        return self._host or self.descriptor.innerIp


    @property
    def online(self) -> bool:
        if self._online:
            #evaluate device MQTT availability by checking lastrequest got answered in less than polling_period
            if (self.lastupdate > self.lastrequest) or ((time() - self.lastrequest) < (self.polling_period - 2)):
                return True
            # when we 'fall' offline while on MQTT eventually retrigger HTTP.
            # the reverse is not needed since we switch HTTP -> MQTT right-away
            # when HTTP fails (see async_http_request)
            if (self.curr_protocol is Protocol.MQTT) and (self.conf_protocol is Protocol.AUTO):
                self.switch_protocol(Protocol.HTTP)
                return True

            self._set_offline()

        return False


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:
        """
        we'll use the device timestamp to 'align' our time to the device one
        this is useful for metered plugs reporting timestamped energy consumption
        and we want to 'translate' this timings in our (local) time.
        We ignore delays below PARAM_TIMESTAMP_TOLERANCE since
        we'll always be a bit late in processing
        """
        epoch = time()
        self.device_timestamp = int(header.get(mc.KEY_TIMESTAMP, epoch))
        device_timedelta = epoch - self.device_timestamp
        if abs(device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            self._config_timestamp(epoch, device_timedelta)
        else:
            self.device_timedelta = 0
        """
        every time we receive a response we save it's 'replykey':
        that would be the same as our self.key (which it is compared against in 'get_replykey')
        if it's good else it would be the device message header to be used in
        a reply scheme where we're going to 'fool' the device by using its own hashes
        if our config allows for that (our self.key is 'None' which means empty key or auto-detect)
        Update: this key trick actually doesnt work on MQTT (but works on HTTP)
        """
        self.replykey = get_replykey(header, self.key)
        if self.key and (self.replykey != self.key):
            self.log(
                logging.WARNING, 14400,
                "MerossDevice(%s) received signature error (incorrect key?)",
                self.device_id
            )

        if method == mc.METHOD_ERROR:
            self.log(
                logging.WARNING, 14400,
                "MerossDevice(%s) protocol error: namespace = '%s' payload = '%s'",
                self.device_id, namespace, json_dumps(payload)
            )
            return True

        self.lastupdate = epoch
        if not self._online:
            self._set_online(namespace)

        if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
            self._parse_all(payload)
            if self.needsave is True:
                self.needsave = False
                self._save_config_entry(payload)
            if self.entity_dnd.enabled:
                """
                this is to optimize polling: when on MQTT we're only requesting/receiving
                when coming online and 'DND' will then work by pushes. While on HTTP we'll
                always call right after receiving 'ALL' which is the general status update
                """
                self.request_get(mc.NS_APPLIANCE_SYSTEM_DNDMODE)
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_TOGGLEX:
            if method == mc.METHOD_SETACK:
                # SETACK doesnt carry payload :(
                # on MQTT this is a pain since we dont have a setack callback
                # system in place and we're not sure this SETACK is for us
                pass
            else:
                self._parse_togglex(payload.get(mc.KEY_TOGGLEX))
            return True

        if namespace == mc.NS_APPLIANCE_SYSTEM_DNDMODE:
            if method == mc.METHOD_SETACK:
                # SETACK doesnt carry payload :(
                # on MQTT this is a pain since we dont have a setack callback
                # system in place and we're not sure this SETACK is for us
                pass
            else:
                dndmode = payload.get(mc.KEY_DNDMODE)
                if isinstance(dndmode, dict):
                    self.entity_dnd.update_onoff(dndmode.get(mc.KEY_MODE))
            return True

        if namespace == mc.NS_APPLIANCE_SYSTEM_CLOCK:
            # this is part of initial flow over MQTT
            # we'll try to set the correct time in order to avoid
            # having NTP opened to setup the device
            # Note: I actually see this NS only on mss310 plugs
            # (msl120j bulb doesnt have it)
            if method == mc.METHOD_PUSH:
                self.mqtt_request(
                    mc.NS_APPLIANCE_SYSTEM_CLOCK,
                    mc.METHOD_PUSH,
                    { mc.KEY_CLOCK: { mc.KEY_TIMESTAMP: int(epoch)}}
                )
            return True

        if namespace == mc.NS_APPLIANCE_SYSTEM_TIME:
            if method == mc.METHOD_PUSH:
                self.descriptor.update_time(payload.get(mc.KEY_TIME, {}))
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_BIND:
            """
            this transaction was observed on a trace from a msh300hk
            the device keeps sending 'SET'-'Bind' so I'm trying to
            kindly answer a 'SETACK'
            assumption is we're working on mqtt
            """
            if method == mc.METHOD_SET:
                self.mqtt_request(
                    mc.NS_APPLIANCE_CONTROL_BIND,
                    mc.METHOD_SETACK,
                    {},
                    header[mc.KEY_MESSAGEID]
                )

        return False


    def mqtt_receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> None:
        if self.conf_protocol is Protocol.HTTP:
            return # even if mqtt parsing is no harming we want a 'consistent' HTTP only behaviour
        self.hasmqtt = True
        self._trace(payload, namespace, method, CONF_OPTION_MQTT, TRACE_DIRECTION_RX)
        if (self.pref_protocol is Protocol.MQTT) and (self.curr_protocol is Protocol.HTTP):
            self.switch_protocol(Protocol.MQTT) # will reset 'lastmqtt'
        self.receive(namespace, method, payload, header)
        # self.lastmqtt is checked against to see if we have to request a full state update
        # when coming online. Set it last so we know (inside self.receive) that we're
        # eventually coming from offline
        # self.lastupdate is not updated when we have protocol ERROR!
        self.lastmqtt = self.lastupdate


    def mqtt_disconnected(self) -> None:
        if self.curr_protocol is Protocol.MQTT:
            if self.conf_protocol is Protocol.AUTO:
                self.switch_protocol(Protocol.HTTP)
            # conf_protocol should be Protocol.MQTT:
            elif self._online:
                self._set_offline()


    def mqtt_request(self, namespace: str, method: str, payload: dict, messageid: str = None):
        self._trace(payload, namespace, method, CONF_OPTION_MQTT, TRACE_DIRECTION_TX)
        self.api.mqtt_publish(
            self.device_id,
            namespace,
            method,
            payload,
            self.key,
            messageid
        )


    async def async_http_request(self, namespace: str, method: str, payload: dict, callback: Callable = None):
        try:
            _httpclient:MerossHttpClient = getattr(self, VOLATILE_ATTR_HTTPCLIENT, None)
            if _httpclient is None:
                _httpclient = MerossHttpClient(self.host, self.key, async_get_clientsession(self.api.hass), LOGGER)
                self._httpclient = _httpclient

            for attempt in range(3):
                # since we get 'random' connection errors, this is a retry attempts loop
                # until we get it done. We'd want to break out early on specific events tho (Timeouts)
                self._trace(payload, namespace, method, CONF_OPTION_HTTP, TRACE_DIRECTION_TX)
                try:
                    response = await _httpclient.async_request(namespace, method, payload)
                    break
                except Exception as e:
                    if (not self._online):
                        raise e # manage this error on the external handler
                    self.log(
                        logging.INFO, 0,
                        "MerossDevice(%s) client connection attempt(%s) error in async_http_request: %s",
                        self.device_id, str(attempt), str(e) or type(e).__name__
                    )
                    if (
                        (self.conf_protocol is Protocol.AUTO) and
                        self.lastmqtt and
                        mqtt_is_connected(self.api.hass)
                    ):
                        self.switch_protocol(Protocol.MQTT)
                        self.mqtt_request(namespace, method, payload)
                        return
                    elif isinstance(e, TimeoutError):
                        self._set_offline()
                        return
                    await asyncio.sleep(0.1)# wait a bit before re-issuing request
            else:
                self._set_offline()
                return

            r_header = response[mc.KEY_HEADER]
            r_namespace = r_header[mc.KEY_NAMESPACE]
            r_method = r_header[mc.KEY_METHOD]
            r_payload = response[mc.KEY_PAYLOAD]
            self._trace(r_payload, r_namespace, r_method, CONF_OPTION_HTTP, TRACE_DIRECTION_RX)
            if (callback is not None) and (r_method == mc.METHOD_SETACK):
                #we're actually only using this for SET->SETACK command confirmation
                callback()
            self.receive(r_namespace, r_method, r_payload, r_header)
        except Exception as e:
            self.log(
                logging.WARNING, 14400,
                "MerossDevice(%s) error in async_http_request: %s",
                self.device_id, str(e) or type(e).__name__
            )


    def request(self, namespace: str, method: str, payload: dict, callback: Callable = None):
        """
            route the request through MQTT or HTTP to the physical device.
            callback will be called on successful replies and actually implemented
            only when HTTPing SET requests. On MQTT we rely on async PUSH and SETACK to manage
            confirmation/status updates
        """
        self.lastrequest = time()
        if self.curr_protocol is Protocol.MQTT:
            # only publish when mqtt component is really connected else we'd
            # insanely dump lot of mqtt errors in log
            if mqtt_is_connected(self.api.hass):
                self.mqtt_request(namespace, method, payload)
                return
            # MQTT not connected
            if self.conf_protocol is Protocol.MQTT:
                return
            # protocol is AUTO
            self.switch_protocol(Protocol.HTTP)

        # curr_protocol is HTTP
        self.api.hass.async_create_task(
            self.async_http_request(namespace, method, payload, callback)
            )


    def request_get(self, namespace: str) -> None:
        self.request(
            namespace,
            mc.METHOD_GET,
            build_default_payload_get(namespace)
        )


    def switch_protocol(self, protocol: Protocol) -> None:
        self.log(
            logging.INFO, 0,
            "MerossDevice(%s) switching protocol to %s",
            self.device_id, protocol.name
        )
        self.lastmqtt = 0 # reset so we'll need a new mqtt message to ensure mqtt availability
        self.curr_protocol = protocol


    def log(self, level: int, timeout: int, msg: str, *args):
        if timeout:
            LOGGER_trap(level, timeout, msg, *args)
        else:
            LOGGER.log(level, msg, *args)
        self._trace(msg % args, logging.getLevelName(level), 'LOG')


    def entry_option_setup(self, config_schema: dict):
        """
        called when setting up an OptionsFlowHandler to expose
        configurable device preoperties which are stored at the device level
        and not at the configuration/option level
        see derived implementations
        """
        if self.hasmqtt and (mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability):
            global TIMEZONES_SET
            if TIMEZONES_SET is None:
                try:
                    import zoneinfo
                    TIMEZONES_SET = zoneinfo.available_timezones()
                except Exception:
                    pass
                if TIMEZONES_SET:
                    TIMEZONES_SET = vol.In(sorted(TIMEZONES_SET))
                else:
                    # if error or empty try fallback to pytz if avail
                    try:
                        from pytz import common_timezones
                        TIMEZONES_SET = vol.In(sorted(common_timezones))
                    except Exception:
                        TIMEZONES_SET = str
            config_schema[
                vol.Optional(
                    mc.KEY_TIMEZONE,
                    description={"suggested_value": self.descriptor.timezone}
                    )
                ] = TIMEZONES_SET


    def entry_option_update(self, user_input: dict):
        """
        called when the user 'SUBMIT' an OptionsFlowHandler: here we'll
        receive the full user_input so to update device config properties
        (this is actually called in sequence with entry_update_listener
        just the latter is async)
        """
        if self.hasmqtt and (mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability):
            self._config_timezone(int(time()), user_input.get(mc.KEY_TIMEZONE))


    @callback
    async def entry_update_listener(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """
        callback after user changed configuration through OptionsFlowHandler
        deviceid and/or host are not changed so we're still referring to the same device
        """
        self._set_config_entry(config_entry.data)
        self.api.update_polling_period()
        _httpclient:MerossHttpClient = getattr(self, VOLATILE_ATTR_HTTPCLIENT, None)
        if _httpclient is not None:
            # this is actually unneeded since OptionsFlow doesnt allow editing CONF_HOST ;)
            if self._host:
                _httpclient.host = self._host
            _httpclient.key = self.key
        """
        We'll activate debug tracing only when the user turns it on in OptionsFlowHandler so we usually
        don't care about it on startup ('_set_config_entry'). When updating ConfigEntry
        we always reset the timeout and so the trace will (eventually) restart
        """
        if self._trace_file is not None:
            self._trace_close()
        _trace_endtime = config_entry.data.get(CONF_TRACE, 0)
        if _trace_endtime > time():
            try:
                tracedir = hass.config.path('custom_components', DOMAIN, CONF_TRACE_DIRECTORY)
                os.makedirs(tracedir, exist_ok=True)
                self._trace_file = open(os.path.join(tracedir, CONF_TRACE_FILENAME.format(self.descriptor.type, int(_trace_endtime))), 'w')
                self._trace_endtime = _trace_endtime
                self._trace(self.descriptor.all, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GETACK)
                self._trace(self.descriptor.ability, mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.METHOD_GETACK)
                self._trace_ability_iter = iter(self.descriptor.ability)
                self._trace_ability()
            except Exception as e:
                LOGGER.warning("MerossDevice(%s) error while creating trace file (%s)", self.device_id, str(e))

        #await hass.config_entries.async_reload(config_entry.entry_id)


    @callback
    def updatecoordinator_listener(self):
        epoch = time()
        """
        this is a bit rude: we'll keep sending 'heartbeats'
        to check if the device is still there
        !!this is mainly for MQTT mode since in HTTP we'll more or less poll
        unless the device went offline so we started skipping polling updates
        """
        if ((epoch - self.lastrequest) > PARAM_HEARTBEAT_PERIOD) \
            and ((epoch - self.lastupdate) > PARAM_HEARTBEAT_PERIOD):
            self.request_get(mc.NS_APPLIANCE_SYSTEM_ALL)
            return

        if self.online:
            if (epoch - self.lastpoll) < self.polling_period:
                return
            self.lastpoll = math.floor(epoch)
            self._request_updates(epoch, None)

        else:# offline
            # when we 'stall' offline while on MQTT eventually retrigger HTTP
            # the reverse is not needed since we switch HTTP -> MQTT right-away
            # when HTTP fails (see async_http_request)
            if (self.curr_protocol is Protocol.MQTT) and (self.conf_protocol is Protocol.AUTO):
                self.switch_protocol(Protocol.HTTP)
            if (epoch - self.lastrequest) > self._retry_period:
                self._retry_period = self._retry_period + self.polling_period
                self.request_get(mc.NS_APPLIANCE_SYSTEM_ALL)


    def _parse_togglex(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL, 0)].update_onoff(payload.get(mc.KEY_ONOFF))
        elif isinstance(payload, list):
            for p in payload:
                self._parse_togglex(p)


    def _parse_all(self, payload: dict) -> None:
        """
        called internally when we receive an NS_SYSTEM_ALL
        i.e. global device setup/status
        we usually don't expect a 'structural' change in the device here
        except maybe for Hub(s) which we're going to investigate later
        set 'self.needsave' if we want to persist the payload to the ConfigEntry
        """
        descr = self.descriptor
        oldaddr = descr.innerIp
        descr.update(payload)
        #persist changes to configentry only when relevant properties change
        newaddr = descr.innerIp
        if oldaddr != newaddr:
            #check the new innerIp is good since we have random blanks in the wild (#90)
            try:
                socket.inet_aton(newaddr)
                # good enough..check if we're using an MQTT device (i.e. device with no CONF_HOST)
                # and eventually cache this value so we could use it when falling back to HTTP
                if not self._host:
                    _httpclient:MerossHttpClient = getattr(self, VOLATILE_ATTR_HTTPCLIENT, None)
                    if _httpclient is not None:
                        _httpclient.host = newaddr

                self.needsave = True
            except:
                pass

        epoch = int(self.lastupdate) # we're not calling time() since it's fresh enough

        if self.hasmqtt:
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            if self.device_timedelta \
                and mc.NS_APPLIANCE_SYSTEM_CLOCK in descr.ability:
                #timestamp misalignment: try to fix it
                #only when devices are paired on our MQTT
                self.request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})

            if mc.NS_APPLIANCE_SYSTEM_TIME in descr.ability:
                # check the appliance timeoffsets are updated (see #36)
                self._config_timezone(epoch, descr.time.get(mc.KEY_TIMEZONE))

        for key, value in descr.digest.items():
            _parse = getattr(self, f"_parse_{key}", None)
            if _parse is not None:
                _parse(value)


    def _config_timestamp(self, epoch, device_timedelta):
        if abs(self.device_timedelta - device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            self.device_timedelta = device_timedelta
        else: # average the sampled timedelta
            self.device_timedelta = (4 * self.device_timedelta + device_timedelta) / 5
        if self.hasmqtt \
            and mc.NS_APPLIANCE_SYSTEM_CLOCK in self.descriptor.ability:
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            last_config_delay = epoch - self.device_timedelta_config_epoch
            if last_config_delay > 1800:
                # 30 minutes 'cooldown' in order to avoid restarting
                # the prcedure too often
                self.request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})
                self.device_timedelta_config_epoch = epoch
                return
            if last_config_delay < 30:
                # 30 sec 'deadzone' where we allow the timestamp
                # transaction to complete (should really be like few seconds)
                return
        if (epoch - self.device_timedelta_log_epoch) > 604800: # 1 week lockout
            self.device_timedelta_log_epoch = epoch
            self.log(
                logging.WARNING, 0,
                "MerossDevice(%s) has incorrect timestamp: %d seconds behind HA",
                self.device_id, int(self.device_timedelta)
        )


    def _config_timezone(self, epoch, timezone) -> None:
        p_time: dict = self.descriptor.time
        p_timerule: list = p_time.get(mc.KEY_TIMERULE, [])
        p_timezone: str = p_time.get(mc.KEY_TIMEZONE)
        """
        timeRule should contain 2 entries: the actual time offsets and
        the next (incoming). If 'now' is after 'incoming' it means the
        first entry became stale and so we'll update the daylight offsets
        to current/next DST time window
        """
        if (p_timezone != timezone) \
            or len(p_timerule) < 2 \
            or p_timerule[1][0] < epoch:

            if timezone:
                """
                we'll look through the list of transition times for current tz
                and provide the actual (last past daylight) and the next to the
                appliance so it knows how and when to offset utc to localtime
                """
                timerules = list()
                try:
                    import pytz
                    import bisect
                    tz_local = pytz.timezone(timezone)
                    idx = bisect.bisect_right(
                        tz_local._utc_transition_times,
                        datetime.utcfromtimestamp(epoch)
                    )
                    # idx would be the next transition offset index
                    _transition_info = tz_local._transition_info[idx-1]
                    timerules.append([
                        int(tz_local._utc_transition_times[idx-1].timestamp()),
                        int(_transition_info[0].total_seconds()),
                        1 if _transition_info[1].total_seconds() else 0
                    ])
                    _transition_info = tz_local._transition_info[idx]
                    timerules.append([
                        int(tz_local._utc_transition_times[idx].timestamp()),
                        int(_transition_info[0].total_seconds()),
                        1 if _transition_info[1].total_seconds() else 0
                    ])
                except Exception as e:
                    self.log(
                        logging.WARNING, 0,
                        "MerossDevice(%s) error while building timezone info (%s)",
                        self.device_id, str(e)
                    )
                    timerules = [[0, 0, 0], [epoch + PARAM_TIMEZONE_CHECK_PERIOD, 0, 1]]

                self.request(
                    mc.NS_APPLIANCE_SYSTEM_TIME,
                    mc.METHOD_SET,
                    payload={
                        mc.KEY_TIME: {
                            mc.KEY_TIMEZONE: timezone,
                            mc.KEY_TIMERULE: timerules
                        }
                    }
                )
            elif p_timezone: # and !timezone
                self.request(
                    mc.NS_APPLIANCE_SYSTEM_TIME,
                    mc.METHOD_SET,
                    payload={
                        mc.KEY_TIME: {
                            mc.KEY_TIMEZONE: '',
                            mc.KEY_TIMERULE: []
                        }
                    }
                )


    def _set_offline(self) -> None:
        self.log(
            logging.DEBUG, 0,
            "MerossDevice(%s) going offline!",
            self.device_id
        )
        self._online = False
        self._retry_period = 0
        self.lastmqtt = 0
        for entity in self.entities.values():
            entity.set_unavailable()


    def _set_online(self, namespace: str) -> None:
        """
            When coming back online allow for a refresh
            also in inheriteds. Pass received namespace along
            so to decide what to refresh (see 'updatecoordinator_listener')
        """
        self.log(
            logging.DEBUG, 0,
            "MerossDevice(%s) back online!",
            self.device_id
        )
        self._online = True
        self._request_updates(time(), namespace)


    def _request_updates(self, epoch, namespace):
        """
        This is a 'versatile' polling strategy called on timer through DataUpdateCoordinator
        or when the device comes online (passing in the received namespace)
        When the device doesnt listen MQTT at all this will always fire the list of requests
        else, when MQTT is alive this will fire the requests only once when just switching online
        or when not listening any MQTT over the PARAM_HEARTBEAT_PERIOD
        """
        if (epoch - self.lastmqtt) > PARAM_HEARTBEAT_PERIOD:
            for ns in self.polling_dictionary:
                if ns != namespace:
                    self.request_get(ns)


    def _save_config_entry(self, payload: dict) -> None:
        try:
            entries:ConfigEntries = self.api.hass.config_entries
            entry:ConfigEntry = entries.async_get_entry(self.entry_id)
            if entry is not None:
                data = dict(entry.data) # deepcopy? not needed: see CONF_TIMESTAMP
                data[CONF_PAYLOAD].update(payload)
                data[CONF_TIMESTAMP] = time() # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)
        except Exception as e:
            self.log(
                logging.WARNING, 0,
                "MerossDevice(%s) error while updating ConfigEntry (%s)",
                self.device_id, str(e)
            )


    def _set_config_entry(self, data: dict) -> None:
        """
        common properties read from ConfigEntry on __init__ or when a configentry updates
        """
        self._host = data.get(CONF_HOST)
        self.key = data.get(CONF_KEY)
        self.conf_protocol = MAP_CONF_PROTOCOL.get(data.get(CONF_PROTOCOL), Protocol.AUTO)
        if self.conf_protocol == Protocol.AUTO:
            self.pref_protocol = Protocol.HTTP if self._host else Protocol.MQTT
        else:
            self.pref_protocol = self.conf_protocol
        """
        When using Protocol.AUTO we try to use our 'preferred' (pref_protocol)
        and eventually fallback (curr_protocol) until some good news allow us
        to retry pref_protocol
        """
        self.curr_protocol = self.pref_protocol
        self.lastmqtt = 0 # reset mqtt availability indicator
        self.hasmqtt = (self.conf_protocol != Protocol.HTTP) and (self.hasmqtt or (self.pref_protocol == Protocol.MQTT))
        self.polling_period = data.get(CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT)
        if self.polling_period < CONF_POLLING_PERIOD_MIN:
            self.polling_period = CONF_POLLING_PERIOD_MIN


    def _trace_close(self):
        try:
            self._trace_file.close()
        except Exception as e:
            LOGGER.warning("MerossDevice(%s) error while closing trace file (%s)", self.device_id, str(e))
        self._trace_file = None
        self._trace_ability_iter = None

    @callback
    def _trace_ability(self, *args):
        if self._trace_ability_iter is None:
            return
        try:
            while True:
                ability:str = next(self._trace_ability_iter)
                if ability not in TRACE_ABILITY_EXCLUDE:
                    self.request_get(ability)
                    break

            async_track_point_in_utc_time(
                self.api.hass,
                self._trace_ability,
                datetime.fromtimestamp(time() + 5)
            )
        except:# finished ?!
            self._trace_ability_iter = None


    def _trace(
        self,
        data: str | dict,
        namespace: str = '',
        method: str = '',
        protocol = CONF_OPTION_AUTO,
        rxtx = ''
        ):
        if self._trace_file is not None:
            now = time()
            if now > self._trace_endtime:
                self._trace_close()
                return

            if isinstance(data, dict):
                obfuscated = _obfuscate(data)

            try:
                self._trace_file.write(strftime('%Y/%m/%d - %H:%M:%S\t') \
                    + rxtx + '\t' + protocol + '\t' + method + '\t' + namespace + '\t' \
                    + (json_dumps(data) if isinstance(data, dict) else data) + '\r\n')
                if self._trace_file.tell() > CONF_TRACE_MAXSIZE:
                    self._trace_close()
            except Exception as e:
                LOGGER.warning("MerossDevice(%s) error while writing to trace file (%s)", self.device_id, str(e))
                self._trace_close()

            if isinstance(data, dict):
                _deobfuscate(data, obfuscated)
