import argparse
import ctypes
import glob
import os
import queue
import shutil
import sys
import tempfile
import threading


def _ensure_ffmpeg_on_path() -> None:
    if shutil.which("ffmpeg"):
        return
    patterns = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                     "Packages", "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            os.environ["PATH"] = os.path.dirname(matches[0]) + os.pathsep + os.environ.get("PATH", "")
            return

_ensure_ffmpeg_on_path()

import ffmpeg
import whisper


# ── Core transcription logic ──────────────────────────────────────────────────

def extract_audio(video_path: str, audio_path: str) -> None:
    (
        ffmpeg
        .input(video_path)
        .output(audio_path, ac=1, ar=16000)
        .overwrite_output()
        .run(quiet=True)
    )


def build_srt(segments: list) -> str:
    blocks = []
    for i, segment in enumerate(segments, start=1):
        start = format_timestamp(segment["start"], srt=True)
        end = format_timestamp(segment["end"], srt=True)
        blocks.append(f"{i}\n{start} --> {end}\n{segment['text'].strip()}")
    return "\n\n".join(blocks)


def format_timestamp(seconds: float, srt: bool = False) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if srt:
        return f"{h:02d}:{m:02d}:{int(s):02d},{int(s % 1 * 1000):03d}"
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def transcribe(
    video_path: str,
    model_size: str = "base",
    output_path: str | None = None,
    language: str | None = None,
    srt: bool = False,
    log=print,
) -> str:
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    log(f"Loading Whisper model '{model_size}'…")
    model = whisper.load_model(model_size)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name

    try:
        log("Extracting audio…")
        extract_audio(video_path, audio_path)

        kwargs = {"verbose": False}
        if language:
            kwargs["language"] = language

        log("Transcribing — this may take a while…")
        result = model.transcribe(audio_path, **kwargs)
    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)

    if srt:
        transcript = build_srt(result["segments"])
        ext = ".srt"
    else:
        lines = []
        for seg in result["segments"]:
            start = format_timestamp(seg["start"])
            end = format_timestamp(seg["end"])
            lines.append(f"[{start} --> {end}] {seg['text'].strip()}")
        transcript = "\n".join(lines)
        ext = ".txt"

    if output_path is None:
        base, _ = os.path.splitext(video_path)
        output_path = base + ext

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(transcript)

    log(f"Saved → {output_path}")
    return transcript


# ── GUI ───────────────────────────────────────────────────────────────────────

def _raise_in_thread(tid: int, exc_type: type) -> None:
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid),
        ctypes.py_object(exc_type),
    )


