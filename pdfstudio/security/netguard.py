"""Process-wide network kill switch.

Folio PDF must never touch the network. Besides not importing any network
module (QtNetwork, urllib, http ...), :func:`install` replaces the socket
primitives of the Python runtime so that *any* attempt to open a connection
or resolve a host name – from our code or from a third party library – fails
immediately with :class:`NetworkBlockedError`.

Local IPC that does not use ``socket`` (Qt's single-instance pipes, file I/O)
is unaffected.
"""

from __future__ import annotations

import socket

_installed = False


class NetworkBlockedError(OSError):
    """Raised whenever code tries to use the network."""

    def __init__(self, what: str = "network access") -> None:
        super().__init__(f"Folio PDF is offline-only: {what} is blocked")


def _blocked(name: str):
    def _raise(*_args, **_kwargs):
        raise NetworkBlockedError(name)

    _raise.__name__ = f"blocked_{name}"
    return _raise


class _BlockedSocket(socket.socket):
    """socket.socket subclass that refuses to be created for network families."""

    def __init__(self, family=-1, type=-1, proto=-1, fileno=None):  # noqa: A002
        fam = socket.AF_INET if family == -1 else family
        if fileno is None and fam in _NET_FAMILIES:
            raise NetworkBlockedError("socket creation")
        super().__init__(family, type, proto, fileno)


_NET_FAMILIES = {socket.AF_INET, socket.AF_INET6}


def install() -> None:
    """Block all network access for the current process (idempotent)."""
    global _installed
    if _installed:
        return
    socket.socket = _BlockedSocket  # type: ignore[misc]
    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "create_server",
        "socketpair",
    ):
        if hasattr(socket, name):
            setattr(socket, name, _blocked(name))
    _installed = True


def is_installed() -> bool:
    return _installed
