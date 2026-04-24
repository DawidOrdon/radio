from __future__ import annotations

import argparse
import audioop
import hmac
import json
import logging
import queue
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import sounddevice as sd

from radio_wz.common.protocol import (
    DEFAULT_AUDIO_PORT,
    DEFAULT_CONTROL_PORT,
    DEFAULT_DISCOVERY_PORT,
    ClientHello,
    ControlMessage,
    make_broadcast_socket,
    unpack_audio_packet,
)


@dataclass(slots=True)
class ClientConfig:
    client_id: str
    client_name: str
    pairing_password: str = "radio123"
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    audio_port: int = DEFAULT_AUDIO_PORT
    control_port: int = DEFAULT_CONTROL_PORT
    sample_rate: int = 48_000
    channels: int = 1
    blocksize: int = 960
    offset_ms: int = 2000
    jitter_target_packets: int = 100
    output_device: int | None = None

    @classmethod
    def from_file(cls, path: Path) -> "ClientConfig":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**payload)

    @classmethod
    def default(cls) -> "ClientConfig":
        hostname = socket.gethostname().lower().replace(" ", "-")
        return cls(
            client_id=f"{hostname}-client",
            client_name=f"Klient {hostname}",
        )

    def write_to_file(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class ClientRuntimeState:
    def __init__(self, config: ClientConfig) -> None:
        self.offset_ms = config.offset_ms
        self.output_device = config.output_device
        self.last_audio_ts = 0.0
        self.last_audio_rms = 0
        self._lock = threading.Lock()

    def set_offset(self, value: int) -> None:
        with self._lock:
            self.offset_ms = max(0, min(8000, value))

    def get_offset(self) -> int:
        with self._lock:
            return self.offset_ms

    def set_output_device(self, device_index: int | None) -> None:
        with self._lock:
            self.output_device = device_index

    def get_output_device(self) -> int | None:
        with self._lock:
            return self.output_device

    def mark_audio_packet(self, rms_value: int) -> None:
        with self._lock:
            self.last_audio_ts = time.time()
            self.last_audio_rms = int(rms_value)

    def get_audio_status(self) -> tuple[float, int]:
        with self._lock:
            return self.last_audio_ts, self.last_audio_rms


class AudioReceiver:
    def __init__(self, config: ClientConfig, state: ClientRuntimeState) -> None:
        self.config = config
        self.state = state
        self.buffer: "queue.Queue[bytes]" = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._last_good_payload: bytes | None = None

    def start(self) -> None:
        threading.Thread(target=self._receive_loop, daemon=True, name="audio-recv").start()
        threading.Thread(target=self._playback_loop, daemon=True, name="audio-play").start()

    def stop(self) -> None:
        self._stop.set()

    def _receive_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        sock.bind(("0.0.0.0", self.config.audio_port))
        sock.settimeout(1.0)

        while not self._stop.is_set():
            try:
                packet, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                logging.error("Błąd gniazda UDP: %s", exc)
                time.sleep(0.5)
                continue

            try:
                _seq, _ts, payload = unpack_audio_packet(packet)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Pominięto uszkodzony pakiet audio: %s", exc)
                continue
            try:
                rms = audioop.rms(payload, 2)
            except audioop.error:
                rms = 0
            self.state.mark_audio_packet(rms)

            try:
                self.buffer.put_nowait(payload)
            except queue.Full:
                _ = self.buffer.get_nowait()
                self.buffer.put_nowait(payload)

    def _playback_loop(self) -> None:
        bytes_per_frame = self.config.channels * 2

        while not self._stop.is_set():
            try:
                offset_frames = int((self.state.get_offset() / 1000) * self.config.sample_rate)
                min_prefill_packets = max(
                    1,
                    offset_frames // self.config.blocksize,
                    self.config.jitter_target_packets,
                )
                while self.buffer.qsize() < min_prefill_packets and not self._stop.is_set():
                    time.sleep(0.05)

                def callback(outdata, frames, _time_info, status):
                    if status:
                        logging.warning("Audio callback status: %s", status)
                    try:
                        raw = self.buffer.get_nowait()
                        self._last_good_payload = raw
                    except queue.Empty:
                        last_ts, _rms = self.state.get_audio_status()
                        if self._last_good_payload is not None and (time.time() - last_ts) < 0.15:
                            raw = self._last_good_payload
                        else:
                            outdata[:] = b"\x00" * (frames * bytes_per_frame)
                            return
                    if raw is None:
                        outdata[:] = b"\x00" * (frames * bytes_per_frame)
                        return
                    if len(raw) != frames * bytes_per_frame:
                        if len(raw) < frames * bytes_per_frame:
                            raw = raw + (b"\x00" * ((frames * bytes_per_frame) - len(raw)))
                        else:
                            raw = raw[: frames * bytes_per_frame]
                    outdata[:] = raw

                output_device = self._resolve_output_device()
                with sd.RawOutputStream(
                    samplerate=self.config.sample_rate,
                    blocksize=self.config.blocksize,
                    channels=self.config.channels,
                    dtype="int16",
                    device=output_device,
                    callback=callback,
                ):
                    if output_device is None:
                        logging.info("Audio output: domyślne urządzenie systemowe")
                    else:
                        logging.info("Audio output: urządzenie #%s", output_device)
                    while not self._stop.is_set():
                        time.sleep(0.25)
            except sd.PortAudioError as exc:
                logging.error("Błąd urządzenia audio: %s", exc)
                time.sleep(2)
            except Exception as exc:  # noqa: BLE001
                logging.exception("Nieoczekiwany błąd pętli odtwarzania: %s", exc)
                time.sleep(1)

    def _resolve_output_device(self) -> int | None:
        preferred = self.state.get_output_device()
        if preferred is None:
            return None
        try:
            dev = sd.query_devices(preferred)
            if int(dev.get("max_output_channels", 0)) > 0:
                return preferred
        except Exception as exc:  # noqa: BLE001
            logging.warning("Skonfigurowane urządzenie #%s niedostępne: %s", preferred, exc)

        logging.warning("Przełączam klienta na domyślne wyjście audio systemu")
        self.state.set_output_device(None)
        return None


class ControlServer:
    def __init__(self, config: ClientConfig, state: ClientRuntimeState) -> None:
        self.config = config
        self.state = state

    def start(self) -> None:
        threading.Thread(target=self._serve_loop, daemon=True, name="control-server").start()

    def _serve_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.config.control_port))
        sock.listen(16)

        while True:
            try:
                conn, addr = sock.accept()
                conn.settimeout(8)
                threading.Thread(target=self._handle_connection, args=(conn, addr[0]), daemon=True).start()
            except OSError as exc:
                logging.error("Błąd accept na control server: %s", exc)
                time.sleep(0.2)

    def _handle_connection(self, conn: socket.socket, host: str) -> None:
        paired = False
        with conn:
            try:
                file = conn.makefile("rwb")
                for line in file:
                    try:
                        msg = ControlMessage.from_line(line)
                    except Exception:
                        self._safe_send(file, ControlMessage("error", {"message": "invalid_json"}))
                        continue

                    if msg.cmd == "pair":
                        supplied = str(msg.payload.get("password", ""))
                        ok = hmac.compare_digest(supplied, self.config.pairing_password)
                        paired = ok
                        self._safe_send(file, ControlMessage("pair_result", {"ok": ok}))
                        logging.info("Pair from %s -> %s", host, ok)
                        continue

                    if not paired:
                        self._safe_send(file, ControlMessage("error", {"message": "not_paired"}))
                        continue

                    response = self._dispatch(msg)
                    self._safe_send(file, response)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Błąd połączenia control (%s): %s", host, exc)

    def _safe_send(self, file: Any, message: ControlMessage) -> None:
        try:
            file.write(message.to_line())
            file.flush()
        except OSError:
            return

    def _dispatch(self, msg: ControlMessage) -> ControlMessage:
        if msg.cmd == "set_offset_ms":
            try:
                value = int(msg.payload["value"])
            except (KeyError, ValueError, TypeError):
                return ControlMessage("error", {"message": "invalid_offset"})
            self.state.set_offset(value)
            return ControlMessage("ok", {"offset_ms": self.state.get_offset()})

        if msg.cmd == "list_output_devices":
            devices = []
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_output_channels"] > 0:
                    devices.append({"index": idx, "name": dev["name"]})
            return ControlMessage("output_devices", {"items": devices})

        if msg.cmd == "set_output_device":
            value = msg.payload.get("value")
            try:
                device = int(value) if value is not None else None
            except (ValueError, TypeError):
                return ControlMessage("error", {"message": "invalid_output_device"})
            self.state.set_output_device(device)
            return ControlMessage("ok", {"output_device": self.state.get_output_device()})

        if msg.cmd == "status":
            return ControlMessage(
                "status",
                {
                    "client_id": self.config.client_id,
                    "offset_ms": self.state.get_offset(),
                    "output_device": self.state.get_output_device(),
                },
            )

        return ControlMessage("error", {"message": f"unknown_cmd:{msg.cmd}"})


