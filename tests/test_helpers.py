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
