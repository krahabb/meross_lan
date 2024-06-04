import typing

from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.helpers import issue_registry
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.util import dt as dt_util

from . import const as mlc
from .helpers import ConfigEntriesHelper
from .helpers.manager import ApiProfile

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_ISSUE_IDS = set()


def create_issue(
    issue_id: str,
    manager_id: str,
    *,
    severity: IssueSeverity = IssueSeverity.CRITICAL,
    translation_placeholders: dict[str, str] | None = None,
):
    issue_unique_id = ".".join((issue_id, manager_id))
    if issue_unique_id not in _ISSUE_IDS:
        issue_registry.async_create_issue(
            ApiProfile.hass,
            mlc.DOMAIN,
            issue_unique_id,
            is_fixable=True,
            severity=severity,
            translation_key=issue_id,
            translation_placeholders=translation_placeholders,
        )
        _ISSUE_IDS.add(issue_unique_id)


def remove_issue(issue_id: str, manager_id: str):
    issue_unique_id = ".".join((issue_id, manager_id))
    if issue_unique_id in _ISSUE_IDS:
        _ISSUE_IDS.remove(issue_unique_id)
        issue_registry.async_delete_issue(ApiProfile.hass, mlc.DOMAIN, issue_unique_id)


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
            if (
                (device := ApiProfile.devices.get(self.manager_id))
                and (tzname := getattr(dt_util.DEFAULT_TIME_ZONE, "key", None))
                and await device.async_config_device_timezone(tzname)
            ):
                if self.issue_unique_id in _ISSUE_IDS:
                    _ISSUE_IDS.remove(self.issue_unique_id)
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
        config_entry = ConfigEntriesHelper(hass).get_config_entry(
            f"profile.{manager_id}"
        )
        assert config_entry
        return OptionsFlow(config_entry, repair_issue_id=_issue_id)

    if _issue_id == mlc.ISSUE_DEVICE_ID_MISMATCH:
        config_entry = ConfigEntriesHelper(hass).get_config_entry(manager_id)
        assert config_entry
        return OptionsFlow(config_entry, repair_issue_id=_issue_id)