def run_gui() -> None:
    import customtkinter as ctk
    from tkinter import filedialog

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    app = ctk.CTk()
    app.title("Video Transcribe")
    app.geometry("720x680")
    app.minsize(620, 580)

    ACCENT = "#3B82F6"
    BG = "#0F172A"
    CARD = "#1E293B"
    MUTED = "#94A3B8"

    app.configure(fg_color=BG)

    # ── Title ──
    title_frame = ctk.CTkFrame(app, fg_color="transparent")
    title_frame.pack(fill="x", padx=32, pady=(28, 0))

    ctk.CTkLabel(
        title_frame,
        text="Video Transcribe",
        font=ctk.CTkFont(size=22, weight="bold"),
        text_color="white",
    ).pack(side="left")

    ctk.CTkLabel(
        title_frame,
        text="powered by Whisper",
        font=ctk.CTkFont(size=12),
        text_color=MUTED,
    ).pack(side="left", padx=(10, 0), pady=(6, 0))

    # ── Card / row helpers ──
    def card(parent, pady=(12, 0)):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        f.pack(fill="x", padx=32, pady=pady)
        return f

    def row(parent):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=16, pady=10)
        return f

    def label(parent, text, muted=False):
        ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=12, weight="bold" if not muted else "normal"),
            text_color=MUTED if muted else "white",
            anchor="w",
        ).pack(side="left")

    # ── Video file ──
    video_card = card(app, pady=(24, 0))
    r1 = row(video_card)
    label(r1, "Video File")

    video_path_var = ctk.StringVar(value="")
    ctk.CTkEntry(
        r1,
        textvariable=video_path_var,
        placeholder_text="No file selected…",
        fg_color="#0F172A",
        border_color="#334155",
        text_color="white",
        height=34,
    ).pack(side="left", fill="x", expand=True, padx=(12, 8))

    def browse_video():
        path = filedialog.askopenfilename(
            title="Select video file",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.webm *.flv *.wmv *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if path:
            video_path_var.set(path)

    ctk.CTkButton(r1, text="Browse", width=80, height=34,
                  fg_color=ACCENT, hover_color="#2563EB",
                  command=browse_video).pack(side="left")

    # ── Output folder ──
    out_card = card(app)
    r2 = row(out_card)
    label(r2, "Output Folder")

    out_dir_var = ctk.StringVar(value="")
    ctk.CTkEntry(
        r2,
        textvariable=out_dir_var,
        placeholder_text="Same folder as video (default)…",
        fg_color="#0F172A",
        border_color="#334155",
        text_color="white",
        height=34,
    ).pack(side="left", fill="x", expand=True, padx=(12, 8))

    def browse_output():
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            out_dir_var.set(path)

    ctk.CTkButton(r2, text="Browse", width=80, height=34,
                  fg_color="#334155", hover_color="#475569",
                  command=browse_output).pack(side="left")

    # ── Settings ──
    settings_card = card(app)
    r3 = row(settings_card)

    label(r3, "Model")
    model_var = ctk.StringVar(value="base")
    ctk.CTkOptionMenu(
        r3, variable=model_var,
        values=["tiny", "base", "small", "medium", "large"],
        fg_color="#0F172A", button_color="#334155",
        button_hover_color="#475569", dropdown_fg_color="#1E293B",
        text_color="white", width=110, height=34,
    ).pack(side="left", padx=(8, 24))

    label(r3, "Language")
    lang_entry = ctk.CTkEntry(
        r3, placeholder_text="auto",
        fg_color="#0F172A", border_color="#334155",
        text_color="white", width=90, height=34,
    )
    lang_entry.pack(side="left", padx=(8, 24))

    label(r3, "SRT output")
    srt_var = ctk.BooleanVar(value=False)
    ctk.CTkSwitch(r3, text="", variable=srt_var,
                  onvalue=True, offvalue=False,
                  progress_color=ACCENT, width=46, height=24,
                  ).pack(side="left", padx=(8, 0))

    # ── Buttons ──
    btn_frame = ctk.CTkFrame(app, fg_color="transparent")
    btn_frame.pack(fill="x", padx=32, pady=(16, 0))

    msg_queue: queue.Queue = queue.Queue()
    is_running = threading.Event()
    worker_thread: list[threading.Thread | None] = [None]

    def run_transcription():
        video = video_path_var.get().strip()
        out_dir = out_dir_var.get().strip()
        model_size = model_var.get()
        language = lang_entry.get().strip() or None
        use_srt = srt_var.get()

        if not video:
            msg_queue.put(("error", "Please select a video file."))
            return

        ext = ".srt" if use_srt else ".txt"
        output_path = (
            os.path.join(out_dir, os.path.splitext(os.path.basename(video))[0] + ext)
            if out_dir else None
        )

        def log(msg):
            msg_queue.put(("log", msg))

        try:
            result = transcribe(
                video, model_size=model_size, output_path=output_path,
                language=language, srt=use_srt, log=log,
            )
            msg_queue.put(("done", result))
        except SystemExit:
            msg_queue.put(("cancelled", None))
        except Exception as exc:
            msg_queue.put(("error", str(exc)))

    def start():
        if is_running.is_set():
            return
        is_running.set()
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.configure(state="disabled")
        preview_box.configure(state="normal")
        preview_box.delete("1.0", "end")
        preview_box.configure(state="disabled")
        tabs.set("Log")
        transcribe_btn.configure(state="disabled", text="Transcribing…")
        cancel_btn.configure(state="normal")
        progress.start()
        t = threading.Thread(target=run_transcription, daemon=True)
        worker_thread[0] = t
        t.start()

    def cancel():
        t = worker_thread[0]
        if t and t.is_alive():
            _raise_in_thread(t.ident, SystemExit)
        cancel_btn.configure(state="disabled")

    transcribe_btn = ctk.CTkButton(
        btn_frame, text="Transcribe", height=44,
        font=ctk.CTkFont(size=14, weight="bold"),
        fg_color=ACCENT, hover_color="#2563EB",
        corner_radius=10, command=start,
    )
    transcribe_btn.pack(side="left", fill="x", expand=True)

    cancel_btn = ctk.CTkButton(
        btn_frame, text="Cancel", height=44, width=110,
        font=ctk.CTkFont(size=14, weight="bold"),
        fg_color="#334155", hover_color="#DC2626",
        corner_radius=10, state="disabled", command=cancel,
    )
    cancel_btn.pack(side="left", padx=(10, 0))

    # ── Progress bar ──
    progress = ctk.CTkProgressBar(app, mode="indeterminate", progress_color=ACCENT, height=4)
    progress.pack(fill="x", padx=32, pady=(8, 0))
    progress.set(0)

    # ── Tabs: Log + Preview ──
    tabs = ctk.CTkTabview(
        app, fg_color=CARD, corner_radius=12,
        segmented_button_fg_color="#0F172A",
        segmented_button_selected_color=ACCENT,
        segmented_button_selected_hover_color="#2563EB",
        segmented_button_unselected_color="#0F172A",
        segmented_button_unselected_hover_color="#334155",
        text_color="white",
    )
    tabs.pack(fill="both", expand=True, padx=32, pady=(8, 28))
    tabs.add("Log")
    tabs.add("Preview")

    log_box = ctk.CTkTextbox(
        tabs.tab("Log"),
        fg_color="transparent",
        text_color=MUTED,
        font=ctk.CTkFont(family="Consolas", size=12),
        state="disabled",
        wrap="word",
    )
    log_box.pack(fill="both", expand=True)

    preview_box = ctk.CTkTextbox(
        tabs.tab("Preview"),
        fg_color="transparent",
        text_color="white",
        font=ctk.CTkFont(family="Consolas", size=12),
        state="disabled",
        wrap="word",
    )
    preview_box.pack(fill="both", expand=True)

    def append_log(text: str):
        log_box.configure(state="normal")
        log_box.insert("end", text + "\n")
        log_box.configure(state="disabled")
        log_box.see("end")

    def reset_controls():
        progress.stop()
        transcribe_btn.configure(state="normal", text="Transcribe")
        cancel_btn.configure(state="disabled")
        is_running.clear()

    def poll_queue():
        try:
            while True:
                kind, payload = msg_queue.get_nowait()
                if kind == "log":
                    append_log(payload)
                elif kind == "done":
                    progress.set(1)
                    append_log("Done!")
                    preview_box.configure(state="normal")
                    preview_box.insert("1.0", payload)
                    preview_box.configure(state="disabled")
                    tabs.set("Preview")
                    reset_controls()
                elif kind == "cancelled":
                    progress.set(0)
                    append_log("Cancelled.")
                    reset_controls()
                elif kind == "error":
                    progress.set(0)
                    append_log(f"Error: {payload}")
                    reset_controls()
        except queue.Empty:
            pass
        app.after(100, poll_queue)

    app.after(100, poll_queue)
    app.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="Transcribe a video file using Whisper.")
        parser.add_argument("video", help="Path to the input video file")
        parser.add_argument(
            "--model",
            default="base",
            choices=["tiny", "base", "small", "medium", "large"],
        )
        parser.add_argument("--output", help="Output file path")
        parser.add_argument("--language", help="Language code (e.g. en, fr, ja)")
        parser.add_argument("--srt", action="store_true")
        args = parser.parse_args()
        transcribe(args.video, model_size=args.model, output_path=args.output,
                   language=args.language, srt=args.srt)
    else:
        run_gui()


if __name__ == "__main__":
    main()
