from __future__ import annotations

import json
import socket
import struct
import time
from dataclasses import dataclass, asdict
from typing import Any

AUDIO_HEADER = struct.Struct("!IQ")  # sequence, timestamp_ms
DEFAULT_DISCOVERY_PORT = 42500
DEFAULT_AUDIO_PORT = 42510
DEFAULT_CONTROL_PORT = 42520


@dataclass(slots=True)
class ClientHello:
    client_id: str
    client_name: str
    audio_port: int
    control_port: int
    status: str = "ready"

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ClientHello":
        data: dict[str, Any] = json.loads(payload.decode("utf-8"))
        return cls(**data)


@dataclass(slots=True)
class ControlMessage:
    cmd: str
    payload: dict[str, Any]

    def to_line(self) -> bytes:
        return (json.dumps({"cmd": self.cmd, "payload": self.payload}, ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_line(cls, line: bytes) -> "ControlMessage":
        body = json.loads(line.decode("utf-8"))
        return cls(cmd=body["cmd"], payload=body.get("payload", {}))


def pack_audio_packet(sequence: int, pcm_payload: bytes) -> bytes:
    ts_ms = int(time.time() * 1000)
    return AUDIO_HEADER.pack(sequence, ts_ms) + pcm_payload


def unpack_audio_packet(packet: bytes) -> tuple[int, int, bytes]:
    seq, ts_ms = AUDIO_HEADER.unpack(packet[: AUDIO_HEADER.size])
    return seq, ts_ms, packet[AUDIO_HEADER.size :]


def make_broadcast_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock
