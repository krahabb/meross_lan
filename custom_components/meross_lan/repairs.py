from __future__ import annotations

import typing

from .config_flow import OptionsFlow
from .helpers import ConfigEntriesHelper
from .meross_profile import ISSUE_CLOUD_TOKEN_EXPIRED

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class CloudTokenExpiredRepairFlow(OptionsFlow):
    """Handler for an issue fixing flow."""

    pass


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
):
    _issue_id = issue_id.split(".")
    if _issue_id[0] == ISSUE_CLOUD_TOKEN_EXPIRED:
        helper = ConfigEntriesHelper(hass)
        config_entry = helper.get_config_entry(f"profile.{_issue_id[1]}")
        assert config_entry
        return CloudTokenExpiredRepairFlow(config_entry)
