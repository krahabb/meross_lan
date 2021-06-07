from enum import Enum
from typing import  Callable, Dict
from time import time
from logging import WARNING, DEBUG
from aiohttp.client_exceptions import ClientConnectionError, ClientConnectorError

from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    CONF_HOST
)

from .logger import LOGGER, LOGGER_trap

from .const import (
    CONF_DEVICE_ID, CONF_KEY, CONF_PAYLOAD, CONF_PROTOCOL,
    CONF_OPTION_AUTO, CONF_OPTION_HTTP, CONF_OPTION_MQTT, CONF_TIMESTAMP,
    PARAM_UNAVAILABILITY_TIMEOUT, PARAM_HEARTBEAT_PERIOD
)

from .merossclient import KeyType, MerossDeviceDescriptor, MerossHttpClient, const as mc  # mEROSS cONST


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
        self.lastrequest = 0
        self.lastupdate = 0
        self.lastmqtt = None
        """
        self.entities: dict()
        is a collection of all of the instanced entities
        they're generally build here during __init__ and will be registered
        in platforms(s) async_setup_entry with HA
        """
        self.entities: Dict[any, '_MerossEntity'] = dict()  # pylint: disable=undefined-variable
        """
        self.platforms: dict()
        when we build an entity we also add the relative platform name here
        so that the async_setup_entry for the integration will be able to forward
        the setup to the appropriate platform.
        The item value here will be set to the async_add_entities callback
        during the corresponfing platform async_setup_entry so to be able
        to dynamically add more entities should they 'pop-up' (Hub only?)
        """
        self.platforms: Dict[str, Callable] = {}
        """
        misc callbacks
        """
        self.unsub_entry_update_listener: Callable = None
        self.unsub_updatecoordinator_listener: Callable = None

        self._set_config_entry(entry)
        """
        warning: would the response be processed after this object is fully init?
        It should if I get all of this async stuff right
        also: !! IMPORTANT !! don't send any other message during init process
        else the responses could overlap and 'fuck' a bit the offline -> online transition
        causing that code to request a new NS_APPLIANCE_SYSTEM_ALL
        """
        self.request(mc.NS_APPLIANCE_SYSTEM_ALL)


    def __del__(self):
        LOGGER.debug("MerossDevice(%s) destroy", self.device_id)
        return


    @property
    def online(self) -> bool:
        if self._online:
            #evaluate device MQTT availability by checking lastrequest got answered in less than 20 seconds
            if (self.lastupdate > self.lastrequest) or ((time() - self.lastrequest) < PARAM_UNAVAILABILITY_TIMEOUT):
                return True

            # when we 'fall' offline while on MQTT eventually retrigger HTTP.
            # the reverse is not needed since we switch HTTP -> MQTT right-away
            # when HTTP fails (see async_http_request)
            if (self.curr_protocol is Protocol.MQTT) and (self.conf_protocol is Protocol.AUTO):
                self._switch_protocol(Protocol.HTTP)
                return True

            self._set_offline()

        return False


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        replykey: KeyType
    ) -> bool:
        """
        every time we receive a response we save it's 'replykey':
        that would be the same as our self.key (which it is compared against in 'get_replykey')
        if it's good else it would be the device message header to be used in
        a reply scheme where we're going to 'fool' the device by using its own hashes
        if our config allows for that (our self.key is 'None' which means empty key or auto-detect)

        Update: this key trick actually doesnt work on MQTT (but works on HTTP)
        """
        self.replykey = replykey
        if self.key and (replykey != self.key):
            LOGGER_trap(WARNING, 14400, "Meross device key error for device_id: %s", self.device_id)

        self.lastupdate = time()
        if not self._online:
            if namespace != mc.NS_APPLIANCE_SYSTEM_ALL:
                self.request(mc.NS_APPLIANCE_SYSTEM_ALL)
            self._set_online()

        if namespace == mc.NS_APPLIANCE_CONTROL_TOGGLEX:
            self._parse_togglex(payload)
            return True

        if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
            if self._update_descriptor(payload):
                self._save_config_entry(payload)
            return True

        return False


    def mqtt_receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        replykey: KeyType
    ) -> None:
        self.lastmqtt = time()
        if (self.pref_protocol is Protocol.MQTT) and (self.curr_protocol is Protocol.HTTP):
            self._switch_protocol(Protocol.MQTT)
        self.receive(namespace, method, payload, replykey)


    async def async_http_request(self, namespace: str, method: str, payload: dict = {}, callback: Callable = None):
        try:
            _httpclient:MerossHttpClient = getattr(self, '_httpclient', None)
            if _httpclient is None:
                _httpclient = MerossHttpClient(self.descriptor.ipAddress, self.key, async_get_clientsession(self.api.hass), LOGGER)
                self._httpclient = _httpclient
            else:
                _httpclient.set_host(self.descriptor.ipAddress)
                _httpclient.key = self.key

            response = await _httpclient.async_request(namespace, method, payload)
            r_header = response[mc.KEY_HEADER]
            r_namespace = r_header[mc.KEY_NAMESPACE]
            r_method = r_header[mc.KEY_METHOD]
            if (callback is not None) and (r_method == mc.METHOD_SETACK):
                #we're actually only using this for SET->SETACK command confirmation
                callback()
            # passing self.key to shut off MerossDevice replykey behaviour
            # since we're already managing replykey in http client
            self.receive(r_namespace, r_method, response[mc.KEY_PAYLOAD], self.key)
        except ClientConnectionError as e:
            LOGGER.info("MerossDevice(%s) client connection error in async_http_request: %s", self.device_id, str(e))
            if self._online:
                if (self.pref_protocol is Protocol.MQTT) or (self.lastmqtt is not None):
                    # this device was either 'discovered' over MQTT or, somehow,
                    # received an MQTT message so it could be able to talk MQTT
                    self._switch_protocol(Protocol.MQTT)
                    self.api.mqtt_publish(
                        self.device_id,
                        namespace,
                        method,
                        payload,
                        self.key or self.replykey
                        )
                else:
                    self._set_offline()
        except Exception as e:
            LOGGER.warning("MerossDevice(%s) error in async_http_request: %s", self.device_id, str(e))


    def request(self, namespace: str, method: str = mc.METHOD_GET, payload: dict = {}, callback: Callable = None):
        """
            route the request through MQTT or HTTP to the physical device.
            callback will be called on successful replies and actually implemented
            only when HTTPing SET requests. On MQTT we rely on async PUSH and SETACK to manage
            confirmation/status updates
        """
        self.lastrequest = time()
        if self.curr_protocol is Protocol.HTTP:
            self.api.hass.async_create_task(
                self.async_http_request(namespace, method, payload, callback)
            )
        else: # self.curr_protocol is Protocol.MQTT:
            self.api.mqtt_publish(
                self.device_id,
                namespace,
                method,
                payload,
                self.key or self.replykey
            )


    def _set_offline(self) -> None:
        LOGGER.debug("MerossDevice(%s) going offline!", self.device_id)
        self._online = False
        for entity in self.entities.values():
            entity._set_unavailable()


    def _set_online(self) -> None:
        """
            When coming back online allow for a refresh
            also in inheriteds
        """
        LOGGER.debug("MerossDevice(%s) back online!", self.device_id)
        self._online = True
        self.updatecoordinator_listener()


    def _switch_protocol(self, protocol: Protocol) -> None:
        LOGGER.info("MerossDevice(%s) switching protocol to %s", self.device_id, protocol.name)
        self.curr_protocol = protocol


    def _parse_togglex(self, payload: dict) -> None:
        togglex = payload.get(mc.KEY_TOGGLEX)
        if isinstance(togglex, list):
            for t in togglex:
                self.entities[t.get(mc.KEY_CHANNEL)]._set_onoff(t.get(mc.KEY_ONOFF))
        elif isinstance(togglex, dict):
            self.entities[togglex.get(mc.KEY_CHANNEL)]._set_onoff(togglex.get(mc.KEY_ONOFF))


    def _update_descriptor(self, payload: dict) -> bool:
        """
        called internally when we receive an NS_SYSTEM_ALL
        i.e. global device setup/status
        we usually don't expect a 'structural' change in the device here
        except maybe for Hub(s) which we're going to investigate later
        Return True if we want to persist the payload to the ConfigEntry
        """
        oldaddr = self.descriptor.ipAddress
        self.descriptor.update(payload)

        p_digest = self.descriptor.digest
        if p_digest:
            self._parse_togglex(p_digest)

        #persist changes to configentry only when relevant properties change
        return oldaddr != self.descriptor.ipAddress


    def _save_config_entry(self, payload: dict) -> None:
        try:
            entries:ConfigEntries = self.api.hass.config_entries
            entry:ConfigEntry = entries.async_get_entry(self.entry_id)
            if entry is not None:
                data = dict(entry.data) # deepcopy? not needed: see CONF_TIMESTAMP
                data[CONF_PAYLOAD].update(payload)
                data[CONF_TIMESTAMP] = int(time()) # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)
        except Exception as e:
            LOGGER.warning("MerossDevice(%s) error while updating ConfigEntry (%s)", self.device_id, str(e))


    def _set_config_entry(self, config_entry: ConfigEntry) -> None:
        """
        common properties read from ConfigEntry on __init__ or when a configentry updates
        """
        self.key = config_entry.data.get(CONF_KEY)
        self.conf_protocol = MAP_CONF_PROTOCOL.get(config_entry.data.get(CONF_PROTOCOL), Protocol.AUTO)
        if self.conf_protocol == Protocol.AUTO:
            self.pref_protocol = Protocol.HTTP if config_entry.data.get(CONF_HOST) else Protocol.MQTT
        else:
            self.pref_protocol = self.conf_protocol
        """
        When using Protocol.AUTO we try to use our 'preferred' (pref_protocol)
        and eventually fallback (curr_protocol) until some good news allow us
        to retry pref_protocol
        """
        self.curr_protocol = self.pref_protocol


    @callback
    async def entry_update_listener(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        # we're not changing device_id or other 'identifying' stuff
        self._set_config_entry(config_entry)
        _httpclient:MerossHttpClient = getattr(self, '_httpclient', None)
        if _httpclient is not None:
            _httpclient.set_host(self.descriptor.ipAddress)
            _httpclient.key = self.key

        #await hass.config_entries.async_reload(config_entry.entry_id)

    @callback
    def updatecoordinator_listener(self) -> bool:
        now = time()
        """
        this is a bit rude: we'll keep sending 'heartbeats'
        to check if the device is still there
        !!this is actually not happening when we connect through HTTP!!
        unless the device went offline so we started skipping polling updates
        """
        if (now - self.lastrequest) > PARAM_HEARTBEAT_PERIOD:
            self.request(mc.NS_APPLIANCE_SYSTEM_ALL)
            return False

        if self.online:
            # on MQTT we already have PUSHES...
            if self.curr_protocol == Protocol.HTTP:
                ability = self.descriptor.ability
                if mc.NS_APPLIANCE_CONTROL_TOGGLEX in ability:
                    self.request(mc.NS_APPLIANCE_CONTROL_TOGGLEX, payload={ mc.KEY_TOGGLEX : [] })
                elif mc.NS_APPLIANCE_CONTROL_TOGGLE in ability:
                    self.request(mc.NS_APPLIANCE_CONTROL_TOGGLE, payload={ mc.KEY_TOGGLE : {} })
                if mc.NS_APPLIANCE_CONTROL_LIGHT in ability:
                    self.request(mc.NS_APPLIANCE_CONTROL_LIGHT, payload={ mc.KEY_LIGHT : {} })

            return True # tell inheriting to continue processing

        # when we 'stall' offline while on MQTT eventually retrigger HTTP
        # the reverse is not needed since we switch HTTP -> MQTT right-away
        # when HTTP fails (see async_http_request)
        if (self.curr_protocol is Protocol.MQTT) and (self.conf_protocol is Protocol.AUTO):
            self._switch_protocol(Protocol.HTTP)
            self.request(mc.NS_APPLIANCE_SYSTEM_ALL)

        return False