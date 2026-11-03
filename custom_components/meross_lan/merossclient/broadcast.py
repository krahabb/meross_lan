from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine


class Broadcast[_T, *_argsT](set["Callable[[*_argsT], _T]"]):
    """A simple 'broadcast' class that allows listener callbacks to be added and removed.
    Use the broadcast methods to invoke all the callbacks.
    _T is the return type of the callbacks, and *_argsT are the argument types of the callbacks.
    Use Coroutine[Any, Any, _T] as the return type for async callbacks."""

    def broadcast(self, *args: *_argsT):
        for listener in self:
            listener(*args)

    async def async_broadcast(self, *args: *_argsT):
        for listener in self:
            await listener(*args)  # type: ignore

    async def async_broadcast_safe(self, *args: *_argsT):
        """"""
        for listener in self:
            await listener(*args)  # type: ignore
