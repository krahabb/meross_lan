

from time import localtime, strftime, time


from homeassistant.core import callback
from homeassistant.const import (
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_VOLTAGE,
    DEVICE_CLASS_ENERGY,
)

from .merossclient import KeyType, const as mc  # mEROSS cONST
from .meross_device import MerossDevice
from .logger import LOGGER, LOGGER_trap
from .sensor import MerossLanSensor
from .switch import MerossLanSwitch
from .cover import MerossLanGarage, MerossLanRollerShutter
from .const import PARAM_ENERGY_UPDATE_PERIOD


class MerossDeviceSwitch(MerossDevice):

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)
        self._lastupdate_consumption = 0
        self._sensor_power = None
        self._sensor_current = None
        self._sensor_voltage = None
        self._sensor_energy = None

        try:
            # use a mix of heuristic to detect device features
            p_digest = self.descriptor.digest
            if p_digest:

                garagedoor = p_digest.get(mc.KEY_GARAGEDOOR)
                if isinstance(garagedoor, list):
                    for g in garagedoor:
                        MerossLanGarage(self, g.get(mc.KEY_CHANNEL))

                # atm we're not sure we can detect this in 'digest' payload
                if "mrs" in self.descriptor.type.lower():
                    MerossLanRollerShutter(self, 0)

                # at any rate: setup switches whenever we find 'togglex'
                # or whenever we cannot setup anything from digest
                togglex = p_digest.get(mc.KEY_TOGGLEX)
                if isinstance(togglex, list):
                    for t in togglex:
                        channel = t.get(mc.KEY_CHANNEL)
                        if channel not in self.entities:
                            MerossLanSwitch(
                                self,
                                channel,
                                mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                                mc.KEY_TOGGLEX)
                elif isinstance(togglex, dict):
                    channel = togglex.get(mc.KEY_CHANNEL)
                    if channel not in self.entities:
                        MerossLanSwitch(
                            self,
                            channel,
                            mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                            mc.KEY_TOGGLEX)

                #endif p_digest

            # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
            # but have 'control'
            p_control = self.descriptor.all.get(mc.KEY_CONTROL) if p_digest is None else None
            if p_control:
                p_toggle = p_control.get(mc.KEY_TOGGLE)
                if isinstance(p_toggle, dict):
                    MerossLanSwitch(
                        self,
                        p_toggle.get(mc.KEY_CHANNEL, 0),
                        mc.NS_APPLIANCE_CONTROL_TOGGLE,
                        mc.KEY_TOGGLE)

            #fallback for switches: in case we couldnt get from NS_APPLIANCE_SYSTEM_ALL
            if not self.entities:
                if mc.NS_APPLIANCE_CONTROL_TOGGLEX in self.descriptor.ability:
                    MerossLanSwitch(
                        self,
                        0,
                        mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                        mc.KEY_TOGGLEX)
                elif mc.NS_APPLIANCE_CONTROL_TOGGLE in self.descriptor.ability:
                    MerossLanSwitch(
                        self,
                        0,
                        mc.NS_APPLIANCE_CONTROL_TOGGLE,
                        mc.KEY_TOGGLE)

            if mc.NS_APPLIANCE_CONTROL_ELECTRICITY in self.descriptor.ability:
                self._sensor_power = MerossLanSensor(self, DEVICE_CLASS_POWER, DEVICE_CLASS_POWER)
                self._sensor_current = MerossLanSensor(self, DEVICE_CLASS_CURRENT, DEVICE_CLASS_CURRENT)
                self._sensor_voltage = MerossLanSensor(self, DEVICE_CLASS_VOLTAGE, DEVICE_CLASS_VOLTAGE)

            if mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX in self.descriptor.ability:
                self._sensor_energy = MerossLanSensor(self, DEVICE_CLASS_ENERGY, DEVICE_CLASS_ENERGY)

        except Exception as e:
            LOGGER.warning("MerossDeviceSwitch(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        replykey: KeyType
    ) -> bool:

        if super().receive(namespace, method, payload, replykey):
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_TOGGLE:
            p_toggle = payload.get(mc.KEY_TOGGLE)
            if isinstance(p_toggle, dict):
                self.entities[p_toggle.get(mc.KEY_CHANNEL, 0)]._set_onoff(p_toggle.get(mc.KEY_ONOFF))
            return True

        if namespace == mc.NS_APPLIANCE_GARAGEDOOR_STATE:
            garagedoor = payload.get(mc.KEY_STATE)
            for g in garagedoor:
                self.entities[g.get(mc.KEY_CHANNEL)]._set_open(g.get(mc.KEY_OPEN))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_STATE:
            state = payload.get(mc.KEY_STATE)
            for s in state:
                self.entities[s.get(mc.KEY_CHANNEL)]._set_rollerstate(s.get(mc.KEY_STATE))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION:
            position = payload.get(mc.KEY_POSITION)
            for p in position:
                self.entities[p.get(mc.KEY_CHANNEL)]._set_rollerposition(p.get(mc.KEY_POSITION))
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_ELECTRICITY:
            electricity = payload.get(mc.KEY_ELECTRICITY)
            self._sensor_power._set_state(electricity.get(mc.KEY_POWER) / 1000)
            self._sensor_current._set_state(electricity.get(mc.KEY_CURRENT) / 1000)
            self._sensor_voltage._set_state(electricity.get(mc.KEY_VOLTAGE) / 10)
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX:
            self._lastupdate_consumption = self.lastupdate
            daylabel = strftime("%Y-%m-%d", localtime())
            for d in payload.get(mc.KEY_CONSUMPTIONX):
                if d.get(mc.KEY_DATE) == daylabel:
                    self._sensor_energy._set_state(d.get(mc.KEY_VALUE))
                    break
            else:
                self._sensor_energy._set_state(0)
            return True

        return False


    def _update_descriptor(self, payload: dict) -> bool:
        update = super()._update_descriptor(payload)

        p_digest = self.descriptor.digest
        if p_digest:
            p_garagedoor = p_digest.get(mc.KEY_GARAGEDOOR)
            if isinstance(p_garagedoor, list):
                for g in p_garagedoor:
                    self.entities[g.get(mc.KEY_CHANNEL)]._set_open(g.get(mc.KEY_OPEN))
        else:
            # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
            p_control = self.descriptor.all.get(mc.KEY_CONTROL)
            if p_control:
                p_toggle = p_control.get(mc.KEY_TOGGLE)
                if isinstance(p_toggle, dict):
                    self.entities[p_toggle.get(mc.KEY_CHANNEL, 0)]._set_onoff(p_toggle.get(mc.KEY_ONOFF))

        return update


    @callback
    def updatecoordinator_listener(self) -> bool:

        if super().updatecoordinator_listener():

            if ((self._sensor_power is not None) and self._sensor_power.enabled) or \
                ((self._sensor_voltage is not None) and self._sensor_voltage.enabled)  or \
                ((self._sensor_current is not None) and self._sensor_current.enabled) :
                self.request(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)
            if (self._sensor_energy is not None) and self._sensor_energy.enabled:
                if ((time() - self._lastupdate_consumption) > PARAM_ENERGY_UPDATE_PERIOD):
                    self.request(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)

            return True

        return False

