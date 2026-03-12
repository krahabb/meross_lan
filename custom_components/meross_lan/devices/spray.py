from ..merossclient.protocol import const as mc, namespaces as mn
from ..select import SelectParser


class Spray(SelectParser):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in DiffuserSpray to override namespace mapping and
    message formatting.
    """

    ENTITY_KEY = mc.KEY_SPRAY
    ns = mn.Appliance_Control_Spray
    key_value = mc.KEY_MODE

    OPTIONS_MAP = {
        mc.SPRAY_MODE_OFF: "off",
        mc.SPRAY_MODE_CONTINUOUS: "on",
        mc.SPRAY_MODE_INTERMITTENT: "eco",
    }

    _attr_entity_category = None
