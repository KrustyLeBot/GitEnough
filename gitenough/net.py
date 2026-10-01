"""TCP connections that do not stall on a broken IPv6 route.

Python tries a host's addresses one after the other, IPv6 first. When IPv6 is announced but does not
route, Windows waits 21 s before each fallback, so every GitLab call took 21 s more. git (through
curl) races both families and is not affected.
"""

import socket

FALLBACK_CONNECT_S = 3.0  # a reachable server answers a SYN far quicker, even across the world

_original = socket.create_connection
_installed = False


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *,
                      all_errors=False):
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)  # IPv4 first, IPv6 as the fallback
    default = timeout if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT else socket.getdefaulttimeout()
    errors = []
    for i, (family, kind, proto, _name, addr) in enumerate(infos):
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            if i == len(infos) - 1:
                sock.settimeout(default)  # the last address keeps the caller's timeout
            else:
                sock.settimeout(min(default, FALLBACK_CONNECT_S) if default else FALLBACK_CONNECT_S)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            sock.settimeout(default)
            return sock
        except OSError as exc:
            errors.append(exc)
            if sock is not None:
                sock.close()
    if not errors:
        raise OSError(f"getaddrinfo returned no address for {host}")
    if all_errors:
        raise ExceptionGroup("create_connection failed", errors)
    raise errors[-1] if len(errors) == 1 else errors[0]


def install() -> None:
    """Used by http.client (urllib) for every connection made after this call."""
    global _installed
    if not _installed:
        socket.create_connection = create_connection
        _installed = True
