from typing import TYPE_CHECKING

from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import const as mlc
from .helpers.component_api import ComponentApi

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .devices.hub import HubMixin
    from .helpers.device import Device


class SimpleRepairFlow(ConfirmRepairFlow):
    """Handler for a simple issue fixing confirmation flow."""

    __slots__ = (
        "issue_id",  # RepairFlow
        "data",  # RepairFlow
        "issue_key",
        "manager_id",
    )

    def __init__(
        self,
        issue_id: str,
        data: dict[str, str | int | float | None] | None,
    ) -> None:
        self.issue_id = issue_id
        self.data = data
        super().__init__()


class DeviceTimeZoneRepairFlow(SimpleRepairFlow):
    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            config_entry = ComponentApi.get(self.hass).get_config_entry(
                self.issue_id.split(".")[1]
            )
            device: "Device | None" = getattr(config_entry, "runtime_data", None)
            if (
                device
                and (tzname := getattr(dt_util.DEFAULT_TIME_ZONE, "key", None))
                and await device.async_config_device_timezone(tzname)
            ):
                device.remove_issue_id(self.issue_id)
                return self.async_create_entry(data={})
            else:
                return self.async_abort(reason="cannot_connect")

        return await super().async_step_confirm(user_input)


class HubSubdeviceRemovedFlow(SimpleRepairFlow):
    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input:
            if user_input["delete"]:
                issue_key, device_id, subdevice_id = self.issue_id.split(".")
                api = ComponentApi.get(self.hass)
                config_entry = api.get_config_entry(device_id)
                assert config_entry
                device: "HubMixin | None" = getattr(config_entry, "runtime_data", None)
                if device:
                    device.remove_issue_id(self.issue_id)
                    if subdevice_id in device.subdevices:
                        # subdevice still registered with hub..abort issue repair
                        return self.async_abort(reason="subdevice_still_registered")
                    logger = device
                else:
                    # Actually, a loaded device isnt needed to cleanup the registry...
                    logger = api

                _identifier = (mlc.DOMAIN, subdevice_id)
                for (
                    device_entry
                ) in api.device_registry.devices.get_devices_for_config_entry_id(
                    config_entry.entry_id
                ):
                    if _identifier in device_entry.identifiers:
                        logger.log(
                            logger.DEBUG,
                            "removing Hub subdevice %s from device registry",
                            device_entry.name_by_user or device_entry.name,
                        )
                        api.device_registry.async_remove_device(device_entry.id)
                        break

            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required("delete", default=True): selector.BooleanSelector()}
            ),
        )


async def async_create_fix_flow(
    hass: "HomeAssistant",
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
):
    from .config_flow import OptionsFlow

    if issue_id.startswith(mlc.ISSUE_DEVICE_TIMEZONE):
        return DeviceTimeZoneRepairFlow(issue_id, data)

    if issue_id.startswith(mlc.ISSUE_CLOUD_TOKEN_EXPIRED):
        config_entry = ComponentApi.get(hass).get_config_entry(
            f"profile.{issue_id.split(".")[1]}"
        )
        assert config_entry
        return OptionsFlow(config_entry, repair_issue_id=issue_id)

    if issue_id.startswith(mlc.ISSUE_DEVICE_ID_MISMATCH):
        config_entry = ComponentApi.get(hass).get_config_entry(issue_id.split(".")[1])
        assert config_entry
        return OptionsFlow(config_entry, repair_issue_id=issue_id)

    if issue_id.startswith(mlc.ISSUE_HUB_SUBDEVICE_REMOVED):
        return HubSubdeviceRemovedFlow(issue_id, data)