class ClientAnnouncer:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._announce_loop, daemon=True, name="announce").start()

    def stop(self) -> None:
        self._stop.set()

    def _announce_loop(self) -> None:
        sock = make_broadcast_socket()
        while not self._stop.is_set():
            hello = ClientHello(
                client_id=self.config.client_id,
                client_name=self.config.client_name,
                audio_port=self.config.audio_port,
                control_port=self.config.control_port,
            ).to_bytes()
            try:
                sock.sendto(hello, ("255.255.255.255", self.config.discovery_port))
            except OSError as exc:
                logging.warning("Discovery broadcast failed: %s", exc)
            time.sleep(2)


def run_client(config_path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    cfg = ClientConfig.from_file(config_path)

    state = ClientRuntimeState(cfg)
    announcer = ClientAnnouncer(cfg)
    receiver = AudioReceiver(cfg, state)
    control_server = ControlServer(cfg, state)

    announcer.start()
    receiver.start()
    control_server.start()

    logging.info("Klient '%s' uruchomiony (audio:%s control:%s)", cfg.client_name, cfg.audio_port, cfg.control_port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Zatrzymywanie klienta...")
        announcer.stop()
        receiver.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Klient radiowęzła szkolnego")
    parser.add_argument("--config", type=Path, default=Path("client-config.json"))
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    run_client(args.config)
