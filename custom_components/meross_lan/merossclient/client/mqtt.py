from abc import abstractmethod
import asyncio
from collections import deque
from contextlib import AbstractAsyncContextManager
import random
import ssl
import string
import threading
from time import monotonic
from typing import TYPE_CHECKING, override
from uuid import uuid4

import paho.mqtt.client as mqtt

from . import AbstractClient
from .. import MEROSSDEBUG, HostAddress, get_macaddress_from_uuid
from ..protocol import const as mc, md5hexdigest

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired, Unpack

    from ..logging import LoggerType
    from ..protocol.message import MerossMessage, MerossRequest, MerossResponse


class MQTTRateLimitExceeded(Exception):

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


class AbstractMQTTConnection(AbstractClient):
    """
    Abstract base MQTT broker client providing common api for both App and Device MQTT clients.
    This class embeds most of the MQTT protocol logic and transaction management, while the
    actual MQTT connection and publish api must be implemented in the derived classes.
    """

    class Transaction(AbstractAsyncContextManager):
        """Context for pending MQTT publish(es) waiting for responses.
        This will allow to synchronize message request-response flow on MQTT"""

        if TYPE_CHECKING:
            response_future: asyncio.Future[MerossResponse]

        __slots__ = (
            "connection",
            "uuid",
            "request",
            "response_future",
        )

        def __init__(
            self,
            connection: "AbstractMQTTConnection",
            request: "MerossRequest",
            uuid: str,
            /,
        ):
            self.connection = connection
            self.uuid = uuid
            self.request = request
            self.response_future = connection.loop.create_future()
            connection._transactions[request.messageid] = self

        def cancel(self, remove: bool = True):
            mqtt_connection = self.connection
            request = self.request
            mqtt_connection.log(
                mqtt_connection.DEBUG,
                "Cancelling mqtt transaction on %s %s (messageId:%s uuid:%s)",
                request.method,
                request.namespace,
                request.messageid,
                uuid=self.uuid,
            )
            self.response_future.cancel()
            if remove:
                mqtt_connection._transactions.pop(request.messageid)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            if not self.response_future.done():
                self.cancel(True)

    class Client(AbstractClient):
        """Implements  a 'soft' client for a single device over an MQTTConnection."""

        if TYPE_CHECKING:
            id: Final[str]  # type: ignore[override]
            parent: Final["AbstractMQTTConnection"]  # type: ignore

            class Args(AbstractClient.Args):
                key: str  # override NotRequired

            class ConnectArgs(AbstractClient.ConnectArgs):
                pass

            class RequestRawArgs(AbstractClient.RequestRawArgs):
                pass

        TRANSPORT = AbstractClient.Transport.MQTT  # type: ignore[override]
        __slots__ = AbstractClient._calc_slots()

        def __init__(
            self,
            uuid: str,
            connection: "AbstractMQTTConnection",
            **kwargs: "Unpack[Args]",
        ):
            kwargs["key"] = kwargs.get("key", connection.key)
            kwargs["from_"] = connection.from_
            super().__init__(uuid, connection, **kwargs)
            # TODO: manage connection state change in parent
            if connection.is_connected:
                self.on_connect()

        @override
        async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"):
            if not self.parent.is_connected:
                await self.parent.async_connect(**kwargs)
            if not self.is_connected:
                self.on_connect()

        @override
        async def async_disconnect(self):
            if self.is_connected:
                self.on_disconnect()
            # we don't disconnect the parent connection since other clients might be using it

        @override
        async def async_request_raw(
            self, request: "MerossRequest", /, **kwargs: "Unpack[RequestRawArgs]"
        ):
            kwargs["uuid"] = self.id
            return await self.parent.async_request_raw(request, **kwargs)

    if TYPE_CHECKING:

        id: Final[HostAddress]  # type: ignore[override]

        class Args(AbstractClient.Args):
            pass

        class ConnectArgs(AbstractClient.ConnectArgs):
            pass

        class RequestRawArgs(AbstractClient.RequestRawArgs):
            uuid: str

        class RequestArgs(RequestRawArgs, AbstractClient.RequestArgs):
            pass

        rl_dropped: Final[int]
        """counter of messages dropped due to rate-limiting, used for diagnostics and testing."""
        _transactions: Final[dict[str, Transaction]]
        """messageid" -> Transaction, used to manage pending transactions and match responses to requests."""
        _random_disconnect_task: asyncio.Task

    TRANSPORT = AbstractClient.Transport.MQTT  # type: ignore[override]
    TIMEOUT = 5

    __SLOTS__ = (
        "rl_dropped",
        "_transactions",
        "_random_disconnect_unsub",
        "_random_disconnect_task",
    )

    def __init__(
        self,
        broker: HostAddress,
        parent: "LoggerType | None" = None,
        /,
        **kwargs: "Unpack[Args]",
    ):
        super().__init__(broker, parent, **kwargs)
        self.rl_dropped = 0
        self._transactions = {}

        if MEROSSDEBUG:

            def _random_disconnect():
                self._random_disconnect_unsub = self.loop.call_later(
                    60, _random_disconnect
                )
                if self.is_connected:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(self.DEBUG, "random disconnect")
                        self._random_disconnect_task = self.loop.create_task(
                            self.async_disconnect()
                        )
                else:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(self.DEBUG, "random connect")
                        self._random_disconnect_task = self.loop.create_task(
                            self.async_connect()
                        )

            self._random_disconnect_unsub = self.loop.call_later(60, _random_disconnect)

    async def async_shutdown(self):
        if MEROSSDEBUG:
            self._random_disconnect_unsub.cancel()
            try:
                self._random_disconnect_task.cancel()
                await self._random_disconnect_task
            except (asyncio.CancelledError, AttributeError):
                pass
        await super().async_shutdown()
        for mqtt_transaction in self._transactions.values():
            mqtt_transaction.cancel(False)
        self._transactions.clear()

    @AbstractClient.virtual
    def get_rl_safe_delay(self, uuid: str, /):
        return 0.0

    @abstractmethod
    async def async_publish_raw(
        self, message: "MerossMessage", /, **kwargs: "Unpack[RequestRawArgs]"
    ):
        """
        Publish a message to the broker. This is the lowest level interface used to actually
        send messages to the broker. This method doesn't perform application level transaction mgmt.
        To actually request a reply message use the async_request... path.
        """
        raise NotImplementedError()

    @override
    async def async_request_raw(
        self, request: "MerossRequest", /, **kwargs: "Unpack[RequestRawArgs]"
    ):
        """
        This is the main request interface to send a message to the broker and wait for the reply.
        This method performs application level transaction mgmt (matching request-reply, timeouts, etc).
        To actually just publish a message without waiting for the reply use the async_publish... path.
        """
        async with asyncio.timeout(kwargs.get("timeout", self.timeout)):
            async with MQTTConnection.Transaction(
                self, request, kwargs["uuid"]
            ) as transaction:
                await self.async_publish_raw(request, **kwargs)
                return await transaction.response_future

    @AbstractClient.virtual
    def on_message(self, mqtt_msg, /):
        """called when the underlying mqtt.Client receives a message."""
        # TODO: maybe reconcile with base on_rx
        pass

    @AbstractClient.virtual
    def on_publish(self):
        """Called when the underlying mqtt.Client publishes a message."""
        # TODO: maybe reconcile with base on_tx
        pass

    @AbstractClient.virtual
    def on_drop(self):
        """Called when the underlying mqtt.Client drops a message due to rate-limiting."""
        pass

    def _mqtt_transactions_clean(self):
        if self._transactions:
            # check and cleanup stale transactions
            epoch = self.time()
            for mqtt_transaction in [
                _t
                for _t in self._transactions.values()
                if (epoch - _t.request.header[mc.KEY_TIMESTAMP]) > 15
            ]:
                mqtt_transaction.cancel(True)


