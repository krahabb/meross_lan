import typing

from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.util import dt as dt_util

from . import const as mlc
from .helpers.component_api import ComponentApi

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .helpers.device import Device


class SimpleRepairFlow(ConfirmRepairFlow):
    """Handler for a simple issue fixing confirmation flow."""

    __slots__ = (
        "issue_unique_id",
        "issue_id",
        "manager_id",
    )

    def __init__(self, issue_unique_id: str, issue_id: str, manager_id: str) -> None:
        self.issue_unique_id = issue_unique_id
        self.issue_id = issue_id
        self.manager_id = manager_id
        super().__init__()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            config_entry = ComponentApi.get(self.hass).get_config_entry(self.manager_id)
            device: "Device | None" = getattr(config_entry, "runtime_data", None)
            if (
                device
                and (tzname := getattr(dt_util.DEFAULT_TIME_ZONE, "key", None))
                and await device.async_config_device_timezone(tzname)
            ):
                device.remove_issue(self.issue_id)
            else:
                return super().async_abort(reason="cannot_connect")

        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: "HomeAssistant",
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
):
    from .config_flow import OptionsFlow

    _issue_id, manager_id = issue_id.split(".")

    if _issue_id == mlc.ISSUE_DEVICE_TIMEZONE:
        return SimpleRepairFlow(issue_id, _issue_id, manager_id)

    if _issue_id == mlc.ISSUE_CLOUD_TOKEN_EXPIRED:
        config_entry = ComponentApi.get(hass).get_config_entry(f"profile.{manager_id}")
        assert config_entry
        return OptionsFlow(config_entry, repair_issue_id=_issue_id)

    if _issue_id == mlc.ISSUE_DEVICE_ID_MISMATCH:
        config_entry = ComponentApi.get(hass).get_config_entry(manager_id)
        assert config_entry
        return OptionsFlow(config_entry, repair_issue_id=_issue_id)
