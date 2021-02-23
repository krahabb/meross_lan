from typing import Any, Callable, Dict, List, Optional

from hashlib import md5
from time import time, strftime, localtime
from uuid import uuid4
import json

from homeassistant.components.switch import SwitchEntity, DEVICE_CLASS_OUTLET
from .const import *

def build_payload(namespace: str, method: str, payload: Any):
    messageid = uuid4().hex
    timestamp = int(time())
    p = {
            "header": {
                "messageId": messageid,
                "namespace": namespace,
                "method": method,
                "payloadVersion": 1,
                #"from": "/appliance/1909182170548290802048e1e9522946/publish",
                "timestamp": timestamp,
                "timestampMs": 0,
                "sign": md5((messageid + str(timestamp)).encode('utf-8')).hexdigest()
            },
            "payload": payload
        }
    return json.dumps(p)

class MerossDevice:


    def __init__(self, device_id: str, discoverypayload: Dict, async_mqtt_publish: Callable):
        self._device_id = device_id
        self._async_mqtt_publish = async_mqtt_publish  #provide the standard hass MQTT async_public signature

        self.ability = discoverypayload.get("ability", {})
        self.switches: [MerossLanSwitch] = []
        self._online = False
        self.lastrequest = 0
        self.lastupdate = 0
        self.lastupdate_consumption = 0

        try:
            togglex = discoverypayload.get("all", {}).get("digest", {}).get("togglex")
            if isinstance(togglex, List):
                for t in togglex:
                    self.switches.append(MerossLanSwitch(self, t.get("channel")))
            elif isinstance(togglex, Dict):
                self.switches.append(MerossLanSwitch(self, togglex.get("channel")))
            elif (NS_APPLIANCE_CONTROL_TOGGLE in self.ability) or (NS_APPLIANCE_CONTROL_TOGGLEX in self.ability):
                #fallback for switches: in case we couldnt get from NS_APPLIANCE_SYSTEM_ALL
                self.switches.append(MerossLanSwitch(self, 0))
        except:
            pass



        return


    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def online(self) -> bool:
        #evaluate device MQTT availability by checking lastrequest got answered in less than 10
        if (self.lastupdate > self.lastrequest) or ((time() - self.lastrequest) < 10):
            if not (self._online):
                self._online = True
                for switch in self.switches:
                    switch._set_available()
            return True
        #else
        if self._online:
            self._online = False
            for switch in self.switches:
                switch._set_unavailable()
        return False


    def triggerupdate(self) -> None:
        if not(self.online):
            self._mqtt_publish(NS_APPLIANCE_SYSTEM_ONLINE, METHOD_GET)
            return

        now = time()

        if NS_APPLIANCE_CONTROL_ELECTRICITY in self.ability:
            self._mqtt_publish(NS_APPLIANCE_CONTROL_ELECTRICITY, METHOD_GET)

        if NS_APPLIANCE_CONTROL_CONSUMPTIONX in self.ability:
            if ((now - self.lastupdate_consumption) > 60):
                self._mqtt_publish(NS_APPLIANCE_CONTROL_CONSUMPTIONX, METHOD_GET)

        return

    def parsepayload(self, namespace: str, method: str, payload: Any) -> None:
        try:
            self.lastupdate = time()

            if namespace == NS_APPLIANCE_CONTROL_TOGGLEX:
                togglex = payload.get("togglex")
                if isinstance(togglex, List):
                    for t in togglex:
                        self.switches[t.get("channel")]._set_is_on(t.get("onoff") == 1)
                elif isinstance(togglex, Dict):
                    self.switches[togglex.get("channel")]._set_is_on(togglex.get("onoff") == 1)
            elif namespace == NS_APPLIANCE_CONTROL_ELECTRICITY:
                electricity = payload.get("electricity")
                self.switches[electricity.get("channel")]._set_power(
                    electricity.get("power") / 1000,
                    electricity.get("voltage") / 10,
                    electricity.get("current") / 1000
                    )
            elif namespace == NS_APPLIANCE_CONTROL_CONSUMPTIONX:
                self.lastupdate_consumption = self.lastupdate
                daylabel = strftime("%Y-%m-%d", localtime())
                for d in payload.get("consumptionx"):
                    if d.get("date") == daylabel:
                        self.switches[0]._set_energy(d.get("value") / 1000)

        except:
            pass

        return


    def togglex_set(self, channel: int, ison: int):
        return self._mqtt_publish(
            NS_APPLIANCE_CONTROL_TOGGLEX,
            METHOD_SET,
            {"togglex": {"channel": channel, "onoff": ison}}
        )

    def togglex_get(self, channel: int):
        return self._mqtt_publish(
            NS_APPLIANCE_CONTROL_TOGGLEX,
            METHOD_GET,
            {"togglex": {"channel": channel}}
        )


    def _mqtt_publish(self, namespace: str, method: str, payload: Dict = {}):
        # self.lastrequest should represent the time of the most recent un-responded request
        if self.lastupdate >= self.lastrequest:
            self.lastrequest = time()
        mqttpayload = build_payload(namespace, method, payload)
        return self._async_mqtt_publish(COMMAND_TOPIC.format(self._device_id), mqttpayload, 0, False)

