"""The Meross IoT local LAN integration."""

from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .helpers import LOGGER, ConfigEntryType, meross_profile as mlp
from .helpers.component_api import ComponentApi

if TYPE_CHECKING:

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import ConfigEntryManager

else:
    # In order to avoid a static dependency we resolve these
    # at runtime only when mqtt is actually needed in code
    mqtt_async_publish = None


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry[ConfigEntryManager]"
):
    LOGGER.debug("async_setup_entry (entry_id:%s)", config_entry.entry_id)

    api = ComponentApi.get(hass)

    match ConfigEntryType.get_type_and_id(config_entry.unique_id):
        case (ConfigEntryType.DEVICE, device_id):
            try:
                assert api.devices[device_id] is None, "device already initialized"
            except KeyError:
                # this could happen when we add profile entries after boot
                api.devices[device_id] = None
            device = await api.async_build_device(device_id, config_entry)
            try:
                await device.async_init()
                await device.async_setup_entry(hass, config_entry)
                api.devices[device_id] = device
                # this code needs to run after registering api.devices[device_id]
                # because of race conditions with profile entry loading
                device.start()
                return True
            except Exception as error:
                await device.async_shutdown()
                raise ConfigEntryError from error

        case (ConfigEntryType.PROFILE, profile_id):
            try:
                assert api.profiles[profile_id] is None, "profile already initialized"
            except KeyError:
                # this could happen when we add entries after boot
                api.profiles[profile_id] = None
            profile = mlp.MerossProfile(api, profile_id, config_entry)
            try:
                await profile.async_init()
                await profile.async_setup_entry(hass, config_entry)
                api.profiles[profile_id] = profile
                # 'link' the devices already initialized
                for device in api.active_devices():
                    if (device.key == profile.key) and (
                        device.descriptor.userId == profile_id
                    ):
                        profile.link(device)
                return True
            except Exception as error:
                await profile.async_shutdown()
                raise ConfigEntryError from error

        case (ConfigEntryType.HUB, _):
            if not await api.mqtt_connection.async_mqtt_subscribe():
                raise ConfigEntryNotReady("MQTT unavailable")
            await api.async_setup_entry(hass, config_entry)
            return True

        case _:
            raise ConfigEntryError(
                f"Unknown configuration type (entry_id:{config_entry.entry_id} title:'{config_entry.title}')"
            )


async def async_unload_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry[ConfigEntryManager]"
) -> bool:
    LOGGER.debug("async_unload_entry (entry_id:%s)", config_entry.entry_id)
    return await config_entry.runtime_data.async_unload_entry(hass, config_entry)


async def async_remove_entry(hass: "HomeAssistant", config_entry: "ConfigEntry"):
    LOGGER.debug("async_remove_entry (entry_id:%s)", config_entry.entry_id)
    api = ComponentApi.get(hass)
    match ConfigEntryType.get_type_and_id(config_entry.unique_id):
        case (ConfigEntryType.DEVICE, device_id):
            api.devices.pop(device_id)

        case (ConfigEntryType.PROFILE, profile_id):
            api.profiles.pop(profile_id)
            await mlp.MerossProfileStore(hass, profile_id).async_remove_and_logout(config_entry.data)  # type: ignore
