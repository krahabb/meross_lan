"""Test for meross_lan.request service calls"""

from custom_components.meross_lan.merossclient import const as mc, namespaces as mn


def test_merossclient_api():
    """
    Test utilities defined in merossclient package/module
    """
    for mc_symbol in dir(mc):
        if mc_symbol.startswith("NS_"):
            namespace = mn.NAMESPACES[getattr(mc, mc_symbol)]
            _is_hub_namespace = namespace.is_hub
            if mc_symbol.startswith("NS_APPLIANCE_HUB_"):
                assert _is_hub_namespace
            else:
                assert not _is_hub_namespace

            _is_thermostat_namespace = namespace.is_thermostat
            if mc_symbol.startswith("NS_APPLIANCE_CONTROL_THERMOSTAT_"):
                assert _is_thermostat_namespace
            else:
                assert not _is_thermostat_namespace
