# InternalUtils

A collection of desktop utilities for release automation and local video
transcription. Each tool runs independently and has its own setup requirements.

## Tools at a glance

| Tool | Purpose | Entry point | Guide |
| --- | --- | --- | --- |
| GitHub Release Tool | Prepare releases, build Windows locally, publish pinned tags and installers, then watch macOS and verify assets. | `release-tool/ReleaseTool.pyw` | [Setup and usage](release-tool/README.md) |
| VideoTranscribe | Turn a local video into a timestamped text transcript or SRT subtitles using Whisper. | `VideoTranscribe.pyw` | [Setup and usage](docs/video-transcribe.md) |

## GitHub Release Tool

Use the default **Local: full release** mode to follow the documented pipeline:
update versions and notes, run configured checks, commit/merge/push, build Windows
on your computer, publish at the recorded merge SHA, watch macOS on GitHub and
verify all four assets plus the latest release. **Local: Windows existing tag**
adds Windows installers to an existing release while preserving the tag and macOS.

The form has local repository/profile/notes pickers, per-platform status, command
logs, field help and an offline guide. Per-app JSON profiles make the gates,
metadata and installer patterns configurable. No E2E commands are added by default.
The older GitHub-only modes remain available and retain their workflow gates.

**Requirements:** Windows, Python 3.10+ with Tkinter, PowerShell, Git, GitHub CLI,
Node/npm, Rust and Tauri build prerequisites. OpenSSL apps also need Strawberry
Perl and short build paths. Local build dependencies and Cargo outputs are reused.
The supplied token is kept in memory and passed only through stdin or the scoped
Git/GitHub CLI environment; it is never saved or included in command arguments.

```powershell
python .\release-tool\ReleaseTool.pyw
```

See [setup and app profiles](release-tool/README.md), the bundled
[Help page](release-tool/help.html), and the source
[release process](release-tool/RELEASE-PROCESS.md).

## VideoTranscribe

Use this tool to transcribe local videos into `.txt` transcripts or `.srt`
subtitles. It offers five Whisper model sizes, an optional language hint,
progress logging, transcript preview and a command-line mode. Transcription runs
locally after dependencies and the selected model have been downloaded.

**Requirements:** Python 3.10+, FFmpeg on PATH, and the Python packages
`customtkinter`, `openai-whisper` and `ffmpeg-python`.

Install its dependencies on Windows:

```powershell
winget install Gyan.FFmpeg
python -m pip install customtkinter openai-whisper ffmpeg-python
```

Launch the desktop interface from the repository root:

```powershell
python .\VideoTranscribe.pyw
```

Choose a video, output folder, model and output format, then click **Transcribe**.
The first use of a model downloads its weights.

See the [VideoTranscribe guide](docs/video-transcribe.md) for CLI options,
examples, output formats and model selection.

## Repository layout

```text
InternalUtils/
├── README.md                 # Overview of the available tools
├── VideoTranscribe.pyw       # Video transcription UI and CLI
├── docs/
│   └── video-transcribe.md   # Detailed transcription guide
└── release-tool/
    ├── ReleaseTool.pyw       # Release UI entry point
    ├── release_ui.py         # Desktop interface
    ├── release_monitor.py    # Read-only workflow status checks
    ├── local_release.py      # Local Windows release pipeline
    ├── Run-LocalCommand.ps1   # Safe command bridge
    ├── profiles/             # Per-app JSON release recipes
    ├── Start-GitHubRelease.ps1
    ├── README.md             # Release setup and usage
    ├── help.html             # Offline user guide
    └── tests/                # Offline release-tool checks
```

## Verification

Run the release tool's offline checks from the repository root:

```powershell
python -m unittest discover -s release-tool/tests -v
```

These tests use fake GitHub responses and do not create tags or start remote
builds. They do not validate VideoTranscribe or an app's GitHub build environment.
