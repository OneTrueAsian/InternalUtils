# InternalUtils

A collection of desktop utilities for release automation and local video
transcription. Each tool runs independently and has its own setup requirements.

## Tools at a glance

| Tool | Purpose | Entry point | Guide |
| --- | --- | --- | --- |
| GitHub Release Tool | Create a release tag and start Windows/macOS build and release workflows for a GitHub app repository. | `release-tool/ReleaseTool.pyw` | [Setup and usage](release-tool/README.md) |
| VideoTranscribe | Turn a local video into a timestamped text transcript or SRT subtitles using Whisper. | `VideoTranscribe.pyw` | [Setup and usage](docs/video-transcribe.md) |

## GitHub Release Tool

Use this tool to start releases for different GitHub applications from a desktop
form. Enter the target repository, remote branch, release tag and API token;
configure the Windows and macOS workflow filenames in **Workflow settings**.

The UI includes a live target summary, per-platform build monitoring, readable
errors, clickable run links, log copying, field help and an offline user guide.
Tokens are masked and passed to PowerShell through stdin; they are not saved to
disk or placed in command-line arguments.

**Requirements:** Windows, Python 3.10+ with Tkinter, and Windows PowerShell 5.1
or PowerShell 7. No pip packages are required for this tool. The target repository
needs enabled Windows and macOS release workflows that accept manual dispatch,
and a GitHub token with the necessary repository permissions.

Run from the repository root:

```powershell
python .\release-tool\ReleaseTool.pyw
```

You can also double-click `ReleaseTool.pyw`. Keep the files in `release-tool`
together. The tool polls both workflow runs until completion and reports success only when
both succeed. **Stop monitoring** leaves builds running on GitHub; their links
remain available for logs and publication details. Only pushed code is built.

See the [release tool README](release-tool/README.md) for token permissions,
workflow setup, retries and CLI usage. The bundled [Help page](release-tool/help.html)
opens from **Help & setup** in the UI and can be read offline.

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