class MQTTConnection(AbstractMQTTConnection):
    """
    Implements an MQTT broker client through paho mqtt.
    TODO: manage spawned task cancellation.
    """

    if TYPE_CHECKING:

        class Args(AbstractMQTTConnection.Args):
            pass

        class ConnectArgs(AbstractMQTTConnection.ConnectArgs):
            pass

        class RequestRawArgs(AbstractMQTTConnection.RequestRawArgs):
            pass

        _mqttc: mqtt.Client
        _rl_queues: dict[str, _MQTTRateLimiter]

    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"
    STATE_DISCONNECTING = "disconnecting"
    STATE_DISCONNECTED = "disconnected"

    @staticmethod
    def generate_app_id():
        return md5hexdigest(uuid4().hex)

    __SLOTS__ = (
        "_mqttc",
        "_lock_state",
        "_rl_queues",
        "_stateext",
        "_connect_future",
    )

    def __init__(
        self,
        broker: HostAddress,
        parent: "LoggerType | None" = None,
        *,
        client_id: str,
        **kwargs: "Unpack[Args]",
    ):
        super().__init__(broker, parent, **kwargs)
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
        self._rl_queues: dict[str, _MQTTRateLimiter] = {}
        self._stateext = self.STATE_DISCONNECTED
        self._connect_future = None
        _mqttc.on_connect = self._mqttc_connect
        _mqttc.on_subscribe = self._mqttc_subscribe
        _mqttc.on_disconnect = self._mqttc_disconnect
        _mqttc.on_message = self._mqttc_message
        _mqttc.on_publish = self._mqttc_publish
        _mqttc.suppress_exceptions = True
        _mqttc._easy_log = self._easy_log

    def _easy_log(self, level, fmt: str, *args) -> None:
        # TODO: obfuscate in case (paho logs the topics...)
        self.log(self.VERBOSE, f"PAHO-LOG{{%s}} -> {fmt}", level, *args)

    # interface: self
    @property
    def stateext(self):
        return self._stateext

    @property
    def state_active(self):
        return self._stateext not in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)

    @property
    def state_inactive(self):
        return self._stateext in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)

    @override
    async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"):
        try:
            async with asyncio.timeout(kwargs.get("timeout", self.timeout)):
                future = self._connect_future
                if not future:
                    self._connect_future = future = self.loop.create_future()
                    await self.loop.run_in_executor(None, self.safe_start)
                await future
        except Exception as e:
            self.log_exception(self.WARNING, e, "async_connect")
            raise

    @override
    async def async_disconnect(self):
        if self._connect_future:
            self._connect_future.cancel()
            self._connect_future = None
        if self.state_active:
            await self.loop.run_in_executor(None, self.safe_stop)

    def safe_start(self, /):
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
            self._mqttc.connect_async(self.id.host, self.id.port)
            self._mqttc.loop_start()
            self._stateext = self.STATE_CONNECTING

    def safe_stop(self, /):
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

    @override
    def get_rl_safe_delay(self, uuid: str, /):
        """
        Returns the 'safe delay' after which we should not incur rate-limiting.
        This is useful to 'plan' mqtt send when these could/should be delayed
        and has a rather stochastic connotation.
        """
        try:
            t_queue = self._rl_queues[uuid].t_queue
        except KeyError:
            # useless maybe but if we're probing this uuid it'll
            # be likely used again
            self._rl_queues[uuid] = _MQTTRateLimiter()
            return 0.0

        t_now = monotonic()
        t_duration_back = t_now - _MQTTRateLimiter.DURATION
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
            return _MQTTRateLimiter.DURATION / (_MQTTRateLimiter.MAXQUEUE - t_queue_len)
        # queue empty
        return 0.0

    @override  # AbstractMQTTConnection
    async def async_publish_raw(
        self,
        message: "MerossMessage",
        /,
        **kwargs: "Unpack[RequestRawArgs]",
    ):
        uuid = kwargs["uuid"]
        self.on_tx(message, self)
        try:
            try:
                _rl = self._rl_queues[uuid]
            except KeyError:
                self._rl_queues[uuid] = _rl = _MQTTRateLimiter()
                _rl.t_queue.append(monotonic())
            else:
                t_now = monotonic()
                # implementing a rate-limiter trying to keep the send rate to lower than
                # 1 MQTT publish every 10 seconds (on average x device). This is accomplished
                # by keeping the count (and times) of sent messages in the last minute
                t_duration_back = t_now - _MQTTRateLimiter.DURATION
                t_queue = _rl.t_queue
                t_queue_len = len(t_queue)
                while t_queue_len:
                    if t_queue[0] <= t_duration_back:
                        t_queue.popleft()
                        t_queue_len -= 1
                        continue
                    if t_queue_len >= _MQTTRateLimiter.MAXQUEUE:
                        self.rl_dropped += 1  # type: ignore[assignment]
                        _rl.dropped += 1
                        self.on_drop()
                        raise MQTTRateLimitExceeded()
                    break
                t_queue.append(t_now)

            return self._mqttc.publish(
                mc.TOPIC_REQUEST.format(uuid),
                message.json,
            )

        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "async_publish_raw %s %s (messageId:%s uuid:%s)",
                message.method,
                message.namespace,
                message.messageid,
                uuid=uuid,
                timeout=14400,
            )
            raise

    def publish(self, topic: str, message: str, /):
        self._mqttc.publish(topic, message)

    @override
    def on_connect(self, /):
        super().on_connect()
        if self._connect_future:
            self._connect_future.set_result(True)
            self._connect_future = None

    def _mqttc_connect(self, *args):
        pass  # subscription implemented in derived classes

    def _mqttc_subscribe(self, *args):
        self._stateext = self.STATE_CONNECTED
        self.loop.call_soon_threadsafe(self.on_connect)

    def _mqttc_disconnect(self, *args):
        self._stateext = (
            self.STATE_DISCONNECTED if self.state_inactive else self.STATE_RECONNECTING
        )
        self.loop.call_soon_threadsafe(self.on_disconnect)

    def _mqttc_message(self, client, userdata, msg: mqtt.MQTTMessage):
        self.loop.call_soon_threadsafe(self.on_message, msg)

    def _mqttc_publish(self, *args):
        self.loop.call_soon_threadsafe(self.on_publish)


