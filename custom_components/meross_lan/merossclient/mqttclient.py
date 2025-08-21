import asyncio
from collections import deque
import logging
import random
import ssl
import string
import threading
from time import monotonic
from typing import TYPE_CHECKING, override
from uuid import uuid4

import paho.mqtt.client as mqtt

from . import HostAddress, _BaseClient, get_macaddress_from_uuid
from .protocol import const as mc, md5hexdigest

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from . import LoggerT


def generate_app_id():
    return md5hexdigest(uuid4().hex)


class MerossMQTTRateLimitException(Exception):

    pass


class _MQTTRateLimiter:
    """
    MQTT publishing rate-limiter x device (in order to prevent Meross account ban):
    The algorithm tries to limit the rate of publish to
    less than MAXQUEUE over a period of DURATION for every single device.
    If a new publish request is submitted when more than MAXQUEUE
    messages have been sent over DURATION, it gets discarded.
    This algorithm has been put in place in 5.1.0 upgrading the previous
    'hard' rate-limiting which set the rate-limiting x connection (so all of
    the devices shared the same timings). Also, the previous algorithm was
    attempting queueing the messages in order to lower the publish rate over
    quick burst but this seemed to lead to message rejection at the device
    (at least on a recent msl320) and my guess is the device is trying to prevent
    message spoofing by rejecting messages too old in time (a few seconds for that msl320)

    2025-03-31
    In this discussion (https://github.com/krahabb/meross_lan/discussions/545#discussioncomment-12678003)
    There's a statement from Meross regarding data rate:
    "Important Requirement:
    To ensure optimal performance and security,
    please limit your device's communication to no more than 200 messages every one hour."

    TODO
    Taking this into account, we should set the rate-limiting to 1 message every 18 seconds
    on average but I guess the short term burst should be allowed (up to 6 messages in 60 seconds)
    We should eventually setup also a long-term data rate-limiting (e.g. 200 messages in 1 hour)
    """

    if TYPE_CHECKING:
        DURATION: ClassVar
        MAXQUEUE: ClassVar

    DURATION = 60
    MAXQUEUE = 6

    __slots__ = (
        "dropped",
        "t_queue",
    )

    def __init__(self) -> None:
        self.dropped: int = 0
        self.t_queue: deque[float] = deque()


