"""Test various Device apis both from merossclient.device.Device and from meross_lan.helpers.device.Device."""

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.meross_lan.merossclient import async_available_timezones
from custom_components.meross_lan.merossclient.protocol import const as mc

from tests import const as tc, helpers


async def test_async_configure_timezone(request, hass: "HomeAssistant"):
    """
    Verify timezone configuration works (useful to cover #687)
    """

    tznames = await async_available_timezones()
    emulator = helpers.build_emulator(mc.TYPE_MSS310, timezone="")

    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as device_context:

        device = device_context.device

        for tzname in tznames:
            assert device.descriptor.timezone != tzname
            await device.async_configure_timezone(tzname)
            assert device.descriptor.timezone == tzname
