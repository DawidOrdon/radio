from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import shutil
import socket
import struct
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

    def clear_pending_packets(self) -> None:
        while not self.packet_queue.empty():
            try:
                self.packet_queue.get_nowait()
            except queue.Empty:
                break

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
        self.displayed_music_tracks: list[Path] = []
        self.displayed_jingle_tracks: list[Path] = []
        self.queue_position = 0

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.worker_lock = threading.Lock()
        self.current_track: Path | None = None
        self.current_track_byte_pos = 0

        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        self.schedule_job_ids: list[str] = []

        self._refresh_loop_enabled = True
        self._client_vars: dict[str, tk.BooleanVar] = {}
        self._client_states_by_id: dict[str, ClientState] = {}

        self.root = tk.Tk()
        self.root.title("RadioWęzeł - Serwer")
        self._build_ui()
        self._warn_if_ffmpeg_missing()
        self._refresh_clients_periodic()

    def _build_ui(self) -> None:
        self.root.geometry("1250x780")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=8)

        self.offset_seconds_var = tk.StringVar(value=f"{self.config.global_offset_ms / 1000:.1f}")
        ttk.Label(top, text="Offset (sekundy):").pack(side="left")
        ttk.Entry(top, textvariable=self.offset_seconds_var, width=7).pack(side="left", padx=6)
        ttk.Button(top, text="Ustaw offset na klientach", command=self.push_offset).pack(side="left", padx=6)

        ttk.Button(top, text="Start mikrofon (pass-through)", command=self.start_microphone).pack(side="left", padx=6)
        ttk.Button(top, text="Start kolejki", command=self.start_queue).pack(side="left", padx=6)
        ttk.Button(top, text="Pauza", command=self.pause_playback).pack(side="left", padx=4)
        ttk.Button(top, text="Wznów", command=self.resume_playback).pack(side="left", padx=4)
        ttk.Button(top, text="Stop nadawania", command=self.stop_playback).pack(side="left", padx=6)
        ttk.Button(top, text="Test ton 1kHz", command=self.play_test_tone_to_clients).pack(side="left", padx=6)
        self.now_playing_var = tk.StringVar(value="Teraz odtwarzane: -")
        ttk.Label(top, textvariable=self.now_playing_var).pack(side="left", padx=12)
        self.stream_state_var = tk.StringVar(value="Stan streamu: STOP")
        ttk.Label(top, textvariable=self.stream_state_var).pack(side="left", padx=8)

        params = ttk.LabelFrame(self.root, text="Parametry audio (zmiana z poziomu programu)")
        params.pack(fill="x", padx=8, pady=8)
        self.sample_rate_var = tk.StringVar(value=str(self.config.sample_rate))
        self.channels_var = tk.StringVar(value=str(self.config.channels))
        self.blocksize_var = tk.StringVar(value=str(self.config.blocksize))
        self.input_device_var = tk.StringVar(value="" if self.config.mic_input_device is None else str(self.config.mic_input_device))
        ttk.Label(params, text="Sample rate:").pack(side="left", padx=4)
        ttk.Entry(params, textvariable=self.sample_rate_var, width=8).pack(side="left")
        ttk.Label(params, text="Kanały:").pack(side="left", padx=4)
        ttk.Entry(params, textvariable=self.channels_var, width=4).pack(side="left")
        ttk.Label(params, text="Blocksize:").pack(side="left", padx=4)
        ttk.Entry(params, textvariable=self.blocksize_var, width=6).pack(side="left")
        ttk.Label(params, text="Mic input idx:").pack(side="left", padx=4)
        ttk.Entry(params, textvariable=self.input_device_var, width=8).pack(side="left")
        ttk.Button(params, text="Zastosuj parametry", command=self.apply_runtime_audio_settings).pack(side="left", padx=8)
        ttk.Button(params, text="Lista wejść audio", command=self.show_input_devices).pack(side="left", padx=6)

        center = ttk.Panedwindow(self.root, orient="horizontal")
        center.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(center)
        mid = ttk.Frame(center)
        right = ttk.Frame(center)
        center.add(left, weight=1)
        center.add(mid, weight=1)
        center.add(right, weight=1)

        ttk.Label(left, text="Klienci").pack(anchor="w")
        self.clients_container = ttk.Frame(left)
        self.clients_container.pack(fill="both", expand=True)
        self.clients_canvas = tk.Canvas(self.clients_container, height=320)
        self.clients_scrollbar = ttk.Scrollbar(self.clients_container, orient="vertical", command=self.clients_canvas.yview)
        self.clients_inner = ttk.Frame(self.clients_canvas)
        self.clients_inner.bind(
            "<Configure>",
            lambda _e: self.clients_canvas.configure(scrollregion=self.clients_canvas.bbox("all")),
        )
        self.clients_canvas.create_window((0, 0), window=self.clients_inner, anchor="nw")
        self.clients_canvas.configure(yscrollcommand=self.clients_scrollbar.set)
        self.clients_canvas.pack(side="left", fill="both", expand=True)
        self.clients_scrollbar.pack(side="right", fill="y")
        ttk.Button(left, text="Odśwież klientów", command=self._refresh_clients_once).pack(anchor="w", pady=4)
        ttk.Button(left, text="Wybierz wyjście audio klienta", command=self.choose_output_on_selected).pack(anchor="w", pady=4)

        ttk.Label(mid, text="Muzyka (katalog)").pack(anchor="w")
        ttk.Button(mid, text="Wybierz katalog muzyki", command=self.load_music_dir).pack(anchor="w")
        self.music_search_var = tk.StringVar(value="")
        ttk.Entry(mid, textvariable=self.music_search_var).pack(fill="x", pady=(4, 4))
        self.music_search_var.trace_add("write", lambda *_: self.refresh_music_view())
        self.music_list = tk.Listbox(mid, height=12)
        self.music_list.pack(fill="x")
        ttk.Button(mid, text="Dodaj do kolejki", command=self.add_music_to_queue).pack(anchor="w", pady=4)

        ttk.Label(mid, text="Dźingle (katalog)").pack(anchor="w", pady=(8, 0))
        ttk.Button(mid, text="Wybierz katalog dźingli", command=self.load_jingles_dir).pack(anchor="w")
        self.jingle_search_var = tk.StringVar(value="")
        ttk.Entry(mid, textvariable=self.jingle_search_var).pack(fill="x", pady=(4, 4))
        self.jingle_search_var.trace_add("write", lambda *_: self.refresh_jingles_view())
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

        sched = ttk.LabelFrame(self.root, text="Automatyka kolejki (wiele przerw)")
        sched.pack(fill="x", padx=8, pady=8)
        ttk.Label(sched, text="Start HH:MM").pack(side="left", padx=4)
        self.start_time_var = tk.StringVar(value="07:55")
        ttk.Entry(sched, textvariable=self.start_time_var, width=8).pack(side="left")
        ttk.Label(sched, text="Stop HH:MM").pack(side="left", padx=4)
        self.stop_time_var = tk.StringVar(value="08:05")
        ttk.Entry(sched, textvariable=self.stop_time_var, width=8).pack(side="left")
        ttk.Button(sched, text="Dodaj przedział", command=self.add_schedule_interval).pack(side="left", padx=8)
        ttk.Button(sched, text="Wyczyść harmonogram", command=self.clear_schedule).pack(side="left", padx=8)

        self.schedule_list = tk.Listbox(self.root, height=5)
        self.schedule_list.pack(fill="x", padx=8, pady=(0, 8))

    def _on_close(self) -> None:
        self._refresh_loop_enabled = False
        self.stop_playback()
        self.scheduler.shutdown(wait=False)
        self.root.destroy()

    def _warn_if_ffmpeg_missing(self) -> None:
        ffmpeg_ok = shutil.which("ffmpeg") is not None
        ffprobe_ok = shutil.which("ffprobe") is not None
        if not ffmpeg_ok or not ffprobe_ok:
            messagebox.showwarning(
                "FFmpeg/FFprobe",
                "Nie wykryto ffmpeg/ffprobe w PATH. MP3 może nie działać.\n"
                "Zainstaluj FFmpeg i dodaj katalog bin do zmiennej PATH.",
            )

    def apply_runtime_audio_settings(self) -> None:
        try:
            sample_rate = int(self.sample_rate_var.get())
            channels = int(self.channels_var.get())
            blocksize = int(self.blocksize_var.get())
            input_device = self.input_device_var.get().strip()
            mic_input_device = int(input_device) if input_device else None
            if sample_rate <= 0 or channels <= 0 or blocksize <= 0:
                raise ValueError("Dodatnie liczby wymagane")
        except ValueError:
            messagebox.showerror("Parametry", "Niepoprawne parametry audio")
            return

        was_running = self.worker_thread is not None and self.worker_thread.is_alive()
        self.stop_playback()

        self.config.sample_rate = sample_rate
        self.config.channels = channels
        self.config.blocksize = blocksize
        self.config.mic_input_device = mic_input_device

        if was_running:
            messagebox.showinfo("Parametry", "Parametry zastosowane. Uruchom ponownie nadawanie.")
        else:
            messagebox.showinfo("Parametry", "Parametry audio zapisane w działającej sesji.")

    def show_input_devices(self) -> None:
        items = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                items.append(f"{idx}: {dev['name']}")
        messagebox.showinfo("Wejścia audio", "\n".join(items) if items else "Brak wejść audio")

    def _refresh_clients_once(self) -> None:
        current_selection = {cid for cid, var in self._client_vars.items() if var.get()}
        self._client_states_by_id = {c.hello.client_id: c for c in self.registry.get_clients()}

        for child in self.clients_inner.winfo_children():
            child.destroy()

        new_vars: dict[str, tk.BooleanVar] = {}
        for client_id, c in self._client_states_by_id.items():
            age = int(time.time() - c.last_seen)
            checked = client_id in current_selection
            var = tk.BooleanVar(value=checked)
            text = f"{c.hello.client_id} | {c.hello.client_name} | {c.address} | {age}s"
            cb = ttk.Checkbutton(self.clients_inner, text=text, variable=var)
            cb.pack(anchor="w", fill="x", padx=2, pady=1)
            new_vars[client_id] = var

        self._client_vars = new_vars

    def _refresh_clients_periodic(self) -> None:
        self._refresh_clients_once()
        if self._refresh_loop_enabled:
            self.root.after(2000, self._refresh_clients_periodic)

    def _selected_clients(self) -> list[ClientState]:
        selected = []
        for client_id, var in self._client_vars.items():
            if var.get():
                state = self._client_states_by_id.get(client_id)
                if state is not None:
                    selected.append(state)
        return selected

    def _current_destinations(self) -> list[tuple[str, int]]:
        return [(c.address, c.hello.audio_port) for c in self._selected_clients()]

    def push_offset(self) -> None:
        try:
            offset_seconds = float(self.offset_seconds_var.get().replace(",", "."))
            if offset_seconds < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Offset", "Podaj offset jako liczbę sekund, np. 2 albo 2.5")
            return

        offset_value_ms = int(offset_seconds * 1000)
        self.config.global_offset_ms = offset_value_ms

        failures = []
        for client in self._selected_clients():
            try:
                self.control.send(client.address, client.hello.control_port, ControlMessage("set_offset_ms", {"value": offset_value_ms}))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{client.hello.client_id}: {exc}")
        if failures:
            messagebox.showwarning("Offset", "Nie ustawiono offsetu na części klientów:\n" + "\n".join(failures))

    def _fetch_client_output_devices(self, client: ClientState) -> list[dict]:
        response = self.control.send(client.address, client.hello.control_port, ControlMessage("list_output_devices", {}))
        if response.cmd != "output_devices":
            raise RuntimeError(f"Unexpected response: {response.cmd}")
        return response.payload.get("items", [])

    def choose_output_on_selected(self) -> None:
        selected_clients = self._selected_clients()
        if not selected_clients:
            messagebox.showwarning("Output", "Wybierz co najmniej jednego klienta")
            return

        # pobieramy listę z pierwszego klienta jako referencję
        try:
            devices = self._fetch_client_output_devices(selected_clients[0])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Output", f"Nie można pobrać listy wyjść: {exc}")
            return

        if not devices:
            messagebox.showwarning("Output", "Brak urządzeń wyjściowych na kliencie")
            return

        lines = [f"{d['index']}: {d['name']}" for d in devices]
        choice = simpledialog.askstring(
            "Wyjście audio klienta",
            "Wpisz indeks urządzenia z listy:\n\n" + "\n".join(lines) + "\n\nPuste = domyślne",
        )

        if choice is None:
            return

        device: int | None
        value = choice.strip()
        if value == "":
            device = None
        else:
            try:
                device = int(value)
            except ValueError:
                messagebox.showerror("Output", "Podany indeks nie jest liczbą")
                return

        failures = []
        for client in selected_clients:
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
        else:
            messagebox.showinfo("Output", "Ustawiono wyjście audio na wybranych klientach")

    def _start_worker(self, target: callable) -> bool:
        with self.worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                messagebox.showwarning("Nadawanie", "Nadawanie już jest uruchomione. Najpierw kliknij Stop.")
                return False
            self.stop_event.clear()
            self.pause_event.clear()
            destinations = self._current_destinations()
            self.broadcaster.set_destinations(destinations)
            if not destinations:
                messagebox.showwarning("Klienci", "Nie wybrano żadnego klienta")
                return False
            self.broadcaster.start_sender()
            self.worker_thread = threading.Thread(target=target, daemon=True)
            self.worker_thread.start()
            self.stream_state_var.set("Stan streamu: START")
            return True

    def start_microphone(self) -> None:
        # Pass-through: to, co jest na wejściu z interfejsu audio, wysyłane jest bezpośrednio do klientów.
        def worker() -> None:
            def callback(indata, _frames, _time_info, status):
                if status:
                    logging.warning("Input status: %s", status)
                if self.pause_event.is_set():
                    return
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
                        while self.pause_event.is_set() and not self.stop_event.is_set():
                            time.sleep(0.05)
                        time.sleep(0.2)
            except sd.PortAudioError as exc:
                logging.error("Mikrofon/interfejs niedostępny: %s", exc)
                self.root.after(0, lambda: messagebox.showerror("Audio", f"Wejście audio niedostępne: {exc}"))
                self.stop_playback()

        self._start_worker(worker)

    def _decode_track_chunks(self, path: Path, start_byte: int = 0):
        segment = AudioSegment.from_file(path)
        segment = segment.set_channels(self.config.channels).set_frame_rate(self.config.sample_rate).set_sample_width(2)
        raw = segment.raw_data
        chunk_size = self.config.blocksize * self.config.channels * 2
        safe_start = max(0, min(start_byte, len(raw)))
        for i in range(safe_start, len(raw), chunk_size):
            chunk = raw[i : i + chunk_size]
            if len(chunk) < chunk_size:
                chunk += b"\x00" * (chunk_size - len(chunk))
            yield chunk

    def start_queue(self) -> None:
        selected_queue_idx = self.queue_list.curselection()
        if selected_queue_idx:
            self.queue_position = selected_queue_idx[0]
        else:
            with self.queue_lock:
                if self.queue_position >= len(self.queue):
                    self.queue_position = 0

        def worker() -> None:
            while not self.stop_event.is_set():
                with self.queue_lock:
                    if not self.queue:
                        break
                    if self.queue_position >= len(self.queue):
                        break
                    track = self.queue[self.queue_position]
                if self.current_track != track:
                    self.current_track = track
                    self.current_track_byte_pos = 0
                self.root.after(0, lambda t=track: self.now_playing_var.set(f"Teraz odtwarzane: {t.name}"))
                try:
                    for chunk in self._decode_track_chunks(track, start_byte=self.current_track_byte_pos):
                        if self.stop_event.is_set():
                            break
                        while self.pause_event.is_set() and not self.stop_event.is_set():
                            time.sleep(0.05)
                        if self.stop_event.is_set():
                            break
                        self.broadcaster.enqueue_pcm(chunk)
                        time.sleep(self.config.blocksize / self.config.sample_rate)
                        self.current_track_byte_pos += len(chunk)
                except Exception as exc:  # noqa: BLE001
                    logging.error("Błąd odczytu utworu %s: %s", track, exc)
                with self.queue_lock:
                    if not self.stop_event.is_set() and not self.pause_event.is_set():
                        self.queue_position += 1
                        self.current_track = None
                        self.current_track_byte_pos = 0
                self.root.after(0, self.refresh_queue_view)
            self.root.after(0, lambda: self.now_playing_var.set("Teraz odtwarzane: -"))
            self.stop_playback()

        self._start_worker(worker)

    def pause_playback(self) -> None:
        self.pause_event.set()
        self.broadcaster.clear_pending_packets()
        self.stream_state_var.set("Stan streamu: PAUZA")

    def resume_playback(self) -> None:
        self.pause_event.clear()
        if self.worker_thread and self.worker_thread.is_alive():
            self.stream_state_var.set("Stan streamu: START")

    def stop_playback(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.broadcaster.stop()
        self.stream_state_var.set("Stan streamu: STOP")
        self.current_track = None
        self.current_track_byte_pos = 0
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
        self.refresh_music_view()

    def load_jingles_dir(self) -> None:
        folder = filedialog.askdirectory(title="Wybierz katalog dźingli")
        if not folder:
            return
        self.jingle_tracks = self._scan_audio_dir(Path(folder))
        self.refresh_jingles_view()

    def refresh_music_view(self) -> None:
        query = self.music_search_var.get().strip().lower()
        if query:
            self.displayed_music_tracks = [p for p in self.music_tracks if query in p.name.lower()]
        else:
            self.displayed_music_tracks = list(self.music_tracks)
        self.music_list.delete(0, tk.END)
        for p in self.displayed_music_tracks:
            self.music_list.insert(tk.END, p.name)

    def refresh_jingles_view(self) -> None:
        query = self.jingle_search_var.get().strip().lower()
        if query:
            self.displayed_jingle_tracks = [p for p in self.jingle_tracks if query in p.name.lower()]
        else:
            self.displayed_jingle_tracks = list(self.jingle_tracks)
        self.jingles_list.delete(0, tk.END)
        for p in self.displayed_jingle_tracks:
            self.jingles_list.insert(tk.END, p.name)

    def add_music_to_queue(self) -> None:
        idx = self.music_list.curselection()
        if not idx:
            return
        if idx[0] >= len(self.displayed_music_tracks):
            return
        with self.queue_lock:
            self.queue.append(self.displayed_music_tracks[idx[0]])
        self.refresh_queue_view()

    def insert_jingle_before(self) -> None:
        j_idx = self.jingles_list.curselection()
        q_idx = self.queue_list.curselection()
        if not j_idx:
            return
        if j_idx[0] >= len(self.displayed_jingle_tracks):
            return
        pos = q_idx[0] if q_idx else 0
        with self.queue_lock:
            self.queue.insert(pos, self.displayed_jingle_tracks[j_idx[0]])
        self.refresh_queue_view()

    def insert_jingle_after(self) -> None:
        j_idx = self.jingles_list.curselection()
        q_idx = self.queue_list.curselection()
        if not j_idx:
            return
        if j_idx[0] >= len(self.displayed_jingle_tracks):
            return
        pos = (q_idx[0] + 1) if q_idx else len(self.queue)
        with self.queue_lock:
            self.queue.insert(pos, self.displayed_jingle_tracks[j_idx[0]])
        self.refresh_queue_view()

    def remove_from_queue(self) -> None:
        idx = self.queue_list.curselection()
        if not idx:
            return
        with self.queue_lock:
            del self.queue[idx[0]]
            if self.queue_position >= len(self.queue):
                self.queue_position = max(0, len(self.queue) - 1)
        self.refresh_queue_view()

    def move_up_queue(self) -> None:
        idx = self.queue_list.curselection()
        if not idx or idx[0] == 0:
            return
        i = idx[0]
        with self.queue_lock:
            self.queue[i - 1], self.queue[i] = self.queue[i], self.queue[i - 1]
            if self.queue_position == i:
                self.queue_position = i - 1
            elif self.queue_position == i - 1:
                self.queue_position = i
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
            if self.queue_position == i:
                self.queue_position = i + 1
            elif self.queue_position == i + 1:
                self.queue_position = i
        self.refresh_queue_view()

    def refresh_queue_view(self) -> None:
        self.queue_list.delete(0, tk.END)
        with self.queue_lock:
            for idx, item in enumerate(self.queue):
                prefix = "▶ " if idx == self.queue_position else "   "
                self.queue_list.insert(tk.END, f"{prefix}{item.name}")

    def _parse_hh_mm(self, value: str) -> tuple[int, int]:
        hour, minute = [int(x) for x in value.split(":")]
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute

    def add_schedule_interval(self) -> None:
        try:
            start_h, start_m = self._parse_hh_mm(self.start_time_var.get())
            stop_h, stop_m = self._parse_hh_mm(self.stop_time_var.get())
        except ValueError:
            messagebox.showerror("Harmonogram", "Błędny format czasu. Użyj HH:MM")
            return

        start_id = f"start-{start_h:02d}{start_m:02d}-{time.time_ns()}"
        stop_id = f"stop-{stop_h:02d}{stop_m:02d}-{time.time_ns()}"

        self.scheduler.add_job(self.start_queue, "cron", hour=start_h, minute=start_m, id=start_id)
        self.scheduler.add_job(self.stop_playback, "cron", hour=stop_h, minute=stop_m, id=stop_id)
        self.schedule_job_ids.extend([start_id, stop_id])
        self.schedule_list.insert(tk.END, f"START {start_h:02d}:{start_m:02d} | STOP {stop_h:02d}:{stop_m:02d}")

    def play_test_tone_to_clients(self) -> None:
        destinations = self._current_destinations()
        if not destinations:
            messagebox.showwarning("Test tonu", "Najpierw zaznacz klientów")
            return

        self.stop_event.clear()
        self.broadcaster.set_destinations(destinations)
        self.broadcaster.start_sender()
        self.stream_state_var.set("Stan streamu: TEST TON")

        def worker() -> None:
            sr = self.config.sample_rate
            ch = self.config.channels
            block = self.config.blocksize
            total_frames = int(sr * 1.5)
            for start in range(0, total_frames, block):
                if self.stop_event.is_set():
                    break
                end = min(start + block, total_frames)
                buf = bytearray()
                for i in range(start, end):
                    sample = int(0.25 * 32767 * math.sin(2 * math.pi * 1000 * (i / sr)))
                    packed = struct.pack("<h", sample)
                    for _ in range(ch):
                        buf.extend(packed)
                target_len = block * ch * 2
                if len(buf) < target_len:
                    buf.extend(b"\x00" * (target_len - len(buf)))
                self.broadcaster.enqueue_pcm(bytes(buf))
                time.sleep(block / sr)
            self.stream_state_var.set("Stan streamu: STOP")
            self.broadcaster.stop()

        threading.Thread(target=worker, daemon=True).start()

    def clear_schedule(self) -> None:
        for job_id in self.schedule_job_ids:
            try:
                self.scheduler.remove_job(job_id)
            except Exception:  # noqa: BLE001
                pass
        self.schedule_job_ids.clear()
        self.schedule_list.delete(0, tk.END)

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