class MQTTAppClient(MQTTConnection):
    """
    Implements an "App behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as an App so that it can receive PUSHES
    from multiple clients (and send messages to them) as they're being grouped under
    the same account (userid) by the Meross brokers session management. This is
    different from the client impersonated by a device even though both (device client
    and app client) connect to the same broker and talk the same protocol.
    """

    if TYPE_CHECKING:

        class Args(MQTTConnection.Args):
            sslcontext: NotRequired[ssl.SSLContext]

    __SLOTS__ = (
        "app_id",
        "user_id",
    )

    def __init__(
        self,
        broker: HostAddress,
        parent: "LoggerType | None",
        /,
        user_id: str,
        app_id: str | None = None,
        **kwargs: "Unpack[Args]",
    ):
        if not app_id:
            app_id = MQTTConnection.generate_app_id()
        self.app_id = app_id
        self.user_id = user_id
        kwargs["from_"] = f"/app/{user_id}-{app_id}/subscribe"
        super().__init__(broker, parent, client_id=f"app:{app_id}", **kwargs)
        self._mqttc.username_pw_set(user_id, md5hexdigest(user_id, self.key))
        try:
            self._mqttc.tls_set_context(kwargs["sslcontext"])  # type: ignore
        except KeyError:
            self._mqttc.tls_set(
                cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT
            )

    @override
    def _mqttc_connect(self, *args):
        self._mqttc.subscribe(
            [
                (f"/app/{self.user_id}/subscribe", 1),  # topic for PUSHed messages
                (self.from_, 1),  # topic for responses to messages sent by this client
            ]
        )


class MQTTDeviceClient(MQTTConnection):
    """
    Implements a "Device behaviored" MQTT client. This client connect to the Meross cloud
    brokers and behaves (or tries to) exactly as a device so that it can receive
    messages sent to it by the apps and mediated by the broker.
    This is different from the client impersonated by an App even though both (device client
    and app client) connect to the same broker and talk the same protocol
    """

    if TYPE_CHECKING:

        class Args(MQTTConnection.Args):
            sslcontext: NotRequired[ssl.SSLContext]

    __slots__ = MQTTConnection._calc_slots(
        "topic_publish",
        "topic_subscribe",
    )

    def __init__(
        self,
        broker: HostAddress,
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
            broker,
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
