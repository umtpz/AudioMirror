"""
AudioMirror
Requirements: pip install pyaudiowpatch customtkinter numpy pystray pillow
"""

import json, os, queue, threading
from datetime import datetime
from pathlib import Path

import numpy as np
import customtkinter as ctk
import pyaudiowpatch as pyaudio
from tkinter import messagebox
import pystray
from PIL import Image, ImageDraw
import keyboard

# ── Beep ─────────────────────────────────────────────────────────────────────
def play_beep(freq: float, duration_ms: int = 80, volume: float = 1.0):
    """Play a sine wave beep through the default output non-blocking."""
    def _play():
        try:
            import pyaudiowpatch as pyaudio
            rate = 44100
            frames = int(rate * duration_ms / 1000)
            t = np.linspace(0, duration_ms / 1000, frames, dtype=np.float32)
            wave = (np.sin(2 * np.pi * freq * t) * volume * 0.8).astype(np.float32)
            # Fade in/out to avoid clicks
            fade = min(int(rate * 0.01), frames // 4)
            wave[:fade] *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            pa = pyaudio.PyAudio()
            s = pa.open(format=pyaudio.paFloat32, channels=1, rate=rate, output=True)
            s.write(wave.tobytes())
            s.stop_stream(); s.close()
            pa.terminate()
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.environ.get("APPDATA", ".")) / "AudioMirror" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}

def save_config(data):
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

# ── Theme ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG     = "#1C1C1C"
CARD   = "#252525"
CARD2  = "#2D2D2D"
CARD3  = "#333333"
BORDER = "#3A3A3A"
TEXT   = "#E8E8E8"
SUBT   = "#888888"
ACCENT = "#4A90D9"
ACCH   = "#5BA3E8"
RED    = "#C0392B"
REDH   = "#E74C3C"
GREEN  = "#27AE60"


# ── Audio Engine ──────────────────────────────────────────────────────────────
class OutputSink:
    def __init__(self):
        self.delay_ms = 0
        self.reverse  = False
        self.volume   = 1.0
        self._dst_rate = 48000
        self._src_rate = 48000
        self._thread  = None
        self._q       = queue.Queue(maxsize=300)
        self._running = False

    def start(self, pa, dst_idx, dst_rate, src_rate, dst_channels, chunk):
        self._running   = True
        self._dst_rate  = dst_rate
        self._src_rate  = src_rate
        self._dst_ch    = dst_channels
        silence = np.zeros(chunk * dst_channels, dtype=np.float32).tobytes()

        def run():
            try:
                s = pa.open(format=pyaudio.paFloat32, channels=dst_channels, rate=dst_rate,
                            output=True, output_device_index=dst_idx,
                            frames_per_buffer=chunk)
                buf = []
                ms_per_pkt = (chunk / dst_rate) * 1000
                while self._running:
                    try:
                        pkt = self._q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    buf.append(pkt)
                    needed = int(self.delay_ms / ms_per_pkt)
                    out = buf.pop(0) if len(buf) > needed + 1 else silence
                    try:
                        s.write(out)
                    except Exception:
                        break
                s.stop_stream(); s.close()
            except Exception:
                pass

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    @staticmethod
    def _adapt_channels(data: np.ndarray, dst_ch: int) -> np.ndarray:
        """Convert any N-channel stereo data to dst_ch channels.
        - Upmix: repeat L/R across extra channels
        - Downmix: mix down to stereo then tile
        """
        src_ch = data.shape[1]
        if src_ch == dst_ch:
            return data
        # Always work in stereo first (data is already stereo here)
        # Upmix stereo -> dst_ch by tiling L/R
        if dst_ch > 2:
            out = np.zeros((len(data), dst_ch), dtype=np.float32)
            for i in range(dst_ch):
                out[:, i] = data[:, i % 2]
            return out
        # Downmix: mix all channels to stereo (already stereo, shouldn't happen)
        return data[:, :2]

    def push(self, stereo: np.ndarray):
        s = stereo[:, ::-1].copy() if self.reverse else stereo.copy()
        s *= self.volume
        # Resample if rates differ
        if self._src_rate != self._dst_rate:
            ratio   = self._dst_rate / self._src_rate
            new_len = int(len(s) * ratio)
            rs = np.zeros((new_len, 2), dtype=np.float32)
            for ch in range(2):
                rs[:, ch] = np.interp(
                    np.linspace(0, len(s)-1, new_len),
                    np.arange(len(s)), s[:, ch])
            s = rs
        # Adapt to destination channel count
        s = self._adapt_channels(s, self._dst_ch)
        try:
            self._q.put_nowait(s.flatten().tobytes())
        except queue.Full:
            pass

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None


