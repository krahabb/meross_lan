from ..merossclient.protocol import const as mc
from ..select import SelectParser


class Spray(SelectParser):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in DiffuserSpray to override namespace mapping and
    message formatting.
    """

    _INIT_entity_key = mc.KEY_SPRAY
    _INIT_key_value = SelectParser.KeyValue(mc.KEY_MODE)
    _INIT_options_map = {
        mc.SPRAY_MODE_OFF: "off",
        mc.SPRAY_MODE_CONTINUOUS: "on",
        mc.SPRAY_MODE_INTERMITTENT: "eco",
    }
    _attr_entity_category = None
