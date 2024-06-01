import typing

from ..merossclient import const as mc
from ..select import MLSelect

if typing.TYPE_CHECKING:
    from ..meross_device import DigestInitReturnType, MerossDevice


def digest_init_spray(device: "MerossDevice", digest) -> "DigestInitReturnType":
    """[{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]"""
    for channel_digest in digest:
        MLSpray(device, channel_digest[mc.KEY_CHANNEL])

    handler = device.get_handler(mc.NS_APPLIANCE_CONTROL_SPRAY)
    handler.register_entity_class(MLSpray)
    return handler.parse_list, (handler,)


class MLSpray(MLSelect):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in MLDiffuserSpray to override namespace mapping and
    message formatting.
    """

    OPTION_SPRAY_MODE_OFF = "off"
    OPTION_SPRAY_MODE_CONTINUOUS = "on"
    OPTION_SPRAY_MODE_ECO = "eco"

    SPRAY_MODE_MAP = {
        mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF,
        mc.SPRAY_MODE_INTERMITTENT: OPTION_SPRAY_MODE_ECO,
        mc.SPRAY_MODE_CONTINUOUS: OPTION_SPRAY_MODE_CONTINUOUS,
    }

    namespace = mc.NS_APPLIANCE_CONTROL_SPRAY
    key_namespace = mc.KEY_SPRAY
    key_value = mc.KEY_MODE

    manager: "MerossDevice"

    # HA core entity attributes:

    __slots__ = ("_spray_mode_map",)

    def __init__(self, manager: "MerossDevice", channel: object):
        # make a copy since different device firmwares
        # could bring in new modes/options
        self._spray_mode_map = dict(self.SPRAY_MODE_MAP)
        self.current_option = None
        self.options = list(self._spray_mode_map.values())
        super().__init__(manager, channel, mc.KEY_SPRAY)
        manager.register_parser(self.namespace, self)

    # interface: select.SelectEntity
    async def async_select_option(self, option: str):
        # reverse lookup the dict
        for mode, _option in self._spray_mode_map.items():
            if _option == option:
                break
        else:
            raise NotImplementedError("async_select_option")

        if await self.async_request_spray_ack(
            {self.key_channel: self.channel, self.key_value: mode}
        ):
            self.update_option(option)

    # interface: self
    async def async_request_spray_ack(self, payload: dict):
        return await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {self.key_namespace: payload},
        )

    def _parse_spray(self, payload: dict):
        """
        We'll map the mode key to a well-known option for this entity
        but, since there could be some additions from newer spray devices
        we'll also eventually add the unknown mode value as a supported mode
        Keep in mind we're updating a class instance dict so it should affect
        all of the same-class-entities
        """
        mode = payload[mc.KEY_MODE]
        option = self._spray_mode_map.get(mode)
        if option is None:
            # unknown mode value -> auto-learning
            option = "mode_" + str(mode)
            self._spray_mode_map[mode] = option
            self.options = list(self._spray_mode_map.values())
        self.update_option(option)
