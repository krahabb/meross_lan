from __future__ import annotations

import asyncio
from collections import deque
from hashlib import md5
import logging
import random
import ssl
import string
import threading
from time import monotonic
from uuid import uuid4

import paho.mqtt.client as mqtt

from . import MEROSSDEBUG, HostAddress, get_macaddress_from_uuid


LOGGER = logging.getLogger(__name__)


def generate_app_id():
    return md5(uuid4().hex.encode("utf-8")).hexdigest()


class _MerossMQTTClient(mqtt.Client):
    """
    Implements a rather abstract MQTT client used by both the MerossMQTTAppClient
    and MerossMQTTDeviceClient
    """

    MQTT_ERR_SUCCESS = mqtt.MQTT_ERR_SUCCESS

    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"
    STATE_DISCONNECTING = "disconnecting"
    STATE_DISCONNECTED = "disconnected"

    # Meross cloud traffic need to be rate-limited in order to prevent banning.
    # Here the policy is pretty simple:
    # the trasmission rate is limited by RATELIMITER_MINDELAY which poses a minimum
    # interval between successive publish. When a message is published with
    # priority == True it is queued in front of any other 'non priority' mesage.
    # RATELIMITER_MAXQUEUE_PRIORITY sets a maximum number of priority messages
    # to be queued: when overflow occurs, older priority messages are discarded
    RATELIMITER_MINDELAY = 12
    RATELIMITER_MAXQUEUE = 5
    RATELIMITER_MAXQUEUE_PRIORITY = 0
    RATELIMITER_AVGPERIOD_DERATE = 0.1

    def __init__(
        self,
        client_id: str,
        subscribe_topics: list[tuple[str, int]],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        super().__init__(client_id, protocol=mqtt.MQTTv311)
        self._lock_state = threading.Lock()
        """synchronize connect/disconnect (not contended by the mqtt thread)"""
        self._lock_queue = threading.Lock()
        """synchronize access to the transmit queue. Might be contended by the mqtt thread"""
        self._rl_lastpublish = monotonic() - self.RATELIMITER_MINDELAY
        self._rl_qeque: deque[tuple[str, str, bool | None]] = deque()
        self._rl_queue_length = 0
        self._rl_dropped = 0
        self._rl_avgperiod = 0.0
        self._stateext = self.STATE_DISCONNECTED
        self._subscribe_error = None
        self._subscribe_topics = subscribe_topics
        if loop:
            # our async interface would fail or simply not work
            # without the loop but we don't want to disseminate
            # checks here and there. Not setting this object property
            # (_asyncio_loop) in this case will be enough for the interpreter
            # to raise the missing attr exception and tell us we're doing it wrong
            # Also type checking will benefit since the attr is expected to host
            # a non null value
            self._asyncio_loop = loop
            self._future_connected = None
            self._tasks: list[asyncio.Task] = []
            self.on_subscribe = self._mqttc_subscribe_loop
            self.on_disconnect = self._mqttc_disconnect_loop
            self.on_publish = self._mqttc_publish_loop
            self.on_message = self._mqttc_message_loop
        else:
            self.on_subscribe = self._mqttc_subscribe
            self.on_disconnect = self._mqttc_disconnect
        self.on_connect = self._mqttc_connect
        self.suppress_exceptions = True
        if MEROSSDEBUG and MEROSSDEBUG.mqtt_client_log_enable:
            self.enable_logger(LOGGER)

    async def async_shutdown(self):
        await self.async_disconnect()
        for task in self._tasks:
            await task

    @property
    def rl_dropped(self):
        return self._rl_dropped

    @property
    def rl_queue_length(self):
        return self._rl_queue_length

    @property
    def rl_queue_duration(self):
        return self._rl_queue_length * self.RATELIMITER_MINDELAY

    @property
    def stateext(self):
        return self._stateext

    @property
    def state_active(self):
        return self._stateext not in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)

    @property
    def state_inactive(self):
        return self._stateext in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)

    async def async_connect(self, broker: HostAddress):
        loop = self._asyncio_loop
        future = self._future_connected
        if not future:
            self._future_connected = future = loop.create_future()
        await loop.run_in_executor(None, self.safe_start, broker)
        return future

    async def async_disconnect(self):
        if self._future_connected:
            self._future_connected.cancel()
            self._future_connected = None
        if self.state_active:
            await self._asyncio_loop.run_in_executor(None, self.safe_stop)

    def schedule_connect(self, broker: HostAddress):
        # even if safe_connect should be as fast as possible and thread-safe
        # we still might incur some contention with thread stop/restart
        # so we delegate its call to an executor
        self._asyncio_loop.run_in_executor(None, self.safe_start, broker)

    def safe_start(self, broker: HostAddress):
        """
        Initiates an async connection and starts the managing thread.
        Safe to be called from any thread (except the mqtt one). Could be a bit
        'blocking' if the thread needs to be stopped (in case it was still running).
        The effective connection is asynchronous and will be managed by the thread.
        The future (optional) allows for synchronization and will be set after
        succesfully subscribing (see _mqttc_connect and overrides)
        """
        with self._lock_state:
            self.loop_stop()
            self.connect_async(broker.host, broker.port)
            self.loop_start()
            self._stateext = self.STATE_CONNECTING

    def safe_stop(self):
        """
        Safe to be called from any thread (except the mqtt one)
        This is non-blocking and the thread will just die
        by itself.
        """
        with self._lock_state:
            self._stateext = self.STATE_DISCONNECTING
            self.disconnect()
            self.loop_stop()
            self._stateext = self.STATE_DISCONNECTED

    def rl_publish(
        self, topic: str, payload: str, priority: bool | None = None
    ) -> mqtt.MQTTMessageInfo | bool:
        with self._lock_queue:
            queuelen = len(self._rl_qeque)
            if queuelen == 0:
                now = monotonic()
                period = now - self._rl_lastpublish
                if period > self.RATELIMITER_MINDELAY:
                    self._rl_lastpublish = now
                    self._rl_avgperiod += self.RATELIMITER_AVGPERIOD_DERATE * (
                        period - self._rl_avgperiod
                    )
                    return mqtt.Client.publish(self, topic, payload)

            if priority is None:
                if queuelen >= self.RATELIMITER_MAXQUEUE:
                    # TODO: log dropped message
                    self._rl_dropped += 1
                    return False
                _queue_pos = queuelen

            elif priority:
                # priority messages are typically SET commands and we want them to be sent
                # asap. As far as this goes we cannot really queue a lot of these
                # else we'd loose responsivity. Moreover, device level meross_lan code
                # would 'timeout' a SET request without a timely response so, actual policy is to not
                # queue too many of these (we'll eventually discard the older ones)
                _queue_pos = 0
                for topic_payload_priority in self._rl_qeque:
                    if not topic_payload_priority[2]:
                        break
                    if _queue_pos == self.RATELIMITER_MAXQUEUE_PRIORITY:
                        # discard older 'priority' msg
                        self._rl_qeque.popleft()
                        self._rl_dropped += 1
                        queuelen -= 1
                        break
                    _queue_pos += 1

            else:
                # priority == False are still prioritized but less than priority == True
                # so they'll be queued in front of priority == None
                # actual meross_lan uses this priority for PUSH messages (not a real reason to do so)
                # also, we're not typically sending PUSH messages over cloud MQTT....
                _queue_pos = 0
                for topic_payload_priority in self._rl_qeque:
                    if topic_payload_priority[2] is None:
                        break
                    if _queue_pos == self.RATELIMITER_MAXQUEUE_PRIORITY:
                        # discard older 'priority' msg
                        self._rl_qeque.popleft()
                        self._rl_dropped += 1
                        queuelen -= 1
                        break
                    _queue_pos += 1

            self._rl_qeque.insert(_queue_pos, (topic, payload, priority))
            self._rl_queue_length = queuelen + 1
            return True

    def loop_misc(self):
        ret = super().loop_misc()
        if (ret == mqtt.MQTT_ERR_SUCCESS) and self._rl_queue_length:
            if self._lock_queue.acquire(False):
                topic_payload_priority = None
                try:
                    queuelen = len(self._rl_qeque)
                    if queuelen > 0:
                        now = monotonic()
                        period = now - self._rl_lastpublish
                        if period > self.RATELIMITER_MINDELAY:
                            topic_payload_priority = self._rl_qeque.popleft()
                            self._rl_lastpublish = now
                            self._rl_avgperiod += self.RATELIMITER_AVGPERIOD_DERATE * (
                                period - self._rl_avgperiod
                            )
                            self._rl_queue_length = queuelen - 1
                    else:
                        self._rl_queue_length = 0
                finally:
                    self._lock_queue.release()
                    if topic_payload_priority:
                        super().publish(
                            topic_payload_priority[0], topic_payload_priority[1]
                        )
        return ret

    def _mqtt_connected(self):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client is connected (subscribed)
        """
        if self._future_connected:
            self._future_connected.set_result(True)
            self._future_connected = None

    def _mqtt_disconnected(self):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client is disconnected
        """
        pass

    def _mqtt_published(self):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client (actually) publishes a message
        """
        pass

    def mqtt_message(self, msg: mqtt.MQTTMessage):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client receives a message. Defaults to creating
        a task for processing the message in async_mqtt_message
        """
        task = self._asyncio_loop.create_task(self.async_mqtt_message(msg))
        self._tasks.append(task)
        task.add_done_callback(self._tasks.remove)

    async def async_mqtt_message(self, msg: mqtt.MQTTMessage):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client receives a message
        """
        pass

    def _mqttc_connect(self, client: mqtt.Client, userdata, rc, other):
        with self._lock_queue:
            self._rl_qeque.clear()
            self._rl_queue_length = 0
        client.subscribe(self._subscribe_topics)

    def _mqttc_subscribe(self, client, userdata, mid, granted_qos):
        """This is the standard version of the callback: called when we're not managed through a loop"""
        self._stateext = self.STATE_CONNECTED

    def _mqttc_subscribe_loop(self, client, userdata, mid, granted_qos):
        """This is the asynced version of the callback: called when we're managed through a loop"""
        self._stateext = self.STATE_CONNECTED
        self._asyncio_loop.call_soon_threadsafe(self._mqtt_connected)

    def _mqttc_disconnect(self, client: mqtt.Client, userdata, rc):
        """This is the standard version of the callback: called when we're not managed through a loop"""
        self._stateext = (
            self.STATE_DISCONNECTED if self.state_inactive else self.STATE_RECONNECTING
        )

    def _mqttc_disconnect_loop(self, client: mqtt.Client, userdata, rc):
        """This is the asynced version of the callback: called when we're managed through a loop"""
        self._stateext = (
            self.STATE_DISCONNECTED if self.state_inactive else self.STATE_RECONNECTING
        )
        self._asyncio_loop.call_soon_threadsafe(self._mqtt_disconnected)

    def _mqttc_publish_loop(self, client, userdata, mid):
        self._asyncio_loop.call_soon_threadsafe(self._mqtt_published)

    def _mqttc_message_loop(self, client, userdata, msg: mqtt.MQTTMessage):
        self._asyncio_loop.call_soon_threadsafe(self.mqtt_message, msg)


