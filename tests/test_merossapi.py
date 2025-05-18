"""Test the core ComponentApi class"""

from time import time
import typing

from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.meross_lan.merossclient import (
    build_message,
    const as mc,
    json_dumps,
    namespaces as mn,
)

from . import const as tc, helpers

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_hamqtt_device_session(
    request, hass: "HomeAssistant", hamqtt_mock: helpers.HAMQTTMocker, aioclient_mock
):
    """
    check the local broker session management handles the device transactions
    when they connect to the HA broker
    """

    # We need to provide a configured device so that our
    # api HAMQTTConnection doesn't spawn discoveries
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSS310, aioclient_mock
    ) as context:
        # let the device perform it's poll and come online
        await context.perform_coldstart()

        device_id = context.device_id
        key = context.device.key
        topic_publish = mc.TOPIC_RESPONSE.format(device_id)
        topic_subscribe = mc.TOPIC_REQUEST.format(device_id)
        #
        # check the mc.NS_APPLIANCE_CONTROL_BIND is replied
        #
        message_bind_set = build_message(
            mn.Appliance_Control_Bind.name,
            mc.METHOD_SET,
            {mn.Appliance_Control_Bind.key: {}},  # actual payload actually doesn't care
            key,
            topic_subscribe,
        )
        # since nothing is (yet) built at the moment, we expect this message
        # will go through all of the initialization process of ComponentApi
        # and then manage the message
        async_fire_mqtt_message(hass, topic_publish, json_dumps(message_bind_set))
        await hass.async_block_till_done()

        hamqtt_mock.async_publish_mock.assert_any_call(
            hass,
            topic_subscribe,
            helpers.MessageMatcher(
                header=helpers.DictMatcher(
                    {
                        mc.KEY_NAMESPACE: mn.Appliance_Control_Bind.name,
                        mc.KEY_METHOD: mc.METHOD_SETACK,
                        mc.KEY_MESSAGEID: message_bind_set[mc.KEY_HEADER][
                            mc.KEY_MESSAGEID
                        ],
                        mc.KEY_FROM: topic_publish,
                        mc.KEY_TRIGGERSRC: "CloudControl",
                    }
                )
            ),
        )
        #
        # check the NS_APPLIANCE_SYSTEM_CLOCK
        #
        message_clock_push = build_message(
            mn.Appliance_System_Clock.name,
            mc.METHOD_PUSH,
            {"clock": {"timestamp": int(time())}},
            key,
            topic_publish,
        )
        async_fire_mqtt_message(hass, topic_publish, json_dumps(message_clock_push))
        await hass.async_block_till_done()
        # check the PUSH was replied
        header_clock_reply = helpers.DictMatcher(message_clock_push[mc.KEY_HEADER])
        header_clock_reply[mc.KEY_TRIGGERSRC] = "CloudControl"
        header_clock_reply[mc.KEY_FROM] = topic_publish
        hamqtt_mock.async_publish_mock.assert_any_call(
            hass,
            topic_subscribe,
            helpers.MessageMatcher(header=header_clock_reply),
        )
        #
        # check the NS_APPLIANCE_CONTROL_CONSUMPTIONCONFIG
        #
        message_consumption_push = build_message(
            mn.Appliance_Control_ConsumptionConfig.name,
            mc.METHOD_PUSH,
            {
                mn.Appliance_Control_ConsumptionConfig.key: {
                    "voltageRatio": 188,
                    "electricityRatio": 102,
                    "maxElectricityCurrent": 11000,
                    "powerRatio": 0,
                }
            },
            key,
            topic_publish,
        )
        async_fire_mqtt_message(
            hass, topic_publish, json_dumps(message_consumption_push)
        )
        await hass.async_block_till_done()
        # check the PUSH was replied
        header_consumption_reply = helpers.DictMatcher(
            message_consumption_push[mc.KEY_HEADER]
        )
        header_consumption_reply[mc.KEY_TRIGGERSRC] = "CloudControl"
        header_consumption_reply[mc.KEY_FROM] = topic_publish
        hamqtt_mock.async_publish_mock.assert_any_call(
            hass,
            topic_subscribe,
            helpers.MessageMatcher(header=header_consumption_reply),
        )
