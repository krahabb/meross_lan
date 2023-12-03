from __future__ import annotations

import typing

from . import const as mlc
from .config_flow import OptionsFlow
from .helpers import ConfigEntriesHelper


if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class RepairFlow(OptionsFlow):
    """
    Handler for an issue fixing flow. This handler inherits from our own
    OptionsFlow so it 'just works' (should the issue be fixable by
    updating/checking the configuration entry)
    """
    pass


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
):
    _issue_id = issue_id.split(".")
    if _issue_id[0] == mlc.ISSUE_CLOUD_TOKEN_EXPIRED:
        helper = ConfigEntriesHelper(hass)
        config_entry = helper.get_config_entry(f"profile.{_issue_id[1]}")
        assert config_entry
        return RepairFlow(config_entry)

    if _issue_id[0] == mlc.ISSUE_DEVICE_ID_MISMATCH:
        helper = ConfigEntriesHelper(hass)
        config_entry = helper.get_config_entry(_issue_id[1])
        assert config_entry
        return RepairFlow(config_entry)