class MerossMQTTAppClient(_MerossMQTTClient):
    """
    Implements an "App behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as an App so that it can receive PUSHES
    from multiple clients (and send messages to them) as they're being grouped under
    the same account (userid) by the Meross brokers session management. This is
    different from the client impersonated by a device even though both (device client
    and app client) connect to the same broker and talk the same protocol
    """

    def __init__(
        self,
        key: str,
        userid: str,
        *,
        app_id: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        if not app_id:
            app_id = generate_app_id()
        self.app_id = app_id
        self.topic_command = f"/app/{userid}-{app_id}/subscribe"
        self.topic_push = f"/app/{userid}/subscribe"
        super().__init__(
            f"app:{app_id}", [(self.topic_push, 1), (self.topic_command, 1)], loop=loop
        )
        self.username_pw_set(userid, md5(f"{userid}{key}".encode("utf8")).hexdigest())
        self.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)


class MerossMQTTDeviceClient(_MerossMQTTClient):
    """
    Implements a "Device behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as a device so that it can receive
    messages sent to it by the apps and mediated by the broker.
    This is different from the client impersonated by an App even though both (device client
    and app client) connect to the same broker and talk the same protocol
    """

    def __init__(
        self,
        uuid: str,
        *,
        key: str = "",
        userid: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        """
        uuid: 16 bytes hex string (lowercase)
        key: see device key
        userid: represents the user account id (any integer number in str form)
        macaddress: xx:xx:xx:xx:xx:xx (lowercase)
        """
        self.topic_publish = f"/appliance/{uuid}/publish"
        self.topic_subscribe = f"/appliance/{uuid}/subscribe"
        characters = string.ascii_letters + string.digits
        super().__init__(
            f"fmware:{uuid}_{''.join(random.choices(characters, k=16))}",
            [(self.topic_subscribe, 1)],
            loop=loop,
        )
        macaddress = get_macaddress_from_uuid(uuid)
        pwd = md5(f"{macaddress}{key}".encode("utf8")).hexdigest()
        self.username_pw_set(macaddress, f"{userid}_{pwd}")
        self.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2)
