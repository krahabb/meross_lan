import typing

from . import MerossApi, const as mlc
from .helpers import OBFUSCATE_DEVICE_ID_MAP, obfuscated_dict_copy
from .meross_profile import MerossCloudProfile

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, MappingProxyType


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

    if unique_id == mlc.DOMAIN:
        # MQTT Hub entry
        return _get_plain_config_data(entry.data)

    unique_id = unique_id.split(".")
    if unique_id[0] == "profile":
        # profile entry
        if profile := MerossApi.profiles.get(unique_id[1]):
            if profile.obfuscate:
                data = obfuscated_dict_copy(profile._data)
                # the profile contains uuid as keys and obfuscation
                # is not smart enough (but OBFUSCATE_DEVICE_ID_MAP is already
                # filled with uuid(s) from the profile device_info(s) and
                # the device_info(s) were already obfuscated in data)
                data[MerossCloudProfile.KEY_DEVICE_INFO] = {  # type: ignore
                    OBFUSCATE_DEVICE_ID_MAP[device_id]: device_info
                    for device_id, device_info in data[MerossCloudProfile.KEY_DEVICE_INFO].items()  # type: ignore
                }
                return data
            else:
                return profile._data
        else:
            return _get_plain_config_data(entry.data)

    if device := MerossApi.devices.get(unique_id[0]):
        if device.obfuscate:
            data = obfuscated_dict_copy(entry.data)
        else:
            data = dict(entry.data)
        data["device"] = {
            "class": type(device).__name__,
            "conf_protocol": device.conf_protocol,
            "pref_protocol": device.pref_protocol,
            "curr_protocol": device.curr_protocol,
            "MQTT": {
                "cloud_profile": isinstance(device._profile, MerossCloudProfile),
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
            "polling_strategies": {
                strategy.namespace: strategy.lastrequest
                for strategy in device.polling_strategies.values()
            },
            "device_response_size_min": device.device_response_size_min,
            "device_response_size_max": device.device_response_size_max,
        }
        data[mlc.CONF_TRACE] = await device.get_diagnostics_trace()
        return data
    else:
        return _get_plain_config_data(entry.data)


def _get_plain_config_data(data: "MappingProxyType"):
    return obfuscated_dict_copy(data) if data.get(mlc.CONF_OBFUSCATE, True) else data
