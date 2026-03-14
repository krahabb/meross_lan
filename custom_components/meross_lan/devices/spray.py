from ..merossclient.protocol import const as mc, namespaces as mn
from ..select import SelectParser


class Spray(SelectParser):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in DiffuserSpray to override namespace mapping and
    message formatting.
    """

    init_entity_key = mc.KEY_SPRAY
    init_ns = mn.Appliance_Control_Spray
    init_key_value = mc.KEY_MODE
    init_options_map = {
        mc.SPRAY_MODE_OFF: "off",
        mc.SPRAY_MODE_CONTINUOUS: "on",
        mc.SPRAY_MODE_INTERMITTENT: "eco",
    }
    _attr_entity_category = None