class _MerossMQTTClient(_BaseClient, mqtt.Client):
    """
    Implements a rather abstract MQTT client used by both the MerossMQTTAppClient
    and MerossMQTTDeviceClient.
    """

    if TYPE_CHECKING:

        class Args(_BaseClient.Args):
            pass

        class RequestArgs(_BaseClient.RequestArgs):
            device_id: str

        _logger: LoggerT | None  # override paho client attribute type-hint

    MQTT_ERR_SUCCESS = mqtt.MQTT_ERR_SUCCESS

    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"
    STATE_DISCONNECTING = "disconnecting"
    STATE_DISCONNECTED = "disconnected"

    # TODO: consider refactoring to remove  mqtt.Client from hierarchy and use a class member
    # since we're risking too much about overriding attributes..
    __SLOTS__ = (
        "_lock_state",
        "_lock_queue",
        "_rl_dropped",
        "_rl2_queues",
        "_stateext",
        "_subscribe_error",
        "_subscribe_topics",
        "_future_connected",
        "_tasks",
        # mqtt.Client slots
        "_manual_ack",
        "_transport",
        "_protocol",
        "_userdata",
        "_sock",
        "_sockpairR",
        "_sockpairW",
        "_keepalive",
        "_connect_timeout",
        "_client_mode",
        "_callback_api_version",
        "_clean_start",
        "_clean_session",
        "_client_id",
        "_username",
        "_password",
        "_in_packet",
        "_out_packet",
        "_last_msg_in",
        "_last_msg_out",
        "_reconnect_min_delay",
        "_reconnect_max_delay",
        "_reconnect_delay",
        "_reconnect_on_failure",
        "_ping_t",
        "_last_mid",
        "_state",
        "_out_messages",
        "_in_messages",
        "_max_inflight_messages",
        "_inflight_messages",
        "_max_queued_messages",
        "_connect_properties",
        "_will_properties",
        "_will",
        "_will_topic",
        "_will_payload",
        "_will_qos",
        "_will_retain",
        "_on_message_filtered",
        "_host",
        "_port",
        "_bind_address",
        "_bind_port",
        "_proxy",
        "_in_callback_mutex",
        "_callback_mutex",
        "_msgtime_mutex",
        "_out_message_mutex",
        "_in_message_mutex",
        "_reconnect_delay_mutex",
        "_mid_generate_mutex",
        "_thread",
        "_thread_terminate",
        "_ssl",
        "_ssl_context",
        "_tls_insecure",
        "_logger",
        "_registered_write",
        "_on_log",
        "_on_pre_connect",
        "_on_connect",
        "_on_connect_fail",
        "_on_subscribe",
        "_on_message",
        "_on_publish",
        "_on_unsubscribe",
        "_on_disconnect",
        "_on_socket_open",
        "_on_socket_close",
        "_on_socket_register_write",
        "_on_socket_unregister_write",
        "_websocket_path",
        "_websocket_extra_headers",
        "_mqttv5_first_connect",
        "suppress_exceptions",
    )

    def __init__(
        self,
        client_id: str,
        subscribe_topics: list[tuple[str, int]],
        **kwargs: "Unpack[Args]",
    ):
        """
        2025-02-28 paho-mqtt is now on v2... which has different callback signatures and
        many other compatibility issues. The __init__ as is will select by default callback
        v1 signatures since their implementation/execution is more performant.
        Our callback signatures, nevertheless, are compatible with both versions so we can switch
        to v2 if needed.
        """
        try:
            mqtt.Client.__init__(
                self,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            )
        except:  # fallback to legacy (pre v2)
            mqtt.Client.__init__(self, client_id=client_id, protocol=mqtt.MQTTv311)
        _BaseClient.__init__(self, **kwargs)
        self._lock_state = threading.Lock()
        """synchronize connect/disconnect (not contended by the mqtt thread)"""
        self._lock_queue = threading.Lock()
        """synchronize access to the transmit queue. Might be contended by the mqtt thread"""
        self._rl_dropped = 0
        self._rl2_queues: dict[str, _MQTTRateLimiter] = {}
        self._stateext = self.STATE_DISCONNECTED
        self._subscribe_error = None
        self._subscribe_topics = subscribe_topics
        if self.loop:
            # our async interface would fail or simply not work
            # without the loop but we don't want to disseminate
            # checks here and there. Not setting this object property
            # (_asyncio_loop) in this case will be enough for the interpreter
            # to raise the missing attr exception and tell us we're doing it wrong
            # Also type checking will benefit since the attr is expected to host
            # a non null value
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

    async def async_shutdown(self):
        await self.async_disconnect()
        for task in self._tasks:
            await task

    # interface: mqtt.Client
    @override
    def enable_logger(self, logger: "LoggerT | None" = None) -> None:
        """
        Our _BaseClient already provides a 'logger' attribute and it's going to override
        the paho client property.
        We're providing this override in order to 'maintain' the paho interface behavior (enable logging)
        """
        if logger is None:
            if self._logger is not None:
                return
            logger = logging.getLogger(__name__)
        self.logger = self._logger = logger

    @override
    def disable_logger(self):
        self.logger = self._logger = None

    @property
    def rl_dropped(self):
        return self._rl_dropped

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
        loop = self.loop
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
            await self.loop.run_in_executor(None, self.safe_stop)

    def schedule_connect(self, broker: HostAddress):
        # even if safe_connect should be as fast as possible and thread-safe
        # we still might incur some contention with thread stop/restart
        # so we delegate its call to an executor
        self.loop.run_in_executor(None, self.safe_start, broker)

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

    def get_rl_safe_delay(self, uuid: str):
        """
        Returns the 'safe delay' after which we should not incur rate-limiting.
        This is useful to 'plan' mqtt send when these could/should be delayed
        and has a rather stochastic connotation.
        """
        with self._lock_queue:
            try:
                _rl2 = self._rl2_queues[uuid]
            except KeyError:
                # useless maybe but if we're probing this uuid it'll
                # be likely used again
                self._rl2_queues[uuid] = _MQTTRateLimiter()
                return 0.0

            t_now = monotonic()
            t_duration_back = t_now - _MQTTRateLimiter.DURATION
            t_queue = _rl2.t_queue
            t_queue_len = len(t_queue)
            while t_queue_len:
                if t_queue[0] <= t_duration_back:
                    # discard in case
                    t_queue.popleft()
                    t_queue_len -= 1
                    continue
                if t_queue_len >= _MQTTRateLimiter.MAXQUEUE:
                    # queue full..any send before expiration
                    # of oldest send will be dropped
                    t_oldest_exp = t_queue[0] + _MQTTRateLimiter.DURATION
                    return t_oldest_exp - t_now  # assert > 0 ?
                # queue not full but we want to 'weigh-in' the queue length
                return _MQTTRateLimiter.DURATION / (
                    _MQTTRateLimiter.MAXQUEUE - t_queue_len
                )
            # queue empty
            return 0.0

    def rl_publish(self, uuid: str, request: str):
        with self._lock_queue:

            try:
                _rl2 = self._rl2_queues[uuid]
            except KeyError:
                self._rl2_queues[uuid] = _rl2 = _MQTTRateLimiter()

            t_now = monotonic()
            # implementing a rate-limiter trying to keep the send rate to lower than
            # 1 MQTT publish every 10 seconds (on average x device). This is accomplished
            # by keeping the count (and times) of sent messages in the last minute
            t_duration_back = t_now - _MQTTRateLimiter.DURATION
            t_queue = _rl2.t_queue
            t_queue_len = len(t_queue)
            while t_queue_len:
                if t_queue[0] <= t_duration_back:
                    t_queue.popleft()
                    t_queue_len -= 1
                    continue
                if t_queue_len >= _MQTTRateLimiter.MAXQUEUE:
                    self._rl_dropped += 1
                    _rl2.dropped += 1
                    raise MerossMQTTRateLimitException()
                break

            t_queue.append(t_now)
            return mqtt.Client.publish(
                self,
                mc.TOPIC_REQUEST.format(uuid),
                request,
            )

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
        task = self.loop.create_task(self.async_mqtt_message(msg))
        self._tasks.append(task)
        task.add_done_callback(self._tasks.remove)

    async def async_mqtt_message(self, msg: mqtt.MQTTMessage):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client receives a message
        """
        pass

    def _mqttc_connect(self, *args):
        self.subscribe(self._subscribe_topics)

    def _mqttc_subscribe(self, *args):
        """This is the standard version of the callback: called when we're not managed through a loop"""
        self._stateext = self.STATE_CONNECTED

    def _mqttc_subscribe_loop(self, *args):
        """This is the asynced version of the callback: called when we're managed through a loop"""
        self._stateext = self.STATE_CONNECTED
        self.loop.call_soon_threadsafe(self._mqtt_connected)

    def _mqttc_disconnect(self, *args):
        """This is the standard version of the callback: called when we're not managed through a loop"""
        self._stateext = (
            self.STATE_DISCONNECTED if self.state_inactive else self.STATE_RECONNECTING
        )

    def _mqttc_disconnect_loop(self, *args):
        """This is the asynced version of the callback: called when we're managed through a loop"""
        self._stateext = (
            self.STATE_DISCONNECTED if self.state_inactive else self.STATE_RECONNECTING
        )
        self.loop.call_soon_threadsafe(self._mqtt_disconnected)

    def _mqttc_publish_loop(self, *args):
        self.loop.call_soon_threadsafe(self._mqtt_published)

    def _mqttc_message_loop(self, client, userdata, msg: mqtt.MQTTMessage):
        self.loop.call_soon_threadsafe(self.mqtt_message, msg)


