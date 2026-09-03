from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
from dataclasses import dataclass, field
from threading import Lock
from typing import BinaryIO
from urllib.parse import urlparse


WEBSOCKET_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_BYTES = 1024 * 1024


def websocket_accept(key: str) -> str:
    digest = hashlib.sha1((key + WEBSOCKET_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, *, opcode: int = 0x1, masked: bool = False) -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("WebSocket frame is too large")
    first = 0x80 | (opcode & 0x0F)
    mask_bit = 0x80 if masked else 0
    length = len(payload)
    if length < 126:
        header = bytes((first, mask_bit | length))
    elif length <= 0xFFFF:
        header = bytes((first, mask_bit | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, mask_bit | 127)) + length.to_bytes(8, "big")
    if not masked:
        return header + payload
    mask = os.urandom(4)
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + encoded


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise ConnectionError("WebSocket connection closed")
        data.extend(chunk)
    return bytes(data)


def read_frame(stream: BinaryIO) -> tuple[int, bytes]:
    first, second = _read_exact(stream, 2)
    if not first & 0x80:
        raise ValueError("Fragmented WebSocket frames are not supported")
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exact(stream, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exact(stream, 8), "big")
    if length > MAX_FRAME_BYTES:
        raise ValueError("WebSocket frame is too large")
    mask = _read_exact(stream, 4) if second & 0x80 else b""
    payload = _read_exact(stream, length)
    if mask:
        payload = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
    return opcode, payload


@dataclass
class WebSocketConnection:
    socket: socket.socket
    lock: Lock = field(default_factory=Lock)

    def send(self, payload: bytes, *, opcode: int = 0x1) -> None:
        frame = encode_frame(payload, opcode=opcode)
        with self.lock:
            self.socket.sendall(frame)


def connect_websocket(cloud_url: str, token: str) -> tuple[socket.socket, BinaryIO]:
    parsed = urlparse(cloud_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("cloud_url must be an absolute http(s) URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = socket.create_connection((parsed.hostname, port), timeout=15)
    if parsed.scheme == "https":
        connection = ssl.create_default_context().wrap_socket(
            connection, server_hostname=parsed.hostname
        )
    connection.settimeout(30)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    default_port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname if port == default_port else f"{parsed.hostname}:{port}"
    request = (
        "GET /_agent/v1/events HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n"
        "\r\n"
    )
    connection.sendall(request.encode("ascii"))
    stream = connection.makefile("rb")
    status = stream.readline().decode("iso-8859-1").strip()
    headers: dict[str, str] = {}
    while True:
        line = stream.readline().decode("iso-8859-1")
        if line in {"\r\n", "\n", ""}:
            break
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    if " 101 " not in f" {status} ":
        stream.close()
        connection.close()
        raise ConnectionError(f"WebSocket upgrade failed: {status}")
    if headers.get("sec-websocket-accept") != websocket_accept(key):
        stream.close()
        connection.close()
        raise ConnectionError("WebSocket upgrade returned an invalid accept key")
    return connection, stream
