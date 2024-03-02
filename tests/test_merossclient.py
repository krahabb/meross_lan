"""Test for meross_lan.request service calls"""

from custom_components.meross_lan.merossclient import (
    const as mc,
    is_hub_namespace,
    is_thermostat_namespace,
)


def test_merossclient_api():
    """
    Test utilities defined in merossclient package/module
    """
    for ns in dir(mc):
        if ns.startswith("NS_"):
            namespace = getattr(mc, ns)
            _is_hub_namespace = is_hub_namespace(namespace)
            if ns.startswith("NS_APPLIANCE_HUB_"):
                assert _is_hub_namespace
            else:
                assert not _is_hub_namespace

            _is_thermostat_namespace = is_thermostat_namespace(namespace)
            if ns.startswith("NS_APPLIANCE_CONTROL_THERMOSTAT_"):
                assert _is_thermostat_namespace
            else:
                assert not _is_thermostat_namespace