class MerossMQTTAppClient(_MerossMQTTClient):
    """
    Implements an "App behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as an App so that it can receive PUSHES
    from multiple clients (and send messages to them) as they're being grouped under
    the same account (userid) by the Meross brokers session management. This is
    different from the client impersonated by a device even though both (device client
    and app client) connect to the same broker and talk the same protocol.
    """

    if TYPE_CHECKING:

        class Args(_MerossMQTTClient.Args):
            sslcontext: NotRequired[ssl.SSLContext]

        class RequestArgs(_MerossMQTTClient.RequestArgs):
            pass

    __SLOTS__ = (
        "app_id",
        "topic_command",
        "topic_push",
    )

    def __init__(
        self, *, user_id: str, app_id: str | None = None, **kwargs: "Unpack[Args]"
    ):
        if not app_id:
            app_id = generate_app_id()
        self.app_id = app_id
        self.topic_command = f"/app/{user_id}-{app_id}/subscribe"
        self.topic_push = f"/app/{user_id}/subscribe"
        super().__init__(
            f"app:{app_id}",
            [(self.topic_push, 1), (self.topic_command, 1)],
            **kwargs,
        )
        self.username_pw_set(user_id, md5hexdigest(user_id, self.key))
        try:
            self.tls_set_context(kwargs["sslcontext"])  # type: ignore
        except KeyError:
            self.tls_set(
                cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT
            )


class MerossMQTTDeviceClient(_MerossMQTTClient):
    """
    Implements a "Device behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as a device so that it can receive
    messages sent to it by the apps and mediated by the broker.
    This is different from the client impersonated by an App even though both (device client
    and app client) connect to the same broker and talk the same protocol
    """

    if TYPE_CHECKING:

        class Args(_MerossMQTTClient.Args):
            sslcontext: NotRequired[ssl.SSLContext]

        class RequestArgs(_MerossMQTTClient.RequestArgs):
            pass

    __slots__ = _MerossMQTTClient._calc_slots(
        "topic_publish",
        "topic_subscribe",
    )

    def __init__(self, *, user_id: str | int, uuid: str, **kwargs: "Unpack[Args]"):
        """
        userid: represents the user account id
        uuid: 16 bytes hex string (lowercase)
        """
        self.topic_publish = f"/appliance/{uuid}/publish"
        self.topic_subscribe = f"/appliance/{uuid}/subscribe"
        characters = string.ascii_letters + string.digits
        super().__init__(
            f"fmware:{uuid}_{''.join(random.choices(characters, k=16))}",
            [(self.topic_subscribe, 1)],
            **kwargs,
        )
        macaddress = get_macaddress_from_uuid(uuid)
        pwd = md5hexdigest(macaddress, self.key)
        self.username_pw_set(macaddress, f"{user_id}_{pwd}")
        try:
            self.tls_set_context(kwargs["sslcontext"])  # type: ignore
        except KeyError:
            self.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2)
