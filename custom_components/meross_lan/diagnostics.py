import typing

from . import MerossApi
from .const import CONF_TRACE, CONF_TRACE_TIMEOUT, DOMAIN
from .helpers import OBFUSCATE_DEVICE_ID_MAP, obfuscated_dict_copy
from .meross_profile import MerossCloudProfile

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def async_get_device_diagnostics(
    hass, entry: "ConfigEntry", device
) -> typing.Mapping[str, typing.Any]:
    """Return diagnostics for a device entry."""
    return await async_get_config_entry_diagnostics(hass, entry)


async def async_get_config_entry_diagnostics(
    hass, entry: "ConfigEntry"
) -> typing.Mapping[str, typing.Any]:
    """Return diagnostics for a config entry."""
    unique_id = entry.unique_id
    assert unique_id, "unique_id must be set to a valid value"

    if unique_id == DOMAIN:
        # MQTT Hub entry
        return obfuscated_dict_copy(entry.data)

    unique_id = unique_id.split(".")
    if unique_id[0] == "profile":
        # profile entry
        if profile := MerossApi.profiles.get(unique_id[1]):
            data = obfuscated_dict_copy(profile._data)
            # the profile contains uuid as keys and obfuscation
            # is not smart enough
            data[MerossCloudProfile.KEY_DEVICE_INFO] = {
                OBFUSCATE_DEVICE_ID_MAP[key]: value
                for key, value in data[MerossCloudProfile.KEY_DEVICE_INFO].items()  # type: ignore
            }
            return data
        else:
            return obfuscated_dict_copy(entry.data)

    data = obfuscated_dict_copy(entry.data)
    if device := MerossApi.devices.get(unique_id[0]):
        data["device"] = {
            "class": type(device).__name__,
            "conf_protocol": device.conf_protocol,
            "pref_protocol": device.pref_protocol,
            "curr_protocol": device.curr_protocol,
            "MQTT": {
                "cloud_profile": bool(device._cloud_profile),
                "locally_active": bool(device.mqtt_locallyactive),
                "mqtt_connection": bool(device._mqtt_connection),
                "mqtt_connected": bool(device._mqtt_connected),
                "mqtt_publish": bool(device._mqtt_publish),
                "mqtt_active": bool(device._mqtt_active),
            },
            "HTTP": {
                "http": bool(device._http),
                "http_active": bool(device._http_active),
            },
            "polling_period": device.polling_period,
            "polling_dictionary": {
                namespace: strategy.lastrequest
                for namespace, strategy in device.polling_dictionary.items()
            },
        }
        data[CONF_TRACE] = await device.get_diagnostics_trace(
            data.get(CONF_TRACE_TIMEOUT)
        )
    return data
