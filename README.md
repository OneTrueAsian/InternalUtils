# Util Scripts


# VideoTranscribe

A local video transcription tool powered by [OpenAI Whisper](https://github.com/openai/whisper). Drop in a video file, pick an output folder, and get a timestamped transcript — no internet required after setup.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![Whisper](https://img.shields.io/badge/OpenAI-Whisper-orange) ![UI](https://img.shields.io/badge/UI-CustomTkinter-purple)

---

## Features

- Modern dark UI built with CustomTkinter
- Supports all common video formats (`.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`, etc.)
- Five Whisper model sizes — trade speed for accuracy
- Optional language hint to skip auto-detection
- Output as plain `.txt` or subtitle `.srt` file
- Live transcript preview tab — read results without opening the file
- Cancel button to stop transcription mid-way
- Also works as a CLI tool

---

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) installed and on PATH

---

## Installation

**1. Install ffmpeg**

```powershell
winget install Gyan.FFmpeg
```

**2. Install Python dependencies**

```bash
pip install customtkinter openai-whisper ffmpeg-python
```

> First run will also download the Whisper model weights (~75 MB for `base`, up to ~3 GB for `large`).

---

## Usage

### GUI

Run without arguments to launch the interface:

```bash
python VideoTranscribe.py
```

1. Click **Browse** to select your video file
2. Optionally choose an **Output Folder** (defaults to the same folder as the video)
3. Pick a **Model** size and optionally enter a **Language** code
4. Toggle **SRT output** if you want a subtitle file instead of plain text
5. Click **Transcribe** — progress is shown in the Log tab
6. When done, the **Preview** tab opens automatically with the full transcript

### CLI

```bash
python VideoTranscribe.py <video_file> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | `base` | Model size: `tiny`, `base`, `small`, `medium`, `large` |
| `--output` | Same folder as video | Custom output file path |
| `--language` | auto | Language code (e.g. `en`, `fr`, `ja`) |
| `--srt` | off | Output `.srt` subtitle format instead of `.txt` |

**Examples:**

```bash
# Basic transcription
python VideoTranscribe.py myvideo.mp4

# High accuracy, English only, save as SRT
python VideoTranscribe.py myvideo.mp4 --model large --language en --srt

# Custom output path
python VideoTranscribe.py myvideo.mp4 --output C:\Transcripts\output.txt
```

---

## Output Formats

**Plain text (`.txt`)**
```
[00:00:03.20 --> 00:00:07.80] Hello and welcome to the video.
[00:00:08.10 --> 00:00:12.40] Today we'll be covering the main topic.
```

**SRT subtitle (`.srt`)**
```
1
00:00:03,200 --> 00:00:07,800
Hello and welcome to the video.

2
00:00:08,100 --> 00:00:12,400
Today we'll be covering the main topic.
```

SRT files can be loaded directly into VLC, Premiere Pro, DaVinci Resolve, and YouTube's subtitle editor.

---

## Model Guide

| Model | Size | Speed | Best For |
|-------|------|-------|----------|
| `tiny` | 39M params | Very fast | Quick drafts, clear audio |
| `base` | 74M params | Fast | General use, good balance |
| `small` | 244M params | Moderate | Better accuracy, most accents |
| `medium` | 769M params | Slow | High accuracy, multiple languages |
| `large` | 1.5B params | Very slow | Best possible accuracy, difficult audio |
