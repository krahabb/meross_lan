"""Test the .helpers module"""

from custom_components.meross_lan.helpers import obfuscate
from custom_components.meross_lan.merossclient.protocol import const as mc


def test_obfuscated_key():
    """
    Verify the obfuscation
    """
    key_samples = {
        mc.KEY_FROM: {
            # check the userid carried in topics (/app/{userid}-{appid}/subscribe")
            "/app/100000-eb40234d5ec8db162c08447c0dc7d772/subscribe": "/app/@0-eb40234d5ec8db162c08447c0dc7d772/subscribe",
            "/app/100000/subscribe": "/app/@0/subscribe",
            "/app/100001/subscribe": "/app/@1/subscribe",
            # check whatever 'might' look as an UUID (/appliance/{uuid}/publish")
            "/appliance/eb40234d5ec8db162c08447c0dc7d772/publish": "/appliance/###############################0/publish",
            "/appliance/eb40234d5ec8db162c08447c0dc7d773/subscribe": "/appliance/###############################1/subscribe",
            "eb40234d5ec8db162c08447c0dc7d772": "###############################0",
        }
    }
    for key, samples in key_samples.items():
        # clear the cached keys to 'stabilize' expected results
        obfuscate.OBFUSCATE_KEYS[key].clear()
        for src, result in samples.items():
            assert (
                obfuscate.obfuscated_dict({key: src})[key] == result
            ), f"{key}: {src}"


def test_via_device_registry_compatibility():
    """Verify we keep working with both current and future Home Assistant APIs."""
    from types import SimpleNamespace

    import custom_components.meross_lan.helpers.device as md

    class LegacyRegistry:
        def async_get_device(self, identifiers):
            return SimpleNamespace(id="parent-device-id")

        def async_get_or_create(self, **kwargs):
            return kwargs

    class NewRegistry:
        def async_get_device(self, identifiers):
            return SimpleNamespace(id="parent-device-id")

        def async_get_or_create(self, *, config_entry_id, via_device_id=None, **kwargs):
            return {"config_entry_id": config_entry_id, "via_device_id": via_device_id}

    legacy_kwargs = md._device_registry_get_or_create(
        LegacyRegistry(),
        config_entry_id="entry-1",
        via_device=("meross_lan", "parent-device"),
    )
    assert legacy_kwargs["via_device"] == ("meross_lan", "parent-device")

    new_kwargs = md._device_registry_get_or_create(
        NewRegistry(),
        config_entry_id="entry-1",
        via_device=("meross_lan", "parent-device"),
    )
    assert new_kwargs["via_device_id"] == "parent-device-id"
