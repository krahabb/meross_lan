import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from .merossclient import MerossDeviceDescriptor, const as mc  # mEROSS cONST
from .meross_device import MerossDevice
from .cover import MerossLanGarage, MerossLanRollerShutter
from .helpers import LOGGER


class MerossDeviceGarage(MerossDevice):

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
            p_digest = descriptor.digest
            if p_digest:
                garagedoor = p_digest.get(mc.KEY_GARAGEDOOR)
                if isinstance(garagedoor, list):
                    for g in garagedoor:
                        MerossLanGarage(self, g.get(mc.KEY_CHANNEL))
                #endif p_digest

        except Exception as e:
            LOGGER.warning("MerossDeviceGarage(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_GARAGEDOOR_STATE:
            self._parse_garageDoor(payload.get(mc.KEY_STATE))
            return True

        return False


    def _parse_garageDoor(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL, 0)]._set_open(
                payload.get(mc.KEY_OPEN),
                payload.get(mc.KEY_EXECUTE)
            )
        elif isinstance(payload, list):
            for p in payload:
                self._parse_garageDoor(p)



class MerossDeviceShutter(MerossDevice):

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
            # atm we're not sure we can detect this in 'digest' payload
            # looks like mrs100 just exposes abilities and we'll have to poll
            # related NS
            if mc.NS_APPLIANCE_ROLLERSHUTTER_STATE in descriptor.ability:
                MerossLanRollerShutter(self, 0)
                self.polling_dictionary.append(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION)
                self.polling_dictionary.append(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)
                self.polling_dictionary.append(mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG)

        except Exception as e:
            LOGGER.warning("MerossDeviceShutter(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_STATE:
            self._parse_rollershutter_state(payload.get(mc.KEY_STATE))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION:
            if method == mc.METHOD_SETACK:
                """
                the SETACK PAYLOAD is empty so no info to extract but we'll use it
                as a trigger to request status update so to refresh movement state
                code moved to _ack_callback in MerossLanRollerShutter
                """
            else:
                self._parse_rollershutter_position(payload.get(mc.KEY_POSITION))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG:
            # payload = {"config": [{"channel": 0, "signalOpen": 50000, "signalClose": 50000}]}
            self._parse_rollershutter_config(payload.get(mc.KEY_CONFIG))
        return False


    def entry_option_setup(self, config_schema: dict):
        super().entry_option_setup(config_schema)
        shutter: MerossLanRollerShutter = self.entities[0]
        config_schema[
            vol.Optional(
                mc.KEY_SIGNALOPEN,
                description={"suggested_value": shutter._signalOpen / 1000}
                )
            ] = cv.positive_int
        config_schema[
            vol.Optional(
                mc.KEY_SIGNALCLOSE,
                description={"suggested_value": shutter._signalClose / 1000}
                )
            ] = cv.positive_int


    def entry_option_update(self, user_input: dict):
        super().entry_option_update(user_input)
        shutter: MerossLanRollerShutter = self.entities[0]
        signalopen = user_input.get(mc.KEY_SIGNALOPEN, shutter._signalOpen / 1000) * 1000
        signalclose = user_input.get(mc.KEY_SIGNALCLOSE, shutter._signalClose / 1000) * 1000
        if (signalopen != shutter._signalOpen) or (signalclose != shutter._signalClose):
            self.request(
                mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG,
                mc.METHOD_SET,
                {mc.KEY_CONFIG: [{
                    mc.KEY_CHANNEL: 0,
                    mc.KEY_SIGNALOPEN: signalopen,
                    mc.KEY_SIGNALCLOSE: signalclose
                    }]
                }
            )


    def _parse_rollershutter_state(self, p_state) -> None:
        if isinstance(p_state, dict):
            self.entities[p_state.get(mc.KEY_CHANNEL, 0)]._set_rollerstate(
                p_state.get(mc.KEY_STATE)
            )
        elif isinstance(p_state, list):
            for s in p_state:
                self._parse_rollershutter_state(s)


    def _parse_rollershutter_position(self, p_position) -> None:
        if isinstance(p_position, dict):
            self.entities[p_position.get(mc.KEY_CHANNEL, 0)]._set_rollerposition(
                p_position.get(mc.KEY_POSITION)
            )
        elif isinstance(p_position, list):
            for p in p_position:
                self._parse_rollershutter_position(p)


    def _parse_rollershutter_config(self, p_config) -> None:
        if isinstance(p_config, dict):
            self.entities[p_config.get(mc.KEY_CHANNEL, 0)]._set_rollerconfig(
                p_config.get(mc.KEY_SIGNALOPEN),
                p_config.get(mc.KEY_SIGNALCLOSE)
            )
        elif isinstance(p_config, list):
            for p in p_config:
                self._parse_rollershutter_config(p)