class MerossLanSwitch(SwitchEntity):
    def __init__(self, meross_device: object, channel: int):
        self._meross_device = meross_device
        self._channel = channel
        self._is_on = None
        self._current_power_w = None
        self._current_voltage_v = None
        self._current_current_a = None
        self._today_energy_kwh = None

    @property
    def unique_id(self) -> Optional[str]:
        """Return a unique id identifying the entity."""
        return f"{self._meross_device.device_id}_{self._channel}"

    # To link this entity to the  device, this property must return an
    # identifiers value matching that used in the cover, but no other information such
    # as name. If name is returned, this entity will then also become a device in the
    # HA UI.
    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._meross_device.device_id)
            }
        }

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return DEVICE_CLASS_OUTLET

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._is_on != None

    @property
    def assumed_state(self) -> bool:
        """Return true if we do optimistic updates."""
        return False

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._is_on

    @property
    def current_power_w(self):
        """Return the current power usage in W."""
        return self._current_power_w

    @property
    def today_energy_kwh(self):
        """Return the today total energy usage in kWh."""
        return self._today_energy_kwh

    @property
    def state_attributes(self):
        data = super().state_attributes
        if self._current_voltage_v is not None:
            data["current_voltage_v"] = self._current_voltage_v
        if self._current_current_a is not None:
            data["current_current_a"] = self._current_current_a
        return data

    async def async_added_to_hass(self) -> None:
        self._meross_device.togglex_get(self._channel)
        return

    async def async_will_remove_from_hass(self) -> None:
        self._is_on = None
        self._current_current_a = None
        self._current_voltage_v = None
        self._current_power_w = None
        self._today_energy_kwh = None
        return

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the entity on."""
        ##return await self.sendmessage(1)
        return self._meross_device.togglex_set(self._channel, 1)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the entity off."""
        #return await self.sendmessage(0)
        return self._meross_device.togglex_set(self._channel, 0)

    def _set_available(self) -> None:
        if self.enabled:
            self._meross_device.togglex_get(self._channel)
        return

    def _set_unavailable(self) -> None:
        if self.enabled and self.available:
            self._is_on = None
            self._current_current_a = None
            self._current_voltage_v = None
            self._current_power_w = None
            self._today_energy_kwh = None
            self.async_write_ha_state()
        return

    def _set_is_on(self, is_on: Optional[bool]) -> None:
        if self.enabled:
            self._is_on = is_on
            self.async_write_ha_state()
        return

    def _set_power(self, power: float, voltage: float, current: float) -> None:
        if self.enabled:
            self._current_power_w = power
            self._current_voltage_v = voltage
            self._current_current_a = current
            self.async_write_ha_state()
        return

    def _set_energy(self, energy: float) -> None:
        if self.enabled:
            self._today_energy_kwh = energy
            self.async_write_ha_state()
        return

