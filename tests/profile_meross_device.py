""""""

import cProfile
import io
import pstats

from custom_components.meross_lan.merossclient.protocol import const as mc

from tests import helpers


async def profile_async_request_updates(request, hass, aioclient_mock, capsys):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSS310, aioclient_mock
    ) as context:
        device = await context.perform_coldstart()

        # disable delay in emulator<->aioclient_mock response
        context.emulator_context.frozen_time = None

        pr = cProfile.Profile()
        pr.enable()

        for _ in range(1000):
            await context.async_poll_single()

        pr.disable()
        with capsys.disabled():
            iostream = io.StringIO()
            sortby = pstats.SortKey.TIME
            ps = pstats.Stats(pr, stream=iostream).sort_stats(sortby)
            print("cProfile sorted stats:")
            ps.print_stats()
            print(iostream.getvalue())
