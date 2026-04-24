from __future__ import annotations

import argparse
import logging
import math
import struct
import tkinter as tk
import time
from pathlib import Path
from tkinter import messagebox, ttk

import sounddevice as sd

from radio_wz.client.client_service import AudioReceiver, ClientAnnouncer, ClientConfig, ClientRuntimeState, ControlServer


class ClientGuiApp:
    def __init__(self, config_path: Path) -> None:
        if not config_path.exists():
            default_cfg = ClientConfig.default()
            default_cfg.write_to_file(config_path)
        self.config = ClientConfig.from_file(config_path)
        self.state = ClientRuntimeState(self.config)

        self.announcer = ClientAnnouncer(self.config)
        self.receiver = AudioReceiver(self.config, self.state)
        self.control = ControlServer(self.config, self.state)

        self.root = tk.Tk()
        self.root.title(f"RadioWęzeł Klient - {self.config.client_name}")
        self.root.geometry("560x330")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.running = False
        self.status_var = tk.StringVar(value="Zatrzymany")
        self.audio_live_var = tk.StringVar(value="Audio RX: brak")
        self.offset_var = tk.StringVar(value=str(self.state.get_offset() / 1000))
        self.output_var = tk.StringVar(value="" if self.state.get_output_device() is None else str(self.state.get_output_device()))
        self.output_devices: list[tuple[int, str]] = []

        self._build_ui()

    def _build_ui(self) -> None:
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=f"Client ID: {self.config.client_id}").pack(anchor="w")
        ttk.Label(frm, text=f"Nazwa: {self.config.client_name}").pack(anchor="w")
        ttk.Label(frm, text=f"Port audio: {self.config.audio_port}, control: {self.config.control_port}").pack(anchor="w", pady=(0, 10))

        ttk.Label(frm, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(frm, textvariable=self.audio_live_var, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 8))

        params = ttk.LabelFrame(frm, text="Lokalne ustawienia klienta")
        params.pack(fill="x")

        row = ttk.Frame(params)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="Offset (sekundy):").pack(side="left")
        ttk.Entry(row, textvariable=self.offset_var, width=8).pack(side="left", padx=6)
        ttk.Label(row, text="Output index:").pack(side="left", padx=(12, 0))
        ttk.Entry(row, textvariable=self.output_var, width=8).pack(side="left", padx=6)
        ttk.Button(row, text="Zastosuj", command=self.apply_local_settings).pack(side="left", padx=8)
        ttk.Button(row, text="Pokaż wyjścia audio", command=self.show_output_devices).pack(side="left")
        ttk.Button(row, text="Test tonu", command=self.play_test_tone).pack(side="left", padx=6)

        row2 = ttk.Frame(params)
        row2.pack(fill="x", padx=8, pady=(0, 8))
        self.output_combo = ttk.Combobox(row2, state="readonly", width=55)
        self.output_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="Odśwież listę", command=self.refresh_output_combo).pack(side="left", padx=6)
        ttk.Button(row2, text="Użyj zaznaczonego", command=self.apply_selected_output).pack(side="left")

        actions = ttk.Frame(frm)
        actions.pack(fill="x", pady=12)
        ttk.Button(actions, text="Start klienta", command=self.start_client).pack(side="left", padx=4)
        ttk.Button(actions, text="Stop klienta", command=self.stop_client).pack(side="left", padx=4)

        help_txt = (
            "Ten panel to front klienta.\n"
            "Klient może działać jako usługa bez GUI, ale tu możesz łatwo sprawdzić status i ustawić output."
        )
        ttk.Label(frm, text=help_txt).pack(anchor="w", pady=(12, 0))

    def apply_local_settings(self) -> None:
        try:
            offset_s = float(self.offset_var.get().replace(",", "."))
            if offset_s < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Błąd", "Offset musi być liczbą >= 0")
            return

        output_text = self.output_var.get().strip()
        if output_text:
            try:
                out = int(output_text)
            except ValueError:
                messagebox.showerror("Błąd", "Output index musi być liczbą lub pusty")
                return
        else:
            out = None

        self.state.set_offset(int(offset_s * 1000))
        self.state.set_output_device(out)
        messagebox.showinfo("OK", "Ustawienia lokalne zapisane")

    def show_output_devices(self) -> None:
        devices = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0:
                devices.append(f"{idx}: {dev['name']}")
        messagebox.showinfo("Wyjścia audio", "\n".join(devices) if devices else "Brak wyjść audio")

    def refresh_output_combo(self) -> None:
        self.output_devices = []
        items = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0:
                self.output_devices.append((idx, dev["name"]))
                items.append(f"{idx}: {dev['name']}")
        self.output_combo["values"] = items
        if items:
            self.output_combo.current(0)

    def apply_selected_output(self) -> None:
        if not self.output_combo.get():
            return
        raw = self.output_combo.get().split(":", 1)[0].strip()
        try:
            idx = int(raw)
        except ValueError:
            messagebox.showerror("Błąd", "Nieprawidłowy indeks urządzenia")
            return
        self.output_var.set(str(idx))
        self.apply_local_settings()

    def play_test_tone(self) -> None:
        try:
            device = self.state.get_output_device()
            sr = self.config.sample_rate
            channels = self.config.channels
            frames = int(sr * 1.0)
            chunk = 480
            with sd.RawOutputStream(
                samplerate=sr,
                blocksize=chunk,
                channels=channels,
                dtype="int16",
                device=device,
            ) as stream:
                for start in range(0, frames, chunk):
                    buf = bytearray()
                    end = min(start + chunk, frames)
                    for i in range(start, end):
                        sample = int(0.25 * 32767 * math.sin(2 * math.pi * 1000 * (i / sr)))
                        packed = struct.pack("<h", sample)
                        for _ in range(channels):
                            buf.extend(packed)
                    stream.write(bytes(buf))
            messagebox.showinfo("Test tonu", "Wysłano ton testowy 1kHz na wybrane wyjście.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Test tonu", f"Nie udało się odtworzyć tonu: {exc}")

    def start_client(self) -> None:
        if self.running:
            return
        self.running = True
        self.announcer.start()
        self.receiver.start()
        self.control.start()
        self.status_var.set("Uruchomiony")

    def stop_client(self) -> None:
        if not self.running:
            return
        self.announcer.stop()
        self.receiver.stop()
        self.running = False
        self.status_var.set("Zatrzymany")

    def _on_close(self) -> None:
        self.stop_client()
        self.root.destroy()

    def run(self) -> None:
        self.start_client()
        self.refresh_output_combo()
        self._update_audio_indicator_loop()
        self.root.mainloop()

    def _update_audio_indicator_loop(self) -> None:
        ts, rms = self.state.get_audio_status()
        if time.time() - ts < 1.5:
            self.audio_live_var.set(f"Audio RX: AKTYWNY (RMS={rms})")
        else:
            self.audio_live_var.set("Audio RX: brak")
        self.root.after(500, self._update_audio_indicator_loop)


def run_gui(config_path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    try:
        app = ClientGuiApp(config_path)
        app.run()
    except Exception as exc:  # noqa: BLE001
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Błąd konfiguracji klienta",
            f"Nie udało się uruchomić klienta.\nSzczegóły: {exc}\n\n"
            f"Sprawdź plik konfiguracji: {config_path}",
        )
        root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GUI klienta radiowęzła")
    parser.add_argument("--config", type=Path, default=Path("client-config.json"))
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    run_gui(args.config)
