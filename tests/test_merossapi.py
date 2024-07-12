"""Test the core MerossApi class"""

from time import time

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.meross_lan import MerossApi, const as mlc
from custom_components.meross_lan.merossclient import (
    build_message,
    const as mc,
    json_dumps,
)

from . import const as tc, helpers


async def test_hamqtt_device_session(hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker):
    """
    check the local broker session management handles the device transactions
    when they connect to the HA broker
    """
    device_id = tc.MOCK_DEVICE_UUID
    key = ""
    topic_publish = mc.TOPIC_RESPONSE.format(device_id)
    topic_subscribe = mc.TOPIC_REQUEST.format(device_id)
    #
    # check the mc.NS_APPLIANCE_CONTROL_BIND is replied
    #
    message_bind_set = build_message(
        mc.NS_APPLIANCE_CONTROL_BIND,
        mc.METHOD_SET,
        {mc.KEY_BIND: {}},  # actual payload actually doesn't care
        key,
        topic_subscribe,
    )
    # since nothing is (yet) built at the moment, we expect this message
    # will go through all of the initialization process of MerossApi
    # and then manage the message
    async_fire_mqtt_message(hass, topic_publish, json_dumps(message_bind_set))
    await hass.async_block_till_done()

    hamqtt_mock.async_publish_mock.assert_any_call(
        hass,
        topic_subscribe,
        helpers.MessageMatcher(
            header=helpers.DictMatcher(
                {
                    mc.KEY_NAMESPACE: mc.NS_APPLIANCE_CONTROL_BIND,
                    mc.KEY_METHOD: mc.METHOD_SETACK,
                    mc.KEY_MESSAGEID: message_bind_set[mc.KEY_HEADER][mc.KEY_MESSAGEID],
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
        mc.NS_APPLIANCE_SYSTEM_CLOCK,
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
        mc.NS_APPLIANCE_CONTROL_CONSUMPTIONCONFIG,
        mc.METHOD_PUSH,
        {
            "config": {
                "voltageRatio": 188,
                "electricityRatio": 102,
                "maxElectricityCurrent": 11000,
                "powerRatio": 0,
            }
        },
        key,
        topic_publish,
    )
    async_fire_mqtt_message(hass, topic_publish, json_dumps(message_consumption_push))
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
