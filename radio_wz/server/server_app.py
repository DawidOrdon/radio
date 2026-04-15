from __future__ import annotations

import argparse
import json
import logging
import queue
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import sounddevice as sd
from apscheduler.schedulers.background import BackgroundScheduler
from pydub import AudioSegment

from radio_wz.common.protocol import (
    DEFAULT_AUDIO_PORT,
    DEFAULT_CONTROL_PORT,
    DEFAULT_DISCOVERY_PORT,
    ClientHello,
    ControlMessage,
    pack_audio_packet,
)

STALE_CLIENT_TTL_SECONDS = 15


@dataclass(slots=True)
class ServerConfig:
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    default_audio_port: int = DEFAULT_AUDIO_PORT
    default_control_port: int = DEFAULT_CONTROL_PORT
    sample_rate: int = 48_000
    channels: int = 1
    blocksize: int = 960
    global_offset_ms: int = 2000
    mic_input_device: int | None = None
    pairing_password: str = "radio123"

    @classmethod
    def from_file(cls, path: Path) -> "ServerConfig":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**payload)


@dataclass(slots=True)
class ClientState:
    hello: ClientHello
    address: str
    last_seen: float = field(default_factory=time.time)


class ClientRegistry:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.clients: dict[str, ClientState] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._discovery_loop, daemon=True, name="discovery").start()

    def _discovery_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", self.config.discovery_port))
        while True:
            payload, addr = sock.recvfrom(4096)
            try:
                hello = ClientHello.from_bytes(payload)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Zły pakiet discovery z %s: %s", addr[0], exc)
                continue
            with self._lock:
                self.clients[hello.client_id] = ClientState(hello=hello, address=addr[0])
                self._drop_stale_locked()

    def _drop_stale_locked(self) -> None:
        now = time.time()
        stale_ids = [cid for cid, state in self.clients.items() if now - state.last_seen > STALE_CLIENT_TTL_SECONDS]
        for cid in stale_ids:
            del self.clients[cid]

    def get_clients(self) -> list[ClientState]:
        with self._lock:
            self._drop_stale_locked()
            return sorted(self.clients.values(), key=lambda c: c.hello.client_id)


class ControlClient:
    def __init__(self, password: str):
        self.password = password

    def send(self, host: str, port: int, msg: ControlMessage, timeout: float = 3.0) -> ControlMessage:
        sock = socket.create_connection((host, port), timeout=timeout)
        with sock:
            file = sock.makefile("rwb")
            file.write(ControlMessage("pair", {"password": self.password}).to_line())
            file.flush()
            pair_line = file.readline()
            if not pair_line:
                raise RuntimeError("Empty pair response")
            pair_response = ControlMessage.from_line(pair_line)
            if pair_response.cmd != "pair_result" or not pair_response.payload.get("ok"):
                raise RuntimeError("Pairing failed")
            file.write(msg.to_line())
            file.flush()
            response_line = file.readline()
            if not response_line:
                raise RuntimeError("Empty command response")
            return ControlMessage.from_line(response_line)


class AudioBroadcaster:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.sequence = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.destinations: list[tuple[str, int]] = []
        self._dest_lock = threading.Lock()

        self.running = threading.Event()
        self.packet_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=5000)
        self._sender_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_destinations(self, destinations: list[tuple[str, int]]) -> None:
        with self._dest_lock:
            self.destinations = destinations

    def start_sender(self) -> None:
        with self._lock:
            if self.running.is_set():
                return
            self.running.set()
            self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True, name="audio-sender")
            self._sender_thread.start()

    def stop(self) -> None:
        self.running.clear()
        with self._lock:
            if self._sender_thread and self._sender_thread.is_alive():
                self._sender_thread.join(timeout=1)
            self._sender_thread = None
        while not self.packet_queue.empty():
            try:
                self.packet_queue.get_nowait()
            except queue.Empty:
                break

    def enqueue_pcm(self, payload: bytes) -> None:
        if not self.running.is_set():
            return
        packet = pack_audio_packet(self.sequence, payload)
        self.sequence += 1
        try:
            self.packet_queue.put_nowait(packet)
        except queue.Full:
            _ = self.packet_queue.get_nowait()
            self.packet_queue.put_nowait(packet)

    def _sender_loop(self) -> None:
        while self.running.is_set():
            try:
                packet = self.packet_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            with self._dest_lock:
                destinations = list(self.destinations)
            for host, port in destinations:
                try:
                    self.sock.sendto(packet, (host, port))
                except OSError as exc:
                    logging.warning("Nie udało się wysłać UDP do %s:%s (%s)", host, port, exc)


