from typing import TYPE_CHECKING
from unittest import mock

import pytest

from custom_components.meross_lan.merossclient.protocol import const as mc
import emulator

from tests import const as tc, helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TRACE = "U01234567890123456789012345678912-Kpippo-msh300hk-mts150.csv"


@pytest.mark.parametrize("as_dict", [False, True])
async def test_hub_update_device_info_subdevices(
    request, hass: "HomeAssistant", as_dict: bool
):
    device_emulator = emulator.build_emulator(
        tc.EMULATOR_TRACES_PATH + TRACE, key=tc.MOCK_KEY, uuid=tc.MOCK_DEVICE_UUID
    )
    async with helpers.DeviceContext(
        request, hass, device_emulator, auto_poll=True
    ) as context:
        device = context.device
        assert device.subdevices
        subdevices_info = [
            {
                mc.KEY_SUBDEVICEID: subdevice_id,
                mc.KEY_SUBDEVICENAME: f"Cloud {subdevice_id}",
            }
            for subdevice_id in device.subdevices
        ]
        device_info = {
            mc.KEY_DEVNAME: "Cloud hub",
            "__subDeviceInfo": (
                {info[mc.KEY_SUBDEVICEID]: info for info in subdevices_info}
                if as_dict
                else subdevices_info
            ),
        }
        profile = mock.MagicMock()
        profile.get_latest_version.return_value = None

        with mock.patch.object(device, "log_exception") as log_exception:
            device.update_device_info(device_info, profile)  # type: ignore

        log_exception.assert_not_called()
        for info in subdevices_info:
            subdevice = device.subdevices[info[mc.KEY_SUBDEVICEID]]
            assert subdevice.update_firmware is not None
