from homeassistant.components import update as hauc

from custom_components.meross_lan.update import UpdateEntity
from custom_components.meross_lan.merossclient.cloudapi import LatestVersionType


from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = hauc.UpdateEntity

    DEVICE_ENTITIES = [UpdateEntity]

    HUB_SUBDEVICES_ENTITIES = {None: [UpdateEntity]}

    async def async_test_each_callback(self, entity: UpdateEntity):
        if not entity.device.is_connected:
            # some Hub subdevices are offline in our traces and we don't care
            return
        descriptor = entity.device.descriptor
        fw_version_old = descriptor.fw_version
        mcu_old = None
        try:
            descriptor.fw_version = "1.0.0"
            latest_version = LatestVersionType(
                {
                    "type": descriptor.type,
                    "subType": descriptor.subType,
                    "md5": "1708a3fa2163716da05f18c99160c6e7",
                    "url": "http://bucket-meross-static.meross.com/production/upload/2019/11/28/17/22/38/201911281722388922034.bin",
                    "version": "1.0.1",
                    "alias": "",
                    "mcu": [],
                    "upgradeType": "01",
                    "description": "Release Notes: Testing",
                },
            )
            if (entity.device == entity.parent) and (descriptor.mcu is not None):
                mcu_old = descriptor.mcu
                descriptor.mcu = {"type": "unknown", "version": "0.0.0", "productId": "xxxx"}  # type: ignore
                latest_version["mcu"].append(
                    {
                        "type": "unknown",
                        "md5": "653c111dc5892771a6796eb833acaeaf",
                        "url": "http://bucket-meross-static.meross.com/staticfile/firmware/2026/01/07/ca84cd3bbbbea2587ef54ed7da77d7ef.bin",
                        "version": "0.0.0",
                    }
                )

            entity.update_latest_version(latest_version)
            await entity.async_install()
            assert entity.in_progress

        finally:
            descriptor.fw_version = fw_version_old
            if mcu_old is not None:
                descriptor.mcu = mcu_old  # type: ignore