class ServerApp:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.registry = ClientRegistry(config)
        self.registry.start()
        self.control = ControlClient(config.pairing_password)
        self.broadcaster = AudioBroadcaster(config)

        self.music_tracks: list[Path] = []
        self.jingle_tracks: list[Path] = []
        self.queue: list[Path] = []
        self.queue_lock = threading.Lock()

        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.worker_lock = threading.Lock()

        self.scheduler = BackgroundScheduler()
        self.scheduler.start()

        self._refresh_loop_enabled = True

        self.root = tk.Tk()
        self.root.title("RadioWęzeł - Serwer")
        self._build_ui()
        self._refresh_clients_periodic()

    def _build_ui(self) -> None:
        self.root.geometry("1200x760")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=8)

        self.offset_var = tk.IntVar(value=self.config.global_offset_ms)
        ttk.Label(top, text="Globalny offset (ms):").pack(side="left")
        ttk.Scale(top, from_=0, to=5000, variable=self.offset_var, orient="horizontal", length=250).pack(side="left", padx=6)
        ttk.Button(top, text="Ustaw offset na klientach", command=self.push_offset).pack(side="left", padx=6)

        ttk.Button(top, text="Start mikrofon", command=self.start_microphone).pack(side="left", padx=6)
        ttk.Button(top, text="Start kolejki", command=self.start_queue).pack(side="left", padx=6)
        ttk.Button(top, text="Stop nadawania", command=self.stop_playback).pack(side="left", padx=6)

        center = ttk.Panedwindow(self.root, orient="horizontal")
        center.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(center)
        mid = ttk.Frame(center)
        right = ttk.Frame(center)
        center.add(left, weight=1)
        center.add(mid, weight=1)
        center.add(right, weight=1)

        ttk.Label(left, text="Klienci").pack(anchor="w")
        self.clients_list = tk.Listbox(left, selectmode=tk.MULTIPLE, height=18)
        self.clients_list.pack(fill="x")
        ttk.Button(left, text="Odśwież klientów", command=self._refresh_clients_once).pack(anchor="w", pady=4)
        ttk.Button(left, text="Ustaw wyjście audio na kliencie", command=self.set_output_on_selected).pack(anchor="w", pady=4)

        ttk.Label(mid, text="Muzyka (katalog)").pack(anchor="w")
        ttk.Button(mid, text="Wybierz katalog muzyki", command=self.load_music_dir).pack(anchor="w")
        self.music_list = tk.Listbox(mid, height=12)
        self.music_list.pack(fill="x")
        ttk.Button(mid, text="Dodaj do kolejki", command=self.add_music_to_queue).pack(anchor="w", pady=4)

        ttk.Label(mid, text="Dźingle (katalog)").pack(anchor="w", pady=(8, 0))
        ttk.Button(mid, text="Wybierz katalog dźingli", command=self.load_jingles_dir).pack(anchor="w")
        self.jingles_list = tk.Listbox(mid, height=8)
        self.jingles_list.pack(fill="x")
        ttk.Button(mid, text="Wstaw dźingiel przed", command=self.insert_jingle_before).pack(anchor="w", pady=4)
        ttk.Button(mid, text="Wstaw dźingiel po", command=self.insert_jingle_after).pack(anchor="w")

        ttk.Label(right, text="Kolejka").pack(anchor="w")
        self.queue_list = tk.Listbox(right, height=18)
        self.queue_list.pack(fill="x")
        ttk.Button(right, text="Usuń z kolejki", command=self.remove_from_queue).pack(anchor="w", pady=4)
        ttk.Button(right, text="Przesuń wyżej", command=self.move_up_queue).pack(anchor="w")
        ttk.Button(right, text="Przesuń niżej", command=self.move_down_queue).pack(anchor="w")

        sched = ttk.LabelFrame(self.root, text="Automatyka kolejki")
        sched.pack(fill="x", padx=8, pady=8)
        ttk.Label(sched, text="Start HH:MM").pack(side="left", padx=4)
        self.start_time_var = tk.StringVar(value="07:55")
        ttk.Entry(sched, textvariable=self.start_time_var, width=8).pack(side="left")
        ttk.Label(sched, text="Stop HH:MM").pack(side="left", padx=4)
        self.stop_time_var = tk.StringVar(value="15:30")
        ttk.Entry(sched, textvariable=self.stop_time_var, width=8).pack(side="left")
        ttk.Button(sched, text="Zapisz harmonogram dzienny", command=self.save_daily_schedule).pack(side="left", padx=8)

    def _on_close(self) -> None:
        self._refresh_loop_enabled = False
        self.stop_playback()
        self.scheduler.shutdown(wait=False)
        self.root.destroy()

    def _refresh_clients_once(self) -> None:
        existing = self.clients_list.curselection()
        self.clients_list.delete(0, tk.END)
        for c in self.registry.get_clients():
            age = int(time.time() - c.last_seen)
            self.clients_list.insert(tk.END, f"{c.hello.client_id} | {c.hello.client_name} | {c.address} | {age}s")
        for idx in existing:
            if idx < self.clients_list.size():
                self.clients_list.selection_set(idx)

    def _refresh_clients_periodic(self) -> None:
        self._refresh_clients_once()
        if self._refresh_loop_enabled:
            self.root.after(2000, self._refresh_clients_periodic)

    def _selected_clients(self) -> list[ClientState]:
        clients = self.registry.get_clients()
        selected = []
        for idx in self.clients_list.curselection():
            if idx < len(clients):
                selected.append(clients[idx])
        return selected

    def _current_destinations(self) -> list[tuple[str, int]]:
        return [(c.address, c.hello.audio_port) for c in self._selected_clients()]

    def push_offset(self) -> None:
        offset_value = int(self.offset_var.get())
        failures = []
        for client in self._selected_clients():
            try:
                self.control.send(client.address, client.hello.control_port, ControlMessage("set_offset_ms", {"value": offset_value}))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{client.hello.client_id}: {exc}")
        if failures:
            messagebox.showwarning("Offset", "Nie ustawiono offsetu na części klientów:\n" + "\n".join(failures))

    def set_output_on_selected(self) -> None:
        device = simpledialog.askinteger("Wyjście audio", "Podaj index output device (lub Cancel=domyślny)")
        failures = []
        for client in self._selected_clients():
            try:
                self.control.send(
                    client.address,
                    client.hello.control_port,
                    ControlMessage("set_output_device", {"value": device}),
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{client.hello.client_id}: {exc}")
        if failures:
            messagebox.showwarning("Output", "Błąd ustawiania wyjścia:\n" + "\n".join(failures))

    def _start_worker(self, target: callable) -> bool:
        with self.worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                messagebox.showwarning("Nadawanie", "Nadawanie już jest uruchomione. Najpierw kliknij Stop.")
                return False
            self.stop_event.clear()
            self.broadcaster.set_destinations(self._current_destinations())
            if not self._current_destinations():
                messagebox.showwarning("Klienci", "Nie wybrano żadnego klienta")
                return False
            self.broadcaster.start_sender()
            self.worker_thread = threading.Thread(target=target, daemon=True)
            self.worker_thread.start()
            return True

    def start_microphone(self) -> None:
        def worker() -> None:
            def callback(indata, _frames, _time_info, status):
                if status:
                    logging.warning("Input status: %s", status)
                self.broadcaster.enqueue_pcm(bytes(indata))

            try:
                with sd.RawInputStream(
                    samplerate=self.config.sample_rate,
                    blocksize=self.config.blocksize,
                    channels=self.config.channels,
                    dtype="int16",
                    device=self.config.mic_input_device,
                    callback=callback,
                ):
                    while not self.stop_event.is_set():
                        time.sleep(0.2)
            except sd.PortAudioError as exc:
                logging.error("Mikrofon niedostępny: %s", exc)
                self.root.after(0, lambda: messagebox.showerror("Audio", f"Mikrofon niedostępny: {exc}"))
                self.stop_playback()

        self._start_worker(worker)

    def _decode_track_chunks(self, path: Path):
        segment = AudioSegment.from_file(path)
        segment = segment.set_channels(self.config.channels).set_frame_rate(self.config.sample_rate).set_sample_width(2)
        raw = segment.raw_data
        chunk_size = self.config.blocksize * self.config.channels * 2
        for i in range(0, len(raw), chunk_size):
            chunk = raw[i : i + chunk_size]
            if len(chunk) < chunk_size:
                chunk += b"\x00" * (chunk_size - len(chunk))
            yield chunk

    def start_queue(self) -> None:
        def worker() -> None:
            while not self.stop_event.is_set():
                with self.queue_lock:
                    if not self.queue:
                        break
                    track = self.queue.pop(0)
                self.root.after(0, self.refresh_queue_view)
                try:
                    for chunk in self._decode_track_chunks(track):
                        if self.stop_event.is_set():
                            break
                        self.broadcaster.enqueue_pcm(chunk)
                        time.sleep(self.config.blocksize / self.config.sample_rate)
                except Exception as exc:  # noqa: BLE001
                    logging.error("Błąd odczytu utworu %s: %s", track, exc)
            self.stop_playback()

        self._start_worker(worker)

    def stop_playback(self) -> None:
        self.stop_event.set()
        self.broadcaster.stop()
        with self.worker_lock:
            if self.worker_thread and self.worker_thread.is_alive() and threading.current_thread() != self.worker_thread:
                self.worker_thread.join(timeout=1)
            self.worker_thread = None

    def _scan_audio_dir(self, path: Path) -> list[Path]:
        allowed = {".mp3", ".wav", ".ogg"}
        return sorted([p for p in path.iterdir() if p.is_file() and p.suffix.lower() in allowed])

    def load_music_dir(self) -> None:
        folder = filedialog.askdirectory(title="Wybierz katalog muzyki")
        if not folder:
            return
        self.music_tracks = self._scan_audio_dir(Path(folder))
        self.music_list.delete(0, tk.END)
        for p in self.music_tracks:
            self.music_list.insert(tk.END, p.name)

    def load_jingles_dir(self) -> None:
        folder = filedialog.askdirectory(title="Wybierz katalog dźingli")
        if not folder:
            return
        self.jingle_tracks = self._scan_audio_dir(Path(folder))
        self.jingles_list.delete(0, tk.END)
        for p in self.jingle_tracks:
            self.jingles_list.insert(tk.END, p.name)

    def add_music_to_queue(self) -> None:
        idx = self.music_list.curselection()
        if not idx:
            return
        with self.queue_lock:
            self.queue.append(self.music_tracks[idx[0]])
        self.refresh_queue_view()

    def insert_jingle_before(self) -> None:
        j_idx = self.jingles_list.curselection()
        q_idx = self.queue_list.curselection()
        if not j_idx:
            return
        pos = q_idx[0] if q_idx else 0
        with self.queue_lock:
            self.queue.insert(pos, self.jingle_tracks[j_idx[0]])
        self.refresh_queue_view()

    def insert_jingle_after(self) -> None:
        j_idx = self.jingles_list.curselection()
        q_idx = self.queue_list.curselection()
        if not j_idx:
            return
        pos = (q_idx[0] + 1) if q_idx else len(self.queue)
        with self.queue_lock:
            self.queue.insert(pos, self.jingle_tracks[j_idx[0]])
        self.refresh_queue_view()

    def remove_from_queue(self) -> None:
        idx = self.queue_list.curselection()
        if not idx:
            return
        with self.queue_lock:
            del self.queue[idx[0]]
        self.refresh_queue_view()

    def move_up_queue(self) -> None:
        idx = self.queue_list.curselection()
        if not idx or idx[0] == 0:
            return
        i = idx[0]
        with self.queue_lock:
            self.queue[i - 1], self.queue[i] = self.queue[i], self.queue[i - 1]
        self.refresh_queue_view()

    def move_down_queue(self) -> None:
        idx = self.queue_list.curselection()
        if not idx:
            return
        i = idx[0]
        with self.queue_lock:
            if i >= len(self.queue) - 1:
                return
            self.queue[i + 1], self.queue[i] = self.queue[i], self.queue[i + 1]
        self.refresh_queue_view()

    def refresh_queue_view(self) -> None:
        self.queue_list.delete(0, tk.END)
        with self.queue_lock:
            for item in self.queue:
                self.queue_list.insert(tk.END, item.name)

    def save_daily_schedule(self) -> None:
        try:
            start_h, start_m = [int(x) for x in self.start_time_var.get().split(":")]
            stop_h, stop_m = [int(x) for x in self.stop_time_var.get().split(":")]
            if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and 0 <= stop_h <= 23 and 0 <= stop_m <= 59):
                raise ValueError("Time out of range")
        except ValueError:
            messagebox.showerror("Harmonogram", "Błędny format czasu. Użyj HH:MM")
            return

        self.scheduler.remove_all_jobs()
        self.scheduler.add_job(self.start_queue, "cron", hour=start_h, minute=start_m)
        self.scheduler.add_job(self.stop_playback, "cron", hour=stop_h, minute=stop_m)
        messagebox.showinfo("Harmonogram", "Zapisano harmonogram dzienny start/stop kolejki")

    def run(self) -> None:
        self.root.mainloop()


def run_server(config_path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    cfg = ServerConfig.from_file(config_path)
    app = ServerApp(cfg)
    app.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serwer radiowęzła szkolnego")
    parser.add_argument("--config", type=Path, default=Path("server-config.json"))
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    run_server(args.config)
