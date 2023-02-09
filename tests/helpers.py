""""""
from contextlib import contextmanager
from typing import Generator

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import emulator

from .const import MOCK_DEVICE_UUID, MOCK_KEY

TRACES_PATH = "./emulator_traces/"
TRACES_MAP = {
    "mts200": "mts200b-1674112759-U0123456789012345678901234567890C-Kpippo.csv"
}


def generate_emulators() -> Generator['emulator.MerossEmulator', None, None]:
    # the actual emulator uuid and key might be different from
    # MOCK_DEVICE_UUID, MOCK_KEY if the trace name carries those
    return emulator.generate_emulators(TRACES_PATH, MOCK_DEVICE_UUID, MOCK_KEY)


def generate_emulator(model: str):
    # Watchout: this call will not use the uuid and key set
    # in the filename, just DEFAULT_UUID and DEFAULT_KEY
    return emulator.build_emulator(
        TRACES_PATH + TRACES_MAP[model], MOCK_DEVICE_UUID, MOCK_KEY
    )


class WebRequestProxy:
    """used to wrap http request data when forwarding to the MerossEmulator"""

    def __init__(self, data):
        self.data = data

    async def json(self):
        import json
        return json.loads(self.data)


@contextmanager
def emulator_mock(model: str, aioclient_mock: 'AiohttpClientMocker'):

    try:
        _emulator = generate_emulator(model)

        async def _handle_http_request(method, url, data):
            response = await _emulator.post_config(WebRequestProxy(data))  # type: ignore pylint: disable=no-member
            return AiohttpClientMockResponse(method, url, text=response.text)

        # we'll use the uuid so we can mock multiple at the same time
        # and the aioclient_mock will route accordingly
        aioclient_mock.post(
            f"http://{_emulator.descriptor.uuid}/config", # pylint: disable=no-member
            side_effect=_handle_http_request,
        )

        yield _emulator

    finally:
        # remove the mock from aioclient
        aioclient_mock.clear_requests()
