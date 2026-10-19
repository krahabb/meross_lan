"""Test repair flows."""

from homeassistant.components import repairs
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient.protocol import const as mc

from tests import const as tc, helpers


async def test_DeviceTimeZoneRepairFlow(request, hass: "HomeAssistant"):

    issue_registry = ir.async_get(hass)

    # initialize repairs (seems little chaotic though..)
    assert await async_setup_component(hass, repairs.DOMAIN, {})
    repairs_manager = repairs.repairs_flow_manager(hass)
    assert repairs_manager

    tz = await dt_util.async_get_time_zone("Australia/Darwin")
    assert tz
    dt_util.set_default_time_zone(tz)

    emulator = helpers.build_emulator(mc.TYPE_MSS310, timezone="")

    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as device_context:

        device = device_context.device

        _timer = device._timers[device._check_device_time]
        await device_context.time_mock.async_move_to_timer(_timer)

        issue_id = f"{mlc.ISSUE_DEVICE_TIMEZONE}.{device.id}."
        issue = issue_registry.async_get_issue(mlc.DOMAIN, issue_id)
        assert issue

        repair_flow_result = await repairs_manager.async_init(
            mlc.DOMAIN,
            context={
                "issue_id": issue_id,
                "source": "test",
            },
        )

        repair_flow_result = await repairs_manager.async_configure(
            repair_flow_result["flow_id"],
            user_input={},
        )

        assert repair_flow_result.get("type") == FlowResultType.CREATE_ENTRY
