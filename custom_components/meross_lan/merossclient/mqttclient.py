import asyncio
from collections import deque
import random
import ssl
import string
import threading
from time import monotonic
from typing import TYPE_CHECKING, override
from uuid import uuid4

import paho.mqtt.client as mqtt

from . import HostAddress, MerossClient, get_macaddress_from_uuid, logging
from .protocol import const as mc, md5hexdigest

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from .logging import LoggerType
    from .protocol.message import MerossMessage


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

    """

    if TYPE_CHECKING:
        DURATION: ClassVar
        MAXQUEUE: ClassVar

    DURATION = 91
    MAXQUEUE = 5

    __slots__ = (
        "dropped",
        "t_queue",
    )

    def __init__(self) -> None:
        self.dropped: int = 0
        self.t_queue: deque[float] = deque()


class _MQTTConnection(MerossClient):
    """
    Implements a rather abstract MQTT client used by both the MQTTAppClient
    and MQTTDeviceClient.
    """

    if TYPE_CHECKING:

        class Args(MerossClient.Args):
            pass

        class RequestArgs(MerossClient.RequestArgs):
            device_id: str

        _mqttc: mqtt.Client

    TRANSPORT = MerossClient.Transport.MQTT  # type: ignore[override]

    MQTT_ERR_SUCCESS = mqtt.MQTT_ERR_SUCCESS

    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"
    STATE_DISCONNECTING = "disconnecting"
    STATE_DISCONNECTED = "disconnected"

    @staticmethod
    def generate_app_id():
        return md5hexdigest(uuid4().hex)

    # TODO: consider refactoring to remove  mqtt.Client from hierarchy and use a class member
    # since we're risking too much about overriding attributes..
    __SLOTS__ = (
        "_mqttc",
        "_lock_state",
        "_lock_queue",
        "_rl_dropped",
        "_rl2_queues",
        "_stateext",
        "_future_connected",
    )

    def __init__(
        self,
        id,
        parent: "LoggerType | None",
        /,
        client_id: str,
        **kwargs: "Unpack[Args]",
    ):
        super().__init__(id, parent, **kwargs)
        try:
            _mqttc = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            )
        except:  # fallback to legacy (pre v2)
            _mqttc = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        self._mqttc = _mqttc
        self._lock_state = threading.Lock()
        """synchronize connect/disconnect (not contended by the mqtt thread)"""
        self._lock_queue = threading.Lock()
        """synchronize access to the transmit queue. Might be contended by the mqtt thread"""
        self._rl_dropped = 0
        self._rl2_queues: dict[str, _MQTTRateLimiter] = {}
        self._stateext = self.STATE_DISCONNECTED
        self._future_connected = None
        _mqttc.on_connect = self._mqttc_connect
        _mqttc.on_subscribe = self._mqttc_subscribe
        _mqttc.on_disconnect = self._mqttc_disconnect
        _mqttc.on_message = self._mqttc_message
        _mqttc.on_publish = self._mqttc_publish
        _mqttc.suppress_exceptions = True
        _mqttc._easy_log = self._easy_log

    async def async_shutdown(self):
        await super().async_shutdown()
        await self.async_disconnect()

    def _easy_log(self, level, fmt: str, *args) -> None:
        # TODO: obfuscate in case (paho logs the topics...)
        self.log(self.VERBOSE, f"PAHO-LOG{{%s}} -> {fmt}", level, *args)

    # interface: self
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
            self._mqttc.loop_stop()
            self._mqttc.connect_async(broker.host, broker.port)
            self._mqttc.loop_start()
            self._stateext = self.STATE_CONNECTING

    def safe_stop(self):
        """
        Safe to be called from any thread (except the mqtt one)
        This is non-blocking and the thread will just die
        by itself.
        """
        with self._lock_state:
            self._stateext = self.STATE_DISCONNECTING
            self._mqttc.disconnect()
            self._mqttc.loop_stop()
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

    def rl_publish(self, request: "MerossMessage"):
        with self._lock_queue:

            try:
                _rl2 = self._rl2_queues[request.uuid]
            except KeyError:
                self._rl2_queues[request.uuid] = _rl2 = _MQTTRateLimiter()

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
            return self._mqttc.publish(
                mc.TOPIC_REQUEST.format(request.uuid),
                request.json,
            )

    def publish(self, topic: str, message: str):
        self._mqttc.publish(topic, message)

    def on_connect(self):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client is connected and has subscribed to the topics.
        """
        if self._future_connected:
            self._future_connected.set_result(True)
            self._future_connected = None

    def _mqttc_connect(self, *args):
        pass  # subscription implemented in derived classes

    def _mqttc_subscribe(self, *args):
        """This is the asynced version of the callback: called when we're managed through a loop"""
        self._stateext = self.STATE_CONNECTED
        self.loop.call_soon_threadsafe(self.on_connect)

    def on_disconnect(self):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client is disconnected
        """
        pass

    def _mqttc_disconnect(self, *args):
        """This is the asynced version of the callback: called when we're managed through a loop"""
        self._stateext = (
            self.STATE_DISCONNECTED if self.state_inactive else self.STATE_RECONNECTING
        )
        self.loop.call_soon_threadsafe(self.on_disconnect)

    def on_message(self, msg: mqtt.MQTTMessage):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client receives a message.
        """
        pass

    def _mqttc_message(self, client, userdata, msg: mqtt.MQTTMessage):
        self.loop.call_soon_threadsafe(self.on_message, msg)

    def on_publish(self):
        """
        This is a placeholder method called by the asyncio implementation in the
        main thread when the mqtt client (actually) publishes a message
        """
        pass

    def _mqttc_publish(self, *args):
        self.loop.call_soon_threadsafe(self.on_publish)


class MQTTAppClient(_MQTTConnection):
    """
    Implements an "App behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as an App so that it can receive PUSHES
    from multiple clients (and send messages to them) as they're being grouped under
    the same account (userid) by the Meross brokers session management. This is
    different from the client impersonated by a device even though both (device client
    and app client) connect to the same broker and talk the same protocol.
    """

    if TYPE_CHECKING:

        class Args(_MQTTConnection.Args):
            sslcontext: NotRequired[ssl.SSLContext]

        class RequestArgs(_MQTTConnection.RequestArgs):
            pass

    __SLOTS__ = (
        "app_id",
        "topic_command",
        "topic_push",
    )

    def __init__(
        self,
        id,
        parent: "LoggerType | None",
        /,
        user_id: str,
        app_id: str | None = None,
        **kwargs: "Unpack[Args]",
    ):
        if not app_id:
            app_id = _MQTTConnection.generate_app_id()
        self.app_id = app_id
        self.topic_command = f"/app/{user_id}-{app_id}/subscribe"
        self.topic_push = f"/app/{user_id}/subscribe"
        super().__init__(id, parent, client_id=f"app:{app_id}", **kwargs)
        self._mqttc.username_pw_set(user_id, md5hexdigest(user_id, self.key))
        try:
            self._mqttc.tls_set_context(kwargs["sslcontext"])  # type: ignore
        except KeyError:
            self._mqttc.tls_set(
                cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT
            )

    @override
    def _mqttc_connect(self, *args):
        self._mqttc.subscribe([(self.topic_push, 1), (self.topic_command, 1)])


class MQTTDeviceClient(_MQTTConnection):
    """
    Implements a "Device behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as a device so that it can receive
    messages sent to it by the apps and mediated by the broker.
    This is different from the client impersonated by an App even though both (device client
    and app client) connect to the same broker and talk the same protocol
    """

    if TYPE_CHECKING:

        class Args(_MQTTConnection.Args):
            sslcontext: NotRequired[ssl.SSLContext]

        class RequestArgs(_MQTTConnection.RequestArgs):
            pass

    __slots__ = _MQTTConnection._calc_slots(
        "topic_publish",
        "topic_subscribe",
    )

    def __init__(
        self,
        id,
        parent: "LoggerType | None",
        /,
        user_id: str | int,
        uuid: str,
        **kwargs: "Unpack[Args]",
    ):
        """
        userid: represents the user account id
        uuid: 16 bytes hex string (lowercase)
        """
        self.topic_publish = f"/appliance/{uuid}/publish"
        self.topic_subscribe = f"/appliance/{uuid}/subscribe"
        characters = string.ascii_letters + string.digits
        super().__init__(
            id,
            parent,
            client_id=f"fmware:{uuid}_{''.join(random.choices(characters, k=16))}",
            **kwargs,
        )
        macaddress = get_macaddress_from_uuid(uuid)
        self._mqttc.username_pw_set(
            macaddress, f"{user_id}_{md5hexdigest(macaddress, self.key)}"
        )
        try:
            self._mqttc.tls_set_context(kwargs["sslcontext"])  # type: ignore
        except KeyError:
            self._mqttc.tls_set(
                cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2
            )

    @override
    def _mqttc_connect(self, *args):
        self._mqttc.subscribe([(self.topic_subscribe, 1)])