class AudioEngine:
    def __init__(self):
        self.running = False
        self._pa = None
        self._stream_in = None
        self.sinks: list[OutputSink] = []
        self.viz_callback = None  # fn(rms_per_band: list[float])

    def get_loopback_devices(self):
        pa = pyaudio.PyAudio()
        devs = []
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("hostApi") == wasapi["index"] and info.get("isLoopbackDevice"):
                    name = info["name"].replace("[Loopback]", "").strip()
                    devs.append({"index": i, "name": name, "info": info})
        except Exception:
            pass
        pa.terminate()
        return devs

    def get_output_devices(self):
        pa = pyaudio.PyAudio()
        devs, seen = [], set()
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if (info.get("hostApi") == wasapi["index"]
                        and info.get("maxOutputChannels", 0) >= 2
                        and not info.get("isLoopbackDevice")):
                    n = info["name"]
                    if n not in seen:
                        seen.add(n)
                        devs.append({"index": i, "name": n, "info": info})
        except Exception:
            pass
        pa.terminate()
        return devs

    def start(self, src_idx, src_info, sink_configs):
        self.running    = True
        self._pa        = pyaudio.PyAudio()
        self.sinks      = []
        src_channels    = int(src_info.get("maxInputChannels", 2))
        src_rate        = int(src_info.get("defaultSampleRate", 48000))
        chunk           = 1024

        for cfg in sink_configs:
            sink = OutputSink()
            sink.reverse   = cfg["reverse"]
            sink.delay_ms  = cfg["delay_ms"]
            sink.volume    = cfg["volume"]
            dst_rate       = int(cfg["dst_info"].get("defaultSampleRate", 48000))
            dst_channels = int(cfg["dst_info"].get("maxOutputChannels", 2))
            dst_channels = min(dst_channels, 8)  # cap at 8
            sink._dst_ch = dst_channels
            sink.start(self._pa, cfg["dst_idx"], dst_rate, src_rate, dst_channels, chunk)
            self.sinks.append(sink)

        _VIZ_BANDS = 20
        def callback_in(in_data, frame_count, time_info, status):
            if not self.running:
                return (None, pyaudio.paComplete)
            raw    = np.frombuffer(in_data, dtype=np.float32).copy().reshape(-1, src_channels)
            base   = raw[:, :2].copy()
            for sink in self.sinks:
                sink.push(base)
            # Feed visualizer
            if self.viz_callback is not None:
                mono = base.mean(axis=1)
                fft  = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
                fft  = fft[:len(fft)//2]  # only positive freqs up to ~Nyquist/2
                band_size = max(1, len(fft) // _VIZ_BANDS)
                bands = [float(np.mean(fft[i*band_size:(i+1)*band_size]))
                         for i in range(_VIZ_BANDS)]
                self.viz_callback(bands)
            return (None, pyaudio.paContinue)

        self._stream_in = self._pa.open(
            format=pyaudio.paFloat32, channels=src_channels, rate=src_rate,
            input=True, input_device_index=src_idx, frames_per_buffer=chunk,
            stream_callback=callback_in)
        self._stream_in.start_stream()

    def stop(self):
        self.running = False
        try:
            if self._stream_in:
                self._stream_in.stop_stream()
                self._stream_in.close()
        except Exception:
            pass
        for s in self.sinks:
            s.stop()
        self.sinks = []
        try:
            if self._pa:
                self._pa.terminate()
        except Exception:
            pass
        self._stream_in = None
        self._pa = None


# ── Tray image ────────────────────────────────────────────────────────────────
def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.ellipse([4,4,60,60], fill="#4A90D9")
    d.arc([14,14,50,50], -60, 60, fill="white", width=3)
    d.arc([20,20,44,44], -60, 60, fill="white", width=3)
    d.ellipse([28,28,36,36], fill="white")
    return img


# ── Output Channel Widget ─────────────────────────────────────────────────────
class OutputChannel(ctk.CTkFrame):
    def __init__(self, parent, label, dst_names, cfg=None, optional=False, save_fn=None, on_enable_cb=None, restart_cb=None, **kw):
        super().__init__(parent, fg_color=CARD2, corner_radius=6, **kw)
        cfg = cfg or {}
        self.optional = optional
        self._save_fn = save_fn
        self._on_enable_cb = on_enable_cb
        self._restart_cb = restart_cb
        self._expanded = ctk.BooleanVar(value=cfg.get("enabled", not optional))

        # ── Header ──
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(8, 6))
        ctk.CTkLabel(hdr, text=label, font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=SUBT).pack(side="left")
        self.enabled_var = ctk.BooleanVar(value=cfg.get("enabled", not optional))
        if optional:
            def _on_enable_and_save():
                self._on_enable()
                self._save()
                if self._on_enable_cb:
                    self._on_enable_cb()
                if self._restart_cb:
                    self._restart_cb()
            self._enable_cb = ctk.CTkCheckBox(hdr, text="Enable", variable=self.enabled_var,
                             text_color=SUBT, font=ctk.CTkFont(size=11),
                             fg_color=ACCENT, hover_color=ACCH, width=20,
                             command=_on_enable_and_save)
            self._enable_cb.pack(side="right")
        else:
            self._enable_cb = None

        # ── Collapsible body ──
        self._body = ctk.CTkFrame(self, fg_color="transparent")

        # Device combo
        self.combo = ctk.CTkComboBox(self._body, values=dst_names or ["No devices found"],
                                      fg_color=CARD3, border_color=BORDER,
                                      text_color=TEXT, button_color=BORDER,
                                      button_hover_color=ACCENT,
                                      dropdown_fg_color=CARD2, dropdown_text_color=TEXT,
                                      font=ctk.CTkFont(size=12))
        self.combo.pack(fill="x", padx=0, pady=(0, 8))
        saved = cfg.get("device")
        if saved and saved in dst_names:
            self.combo.set(saved)
        elif dst_names:
            self.combo.set(dst_names[0])
        self.combo.configure(command=lambda _: self._save())

        # Controls grid
        grid = ctk.CTkFrame(self._body, fg_color="transparent")
        grid.pack(fill="x", pady=(0, 4))
        grid.columnconfigure(0, weight=0)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(2, weight=1)

        self.reverse_var = ctk.BooleanVar(value=cfg.get("reverse", False))
        self.reverse_var.trace_add("write", self._save)
        ctk.CTkCheckBox(grid, text="Reverse L/R", variable=self.reverse_var,
                         text_color=TEXT, fg_color=ACCENT, hover_color=ACCH,
                         font=ctk.CTkFont(size=11)).grid(row=0, column=0, rowspan=2,
                                                          sticky="w", padx=(0, 12))

        vh = ctk.CTkFrame(grid, fg_color="transparent")
        vh.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkLabel(vh, text="Volume", font=ctk.CTkFont(size=10), text_color=SUBT).pack(side="left")
        self._vol_lbl = ctk.CTkLabel(vh, text=f"{int(cfg.get('volume', 1.0)*100)}%",
                                      font=ctk.CTkFont(size=10), text_color=TEXT)
        self._vol_lbl.pack(side="right")
        self.vol_slider = ctk.CTkSlider(grid, from_=0, to=150, number_of_steps=150,
                                         fg_color=CARD3, progress_color=ACCENT,
                                         button_color=ACCENT, button_hover_color=ACCH,
                                         command=self._on_vol)
        self.vol_slider.set(cfg.get("volume", 1.0) * 100)
        self.vol_slider.grid(row=1, column=1, sticky="ew", padx=(0, 8))

        lh = ctk.CTkFrame(grid, fg_color="transparent")
        lh.grid(row=0, column=2, sticky="ew")
        ctk.CTkLabel(lh, text="Latency", font=ctk.CTkFont(size=10), text_color=SUBT).pack(side="left")
        self._lat_lbl = ctk.CTkLabel(lh, text=f"{cfg.get('latency', 0)} ms",
                                      font=ctk.CTkFont(size=10), text_color=TEXT)
        self._lat_lbl.pack(side="right")
        self.lat_slider = ctk.CTkSlider(grid, from_=0, to=500, number_of_steps=100,
                                         fg_color=CARD3, progress_color=ACCENT,
                                         button_color=ACCENT, button_hover_color=ACCH,
                                         command=self._on_lat)
        self.lat_slider.set(cfg.get("latency", 0))
        self.lat_slider.grid(row=1, column=2, sticky="ew")

        self._all_controls = [self.combo, self.vol_slider, self.lat_slider]
        self._on_enable()

    def _save(self, *_):
        if self._save_fn:
            self._save_fn()

    def _on_vol(self, v):
        self._vol_lbl.configure(text=f"{int(v)}%")
        self._save()

    def _on_lat(self, v):
        self._lat_lbl.configure(text=f"{int(v)} ms")
        self._save()

    def _on_enable(self):
        if self.enabled_var.get():
            self._body.pack(fill="x", padx=12, pady=(0, 10))
        else:
            self._body.pack_forget()

    def is_active(self):
        return self.enabled_var.get()

    def get_config(self):
        return {
            "enabled": self.enabled_var.get(),
            "device":  self.combo.get(),
            "reverse": self.reverse_var.get(),
            "volume":  round(self.vol_slider.get() / 100, 3),
            "latency": int(self.lat_slider.get()),
        }

    def get_device(self):
        return self.combo.get()

    def set_dst_names(self, names):
        """Set the full available device list (called on refresh)."""
        cur = self.combo.get()
        self.combo.configure(values=names or ["No devices found"])
        if cur in names:
            self.combo.set(cur)
        elif names:
            self.combo.set(names[0])

    def set_available_names(self, names):
        """Update combo values without changing selection (used for dedup filtering)."""
        cur = self.combo.get()
        available = names if names else ["No devices found"]
        self.combo.configure(values=available)
        # Keep current selection even if it's been removed from the filtered list
        # (it's still 'taken' by this channel)
        self.combo.set(cur)


# ── Main App ──────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AudioMirror")
        self.geometry("860x648")
        self.resizable(False, False)
        self.configure(fg_color=BG)

        self.engine = AudioEngine()
        self.loopback_devices = []
        self.output_devices   = []
        self._tray = None
        self._tray_thread = None

        self._build_ui()
        self._refresh(silent=True)
        self.after(100, self._apply_height)
        self.after(100, self._update_optional_states)
        self._try_autostart()

        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.bind("<Unmap>", self._on_unmap)
        self._start_tray()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _card(self, parent, **kw):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=6, **kw)

    def _lbl(self, parent, text, size=11, color=SUBT, **kw):
        return ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=size),
                            text_color=color, **kw)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        cfg = load_config()

        # ── Two-column root ──
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=0, pady=0)
        root.columnconfigure(0, weight=0, minsize=300)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ── LEFT COLUMN ──
        left = ctk.CTkFrame(root, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(16, 6), pady=16)

        # Header
        hdr = ctk.CTkFrame(left, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 10))
        self._lbl(hdr, "AudioMirror", size=18, color=TEXT).pack(side="left")
        ctk.CTkButton(hdr, text="↺", width=32, height=28,
                      fg_color=CARD2, hover_color=BORDER, text_color=TEXT,
                      font=ctk.CTkFont(size=13), corner_radius=5,
                      command=self._refresh).pack(side="right")

        self._hotkey_str = load_config().get("hotkey", "f8")
        self._hotkey_handle = None
        self._listening_hotkey = False

        # Source
        sc = self._card(left)
        sc.pack(fill="x", pady=(0, 8))
        self._lbl(sc, "SOURCE", color=SUBT).pack(anchor="w", padx=12, pady=(10, 2))
        self.src_combo = ctk.CTkComboBox(sc, values=[], fg_color=CARD2,
                                          border_color=BORDER, text_color=TEXT,
                                          button_color=BORDER, button_hover_color=ACCENT,
                                          dropdown_fg_color=CARD2, dropdown_text_color=TEXT,
                                          font=ctk.CTkFont(size=11),
                                          command=lambda _: self._autosave())
        self.src_combo.pack(fill="x", padx=12, pady=(0, 12))

        # Shortcut bar
        shc = self._card(left)
        shc.pack(fill="x", pady=(0, 8))
        shc_row = ctk.CTkFrame(shc, fg_color="transparent")
        shc_row.pack(fill="x", padx=12, pady=(8, 4))
        ctk.CTkLabel(shc_row, text="REVERSE ALL",
                     font=ctk.CTkFont(size=10), text_color=SUBT).pack(side="left")
        self.hotkey_btn = ctk.CTkButton(shc_row, text=self._hotkey_str.upper(),
                                         width=80, height=26,
                                         fg_color=CARD2, hover_color=BORDER,
                                         border_width=1, border_color=BORDER,
                                         text_color=TEXT, font=ctk.CTkFont(size=11),
                                         corner_radius=4,
                                         command=self._start_listen_hotkey)
        self.hotkey_btn.pack(side="right")
        beep_row = ctk.CTkFrame(shc, fg_color="transparent")
        beep_row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(beep_row, text="Beep", font=ctk.CTkFont(size=10),
                     text_color=SUBT).pack(side="left")
        saved_beep = load_config().get("beep_volume", 0.5)
        self._beep_vol_lbl = ctk.CTkLabel(beep_row, text=f"{int(saved_beep*100)}%",
                                           font=ctk.CTkFont(size=10), text_color=TEXT)
        self._beep_vol_lbl.pack(side="right")
        self._beep_volume = saved_beep
        self._beep_slider = ctk.CTkSlider(beep_row, from_=0, to=100,
                                           number_of_steps=100,
                                           fg_color=CARD2, progress_color=ACCENT,
                                           button_color=ACCENT, button_hover_color=ACCH,
                                           command=self._on_beep_vol)
        self._beep_slider.set(saved_beep * 100)
        self._beep_slider.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._register_hotkey()

        # Status
        stc = self._card(left)
        stc.pack(fill="x", pady=(0, 8))
        sr = ctk.CTkFrame(stc, fg_color="transparent")
        sr.pack(fill="x", padx=12, pady=10)
        self.status_dot = self._lbl(sr, "●", size=14, color=RED)
        self.status_dot.pack(side="left")
        self.status_lbl = self._lbl(sr, "Stopped", size=12, color=SUBT)
        self.status_lbl.pack(side="left", padx=(8, 0))

        # Visualizer
        viz_frame = self._card(left)
        viz_frame.pack(fill="x", pady=(0, 8))
        viz_header = ctk.CTkFrame(viz_frame, fg_color="transparent")
        viz_header.pack(fill="x", padx=12, pady=(6, 0))
        self._lbl(viz_header, "SOURCE MONITOR", color=SUBT).pack(side="left")
        self._viz_active_lbl = self._lbl(viz_header, "—", size=10, color=SUBT)
        self._viz_active_lbl.pack(side="right")

        import tkinter as tk
        self._viz_canvas = tk.Canvas(viz_frame, height=84, bg=CARD2,
                                     highlightthickness=0, bd=0)
        self._viz_canvas.pack(fill="x", padx=6, pady=(4, 8))
        self._viz_bands = 20
        self._viz_levels = [0.0] * self._viz_bands
        self._viz_peaks  = [0.0] * self._viz_bands
        self._viz_canvas.bind("<Configure>", lambda e: self._viz_draw())
        self.after(50, self._viz_tick)

        # Log
        lf = self._card(left)
        lf.pack(fill="x", pady=(0, 8))
        self.log_box = ctk.CTkTextbox(lf, height=144, fg_color=CARD2, text_color="#606060",
                                       font=ctk.CTkFont(family="Consolas", size=10),
                                       border_width=0)
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_box.configure(state="disabled")

        # Buttons
        br = self._card(left)
        br.pack(fill="x", pady=(0, 0))
        btn_inner = ctk.CTkFrame(br, fg_color="transparent")
        btn_inner.pack(fill="x", padx=10, pady=10)
        self.start_btn = ctk.CTkButton(btn_inner, text="▶  Start", height=34,
                                        fg_color=ACCENT, hover_color=ACCH,
                                        text_color="white",
                                        border_width=1, border_color=ACCENT,
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        corner_radius=5, command=self._start)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.stop_btn = ctk.CTkButton(btn_inner, text="■  Stop", height=34,
                                       fg_color=CARD2, hover_color=CARD3,
                                       text_color=SUBT,
                                       border_width=1, border_color=BORDER,
                                       font=ctk.CTkFont(size=12, weight="bold"),
                                       corner_radius=5, state="disabled",
                                       command=self._stop)
        self.stop_btn.pack(side="right", fill="x", expand=True, padx=(5, 0))

        # ── RIGHT COLUMN — Outputs ──
        right = ctk.CTkFrame(root, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 16), pady=16)

        oc = self._card(right)
        oc.pack(fill="both", expand=True)
        self._lbl(oc, "OUTPUTS", color=SUBT).pack(anchor="w", padx=14, pady=(10, 6))

        self.out1 = OutputChannel(oc, "Output 1", [],
                                   cfg=cfg.get("out1"), optional=False,
                                   save_fn=self._autosave)
        self.out1.pack(fill="x", padx=10, pady=(0, 6))
        self.out1.combo.configure(command=lambda _: (self._autosave(), self._update_dedup()))

        self.out2 = OutputChannel(oc, "Output 2  (optional)", [],
                                   cfg=cfg.get("out2"), optional=True,
                                   save_fn=self._autosave,
                                   on_enable_cb=self._on_output_enable,
                                   restart_cb=self._hot_restart)
        self.out2.pack(fill="x", padx=10, pady=(0, 6))
        self.out2.combo.configure(command=lambda _: (self._autosave(), self._update_dedup()))

        self.out3 = OutputChannel(oc, "Output 3  (optional)", [],
                                   cfg=cfg.get("out3"), optional=True,
                                   save_fn=self._autosave,
                                   on_enable_cb=self._on_output_enable,
                                   restart_cb=self._hot_restart)
        self.out3.pack(fill="x", padx=10, pady=(0, 6))
        self.out3.combo.configure(command=lambda _: (self._autosave(), self._update_dedup()))

        self.out4 = OutputChannel(oc, "Output 4  (optional)", [],
                                   cfg=cfg.get("out4"), optional=True,
                                   save_fn=self._autosave,
                                   on_enable_cb=self._on_output_enable,
                                   restart_cb=self._hot_restart)
        self.out4.pack(fill="x", padx=10, pady=(0, 10))
        self.out4.combo.configure(command=lambda _: (self._autosave(), self._update_dedup()))

        # Hook source combo for dedup
        self.src_combo.configure(command=lambda _: (self._autosave(), self._update_dedup()))

    # ── Output enable logic ───────────────────────────────────────────────────
    HEIGHT_MAP = {1: 648, 2: 648, 3: 648, 4: 648}  # horizontal layout, height fixed

    def _all_channels(self):
        return [self.out1, self.out2, self.out3, self.out4]

    def _apply_height(self):
        active = sum(1 for ch in self._all_channels() if ch.is_active())
        h = self.HEIGHT_MAP.get(active, 648)
        self.geometry(f"860x{h}")

    def _on_output_enable(self):
        # Enforce chain: out3 requires out2, out4 requires out3
        if self.out3.is_active() and not self.out2.is_active():
            self.out3.enabled_var.set(False)
            self.out3._on_enable()
        if self.out4.is_active() and not self.out3.is_active():
            self.out4.enabled_var.set(False)
            self.out4._on_enable()
        self._apply_height()
        self._update_optional_states()
        self._update_dedup()

    def _update_optional_states(self):
        """Enable/disable optional checkboxes based on chain dependency."""
        # out2: always available
        if hasattr(self, "out2") and hasattr(self.out2, "_enable_cb") and self.out2._enable_cb:
            self.out2._enable_cb.configure(state="normal")
        # out3: requires out2
        if hasattr(self, "out3") and hasattr(self.out3, "_enable_cb") and self.out3._enable_cb:
            self.out3._enable_cb.configure(
                state="normal" if self.out2.is_active() else "disabled")
        # out4: requires out3
        if hasattr(self, "out4") and hasattr(self.out4, "_enable_cb") and self.out4._enable_cb:
            self.out4._enable_cb.configure(
                state="normal" if self.out3.is_active() else "disabled")

    def _update_dedup(self):
        """Filter combo lists so no device can be selected in two places at once.
        Source loopback is excluded from output lists; selected outputs are
        excluded from each other's dropdowns (but each channel still shows
        its own current selection)."""
        if not hasattr(self, "out1"):
            return
        all_dst = [d["name"] for d in self.output_devices]
        # Source loopback names that overlap with output names should be excluded
        src_name = self.src_combo.get() if hasattr(self, "src_combo") else ""

        channels = self._all_channels()
        for i, ch in enumerate(channels):
            if not ch.is_active():
                continue
            # Collect devices taken by other active channels
            taken = set()
            for j, other in enumerate(channels):
                if j != i and other.is_active():
                    taken.add(other.get_device())
            # Available = all - taken (own selection is always kept via set_available_names)
            available = [d for d in all_dst if d not in taken]
            ch.set_available_names(available)

    # ── Hotkey / Reverse All ──────────────────────────────────────────────────
    def _on_beep_vol(self, v):
        self._beep_volume = v / 100
        self._beep_vol_lbl.configure(text=f"{int(v)}%")
        self._autosave()

    def _reverse_all(self):
        # Toggle each active channel independently
        active = [ch for ch in self._all_channels() if ch.is_active()]
        if not active:
            return
        for ch in active:
            ch.reverse_var.set(not ch.reverse_var.get())
        # Beep based on out1's new state
        if self._beep_volume > 0:
            new_state = active[0].reverse_var.get()
            if new_state:
                play_beep(1200, 70, self._beep_volume)
            else:
                play_beep(300, 100, self._beep_volume)
        self._autosave()

    def _register_hotkey(self):
        if self._hotkey_handle:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
        try:
            self._hotkey_handle = keyboard.add_hotkey(self._hotkey_str, self._reverse_all)
        except Exception:
            pass

    def _start_listen_hotkey(self):
        self.hotkey_btn.configure(text="Press key...", fg_color=CARD3)
        self._listening_hotkey = True
        self.bind("<KeyPress>", self._capture_hotkey)
        self.focus_set()

    def _capture_hotkey(self, event):
        if not self._listening_hotkey:
            return
        ignore = {"shift_l","shift_r","control_l","control_r",
                  "alt_l","alt_r","super_l","super_r","caps_lock","alt"}
        key = event.keysym.lower()
        if key in ignore:
            return
        # Only use modifier if physically held (state bits), exclude Alt entirely
        parts = []
        if event.state & 0x4: parts.append("ctrl")
        if event.state & 0x1: parts.append("shift")
        # Do NOT capture Alt — it causes OS-level conflicts
        parts.append(key)
        self._hotkey_str = "+".join(parts)
        self.hotkey_btn.configure(text=self._hotkey_str.upper(), fg_color=CARD2)
        self._listening_hotkey = False
        self.unbind("<KeyPress>")
        self._register_hotkey()
        self._autosave()

    # ── Hot restart ───────────────────────────────────────────────────────────
    def _hot_restart(self):
        """Silently restart engine if running, to apply enable/disable changes."""
        if not self.engine.running:
            return
        # Remember source
        src_idx, src_info = self._find_loopback(self.src_combo.get())
        if src_idx is None:
            return
        sink_configs = self._build_sink_configs()
        # Stop engine quietly
        self.engine.stop()
        if not sink_configs:
            self._set_ui(False)
            self._log("All outputs disabled — stopped.")
            self._update_tray()
            return
        # Restart
        try:
            self.engine.start(src_idx, src_info, sink_configs)
        except Exception as e:
            self._set_ui(False)
            self._log(f"Restart error: {e}")
            return
        # Re-wire live controls
        active_channels = [ch for ch in self._all_channels() if ch.is_active()]
        for ch, sink in zip(active_channels, self.engine.sinks):
            ch.reverse_var.trace_add("write",
                lambda *_, s=sink, v=ch.reverse_var: setattr(s, "reverse", v.get()))
            ch.vol_slider.configure(
                command=lambda val, s=sink, ch=ch: (
                    setattr(s, "volume", val / 100),
                    ch._vol_lbl.configure(text=f"{int(val)}%"),
                    ch._save()))
            ch.lat_slider.configure(
                command=lambda val, s=sink, ch=ch: (
                    setattr(s, "delay_ms", int(val)),
                    ch._lat_lbl.configure(text=f"{int(val)} ms"),
                    ch._save()))
        self._log(f"Restarted → {len(sink_configs)} output(s) active")
        self.engine.viz_callback = self._on_viz_data

    # ── Autosave ──────────────────────────────────────────────────────────────
    def _autosave(self, *_):
        save_config({
            "src":         self.src_combo.get(),
            "out1":        self.out1.get_config(),
            "out2":        self.out2.get_config(),
            "out3":        self.out3.get_config(),
            "out4":        self.out4.get_config(),
            "hotkey":      self._hotkey_str,
            "beep_volume": getattr(self, "_beep_volume", 0.5),
        })

    # ── Devices ───────────────────────────────────────────────────────────────
    def _refresh(self, silent=False):
        if self.engine.running:
            messagebox.showwarning("AudioMirror", "Stop mirroring first.")
            return
        self.loopback_devices = self.engine.get_loopback_devices()
        self.output_devices   = self.engine.get_output_devices()
        src_names = [d["name"] for d in self.loopback_devices]
        dst_names = [d["name"] for d in self.output_devices]

        self.src_combo.configure(values=src_names or ["No devices found"])
        cfg = load_config()
        saved_src = cfg.get("src")
        if saved_src and saved_src in src_names:
            self.src_combo.set(saved_src)
        elif src_names:
            self.src_combo.set(src_names[0])
        # restore saved output device selections
        for key, ch in [("out1", self.out1), ("out2", self.out2), ("out3", self.out3), ("out4", self.out4)]:
            saved_dev = cfg.get(key, {}).get("device")
            if saved_dev and saved_dev in dst_names:
                ch.combo.set(saved_dev)

        for ch in self._all_channels():
            ch.set_dst_names(dst_names)

        self._update_dedup()

        if not silent:
            self._log(f"Found {len(self.loopback_devices)} sources, {len(self.output_devices)} outputs.")

    def _find_loopback(self, name):
        for d in self.loopback_devices:
            if d["name"] == name:
                return d["index"], d["info"]
        return None, None

    def _find_output(self, name):
        for d in self.output_devices:
            if d["name"] == name:
                return d["index"], d["info"]
        return None, None

    # ── Start / Stop ──────────────────────────────────────────────────────────
    def _try_autostart(self):
        cfg = load_config()
        if not cfg.get("src"):
            return
        src_names = [d["name"] for d in self.loopback_devices]
        if cfg["src"] not in src_names:
            return
        self._log("Saved devices found — starting automatically...")
        self.after(300, self._start)

    def _build_sink_configs(self):
        configs = []
        for ch in self._all_channels():
            if not ch.is_active():
                continue
            c = ch.get_config()
            dst_idx, dst_info = self._find_output(c["device"])
            if dst_idx is None:
                continue
            configs.append({
                "dst_idx":  dst_idx,
                "dst_info": dst_info,
                "reverse":  c["reverse"],
                "delay_ms": c["latency"],
                "volume":   c["volume"],
            })
        return configs

    def _start(self):
        src_idx, src_info = self._find_loopback(self.src_combo.get())
        if src_idx is None:
            messagebox.showwarning("AudioMirror", "Source device not found.\nTry refreshing.")
            return
        sink_configs = self._build_sink_configs()
        if not sink_configs:
            messagebox.showwarning("AudioMirror", "No output devices configured.")
            return
        try:
            self.engine.start(src_idx, src_info, sink_configs)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        self._autosave()

        # Wire visualizer
        self.engine.viz_callback = self._on_viz_data

        # Wire live controls
        active_channels = [ch for ch in self._all_channels() if ch.is_active()]
        for ch, sink in zip(active_channels, self.engine.sinks):
            ch.reverse_var.trace_add("write",
                lambda *_, s=sink, v=ch.reverse_var: setattr(s, "reverse", v.get()))
            ch.vol_slider.configure(
                command=lambda val, s=sink, ch=ch: (
                    setattr(s, "volume", val / 100),
                    ch._vol_lbl.configure(text=f"{int(val)}%"),
                    ch._save()))
            ch.lat_slider.configure(
                command=lambda val, s=sink, ch=ch: (
                    setattr(s, "delay_ms", int(val)),
                    ch._lat_lbl.configure(text=f"{int(val)} ms"),
                    ch._save()))

        self._set_ui(True)
        src_rate = int(src_info.get("defaultSampleRate", 0))
        self._log(f"Started  {self.src_combo.get()}  →  {len(sink_configs)} output(s)")
        self._log(f"  SRC rate: {src_rate} Hz")
        for i, (cfg, ch) in enumerate(zip(sink_configs, active_channels)):
            dst_rate = int(cfg["dst_info"].get("defaultSampleRate", 0))
            match = "✓" if dst_rate == src_rate else "⚠ MISMATCH"
            self._log(f"  Out{i+1}: {ch.combo.get()} | "
                      f"rev={'on' if cfg['reverse'] else 'off'} | "
                      f"vol={int(cfg['volume']*100)}% | delay={cfg['delay_ms']}ms | "
                      f"rate={dst_rate} Hz {match}")
        self._update_tray()

    def _stop(self):
        self.engine.stop()
        self.engine.viz_callback = None
        self._set_ui(False)
        self._log("Stopped.")
        self._update_tray()

    def _set_ui(self, running):
        self.start_btn.configure(
            state="disabled" if running else "normal",
            fg_color=CARD2 if running else ACCENT,
            hover_color=CARD3 if running else ACCH,
            text_color=SUBT if running else "white",
            border_color=BORDER if running else ACCENT)
        self.stop_btn.configure(
            state="normal" if running else "disabled",
            fg_color=RED if running else CARD2,
            hover_color=REDH if running else CARD3,
            text_color="white" if running else SUBT,
            border_color=RED if running else BORDER)
        self.src_combo.configure(state="disabled" if running else "normal")
        for ch in self._all_channels():
            ch.combo.configure(state="disabled" if running else "normal")
        self.status_dot.configure(text_color=GREEN if running else RED)
        self.status_lbl.configure(text="Running" if running else "Stopped")

    # ── Visualizer ────────────────────────────────────────────────────────────
    def _on_viz_data(self, bands: list):
        """Called from audio thread — just store latest levels."""
        peak = max(bands) if bands else 1e-9
        norm = [b / peak for b in bands] if peak > 1e-6 else [0.0] * len(bands)
        self._viz_levels = norm

    def _viz_tick(self):
        """UI thread: smooth & draw every 50 ms."""
        alpha = 0.35
        for i in range(self._viz_bands):
            target = self._viz_levels[i]
            self._viz_peaks[i] = max(self._viz_peaks[i] * 0.92, target)
            self._viz_levels[i] = self._viz_levels[i] * (1 - alpha) + target * alpha
        self._viz_draw()
        self.after(50, self._viz_tick)

    def _viz_draw(self):
        c = self._viz_canvas
        c.delete("all")
        try:
            w = c.winfo_width()
            h = c.winfo_height()
        except Exception:
            return
        if w < 4 or h < 4:
            return
        n = self._viz_bands
        gap = 2
        bar_w = max(1, (w - gap * (n + 1)) / n)
        running = self.engine.running
        for i in range(n):
            x0 = gap + i * (bar_w + gap)
            x1 = x0 + bar_w
            lvl = self._viz_levels[i] if running else 0.0
            bar_h = max(2, lvl * (h - 4))
            y0 = h - 2 - bar_h
            y1 = h - 2
            # Gradient colour: blue → cyan based on level
            r = int(74  + (0   - 74)  * lvl)
            g = int(144 + (210 - 144) * lvl)
            b = int(217 + (255 - 217) * lvl)
            color = f"#{r:02x}{g:02x}{b:02x}"
            c.create_rectangle(x0, y0, x1, y1, fill=color, outline="")
            # Peak dot
            pk = self._viz_peaks[i] if running else 0.0
            if pk > 0.05:
                py = h - 2 - pk * (h - 4)
                c.create_rectangle(x0, py - 1, x1, py + 1, fill="#ffffff", outline="")
        # Label
        if not running:
            c.create_text(w // 2, h // 2, text="not running",
                          fill="#444444", font=("Consolas", 9))
        # Update active label
        src = self.src_combo.get() if running else ""
        self._viz_active_lbl.configure(text=src[:28] if src else "—")

    # ── Log ───────────────────────────────────────────────────────────────────
    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.after(0, lambda m=f"[{ts}] {msg}\n": self._log_write(m))

    def _log_write(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ── Tray ──────────────────────────────────────────────────────────────────
    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show",           self._show_window, default=True),
            pystray.MenuItem("Stop mirroring", self._stop_from_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",           self._quit),
        )
        self._tray = pystray.Icon("AudioMirror", make_tray_image(),
                                   "AudioMirror — Stopped", menu)
        self._tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        self._tray_thread.start()
        self._minimize_to_tray()

    def _show_window(self, *_):
        self.after(0, self._do_show)

    def _do_show(self):
        self.deiconify(); self.lift(); self.focus_force()

    def _minimize_to_tray(self, *_):
        self.withdraw()

    def _on_unmap(self, event):
        if self.state() == "iconic":
            self._minimize_to_tray()

    def _stop_from_tray(self, *_):
        if self.engine.running:
            self.after(0, self._stop)

    def _update_tray(self):
        if self._tray:
            self._tray.title = f"AudioMirror — {'Running' if self.engine.running else 'Stopped'}"

    def _quit(self, *_):
        self.engine.stop()
        if self._hotkey_handle:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)


if __name__ == "__main__":
    app = App()
    app.mainloop()