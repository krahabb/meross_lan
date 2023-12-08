""""""
import cProfile
import io
import pstats

# import datetime as dt
import time

from custom_components.meross_lan.merossclient import const as mc

from tests import helpers


async def profile_async_request_updates(hass, aioclient_mock, capsys):
    async with helpers.DeviceContext(hass, mc.TYPE_MSS310, aioclient_mock) as context:
        await context.perform_coldstart()

        device = context.device
        assert device

        # disable delay in emulator<->aioclient_mock response
        context.emulator_context.frozen_time = None

        # polling_tick = dt.timedelta(seconds=device.polling_period)
        epoch = time.time()

        pr = cProfile.Profile()
        pr.enable()

        for i in range(1000):
            epoch += device.polling_period
            await device.async_request_updates(epoch, None)

        pr.disable()
        with capsys.disabled():
            iostream = io.StringIO()
            sortby = pstats.SortKey.TIME
            ps = pstats.Stats(pr, stream=iostream).sort_stats(sortby)
            print("cProfile sorted stats:")
            ps.print_stats()
            print(iostream.getvalue())
            # print("cProfile stats:")
            # pr.print_stats(2)

        """
        with cProfile.Profile() as pr:

            for i in range(1000):
                epoch += device.polling_period
                await device.async_request_updates(epoch, None)

            with capsys.disabled():
                pr.print_stats()
        """
