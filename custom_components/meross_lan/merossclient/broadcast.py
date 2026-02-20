from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Callable


class Broadcast[*_argsT](set["Callable[[*_argsT], Any]"]):
    """A simple broadcast class that allows listeners to be added and removed,
    and broadcasts to be sent to all listeners. Use the set interface to add and remove
    listeners, and the broadcast method to send a broadcast to all listeners."""

    if TYPE_CHECKING:
        type ListenerT = Callable[[*_argsT], Any]

    def broadcast(self, *args: *_argsT):
        for listener in self:
            listener(*args)
