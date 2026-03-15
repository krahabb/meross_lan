from ..merossclient.protocol import const as mc, namespaces as mn
from ..select import SelectParser


class Spray(SelectParser):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in DiffuserSpray to override namespace mapping and
    message formatting.
    """

    init_ns = mn.Appliance_Control_Spray
    # TODO: remove to avoid confusion
    # with diffuser spray which has different ns.
    # TODO: We should build a mapping grammar between digest keys and ns so that the digest/namespace
    # initialization can be more flexible and less hardcoded.
    init_entity_key = mc.KEY_SPRAY
    init_key_value = mc.KEY_MODE
    init_options_map = {
        mc.SPRAY_MODE_OFF: "off",
        mc.SPRAY_MODE_CONTINUOUS: "on",
        mc.SPRAY_MODE_INTERMITTENT: "eco",
    }
    _attr_entity_category = None
