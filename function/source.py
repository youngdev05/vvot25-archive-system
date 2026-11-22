"""Download public HTTP(S) sources with bounded time and memory."""

import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit


MAX_SOURCE_BYTES = 2_400_000
IO_TIMEOUT = 15
MAX_REDIRECTS = 5
URL = re.compile(r"https?://[^/?#@\s\\]+([/?#][^\s\\]*)?")
TLS_CONTEXT = ssl.create_default_context()
IPV6_TRANSITION_NETWORKS = tuple(
    ipaddress.ip_network(prefix) for prefix in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)


def public_address(address):
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.ipv4_mapped is None and not any(ip in net for net in IPV6_TRANSITION_NETWORKS)
    return True


def connect(host, port, use_tls, deadline):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not addresses or any(not public_address(addr[4][0]) for addr in addresses):
        raise ValueError("Source must resolve to public addresses")

    last_error = None
    for family, kind, proto, _, address in addresses:
        timeout = min(IO_TIMEOUT, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Source download deadline exceeded")
        if use_tls:
            connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=TLS_CONTEXT)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        sock = socket.socket(family, kind, proto)
        try:
            # Connect to the validated address without resolving the host again.
            sock.settimeout(timeout)
            sock.connect(address)
            connection.sock = TLS_CONTEXT.wrap_socket(sock, server_hostname=host) if use_tls else sock
            return connection
        except OSError as error:
            sock.close()
            last_error = error
    raise OSError("Could not connect to source") from last_error


def read_file(response, deadline):
    length = response.getheader("Content-Length")
    if length is not None and not 0 <= int(length) <= MAX_SOURCE_BYTES:
        raise ValueError("Source file too large")

    data = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Source download deadline exceeded")
        chunk = response.read1(64 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Source file too large")
    if length is not None and len(data) != int(length):
        raise OSError("Incomplete source file")
    return bytes(data)


def download(url):
    deadline = time.monotonic() + 240
    for _ in range(MAX_REDIRECTS + 1):
        if not isinstance(url, str) or len(url) > 4096 or not URL.fullmatch(url):
            raise ValueError("Invalid source URL")
        parsed = urlsplit(url)
        host = parsed.hostname.encode("idna").decode("ascii")
        use_tls = parsed.scheme == "https"
        port = parsed.port if parsed.port is not None else (443 if use_tls else 80)
        target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            target += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~[]")

        connection = connect(host, port, use_tls, deadline)
        try:
            connection.request("GET", target, headers={"Accept-Encoding": "identity"})
            with connection.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Redirect has no Location header")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise OSError(f"Source replied HTTP {response.status}")
                return read_file(response, deadline)
        finally:
            connection.close()
    raise ValueError("Too many source redirects")
