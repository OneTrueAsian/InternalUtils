"""Windows desktop launcher for the PowerShell GitHub release script."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from urllib.parse import urlparse
import webbrowser
from release_monitor import monitor_runs, verify_windows_draft, draft_state, existing_draft_state, verify_complete_draft
from local_release import load_profile, validate_local_request, run_local_release, ReleaseError
from release_errors import reserved_test_version

SCRIPT = Path(__file__).resolve().with_name("Start-GitHubRelease.ps1")
HELP_PAGE = Path(__file__).resolve().with_name("help.html")

REMOTE_BRANCH_HELP = (
    "Choose the remote branch containing the code you want to release.\n\n"
    "For example: branch release-1.0.0 with tag v1.0.0. "
    "Use main only if its code is ready to release.\n\n"
    "A blank branch uses the repository's default branch. "
    "The tool tags the latest commit already pushed to GitHub; local changes are not included.\n\n"
    "Both workflow files must exist on the selected branch and on the repository's default branch."
)

BUILD_MODES = ("Windows + macOS", "Windows only · existing tag", "Local: full release", "Local: Windows existing tag",
               "Windows: sharded draft", "Windows: serial draft",
               "Windows + macOS: sharded draft", "Windows + macOS: serial draft", "Local: Windows-only unsigned draft")


def draft_mode(request):
    return {"windows_sharded": "windows_sharded", "windows_serial_draft": "windows_serial_draft",
            "draft_sharded_macos": "windows_sharded", "draft_serial_macos": "windows_serial_draft",
            BUILD_MODES[4]: "windows_sharded", BUILD_MODES[5]: "windows_serial_draft",
            BUILD_MODES[6]: "windows_sharded", BUILD_MODES[7]: "windows_serial_draft"}.get(request.get("build_mode"))


def macos_follows(request):
    """The draft modes that add macOS to the finished, signed Windows draft (never alongside it)."""
    return request.get("build_mode") in ("draft_sharded_macos", "draft_serial_macos", BUILD_MODES[6], BUILD_MODES[7])


def windows_only(request):
    return recovery_mode(request) or (bool(draft_mode(request)) and not macos_follows(request))


def local_mode(request):
    return request.get("build_mode") in ("local_release", "local_windows", "local_draft", BUILD_MODES[2], BUILD_MODES[3], BUILD_MODES[8])


def normalize_local(request):
    result = dict(request)
    mode = request.get("build_mode")
    result["build_mode"] = ("local_windows" if mode in ("local_windows", BUILD_MODES[3]) else
                            "local_draft" if mode in ("local_draft", BUILD_MODES[8]) else "local_release")
    return result


def recovery_mode(request: dict[str, str]) -> bool:
    return request.get("build_mode", "both") in ("windows_recovery", BUILD_MODES[1])


FIELD_HELP = {
    "local_repo": "Choose your local application Git repository, not InternalUtils. The full local release updates versions, runs checks, commits and merges, builds Windows here, and publishes. Use a short path for OpenSSL builds. Existing-tag mode checks out the original tag temporarily and restores your branch.",
    "app_profile": "Choose a JSON app profile defining version files, release branches, checks and exact asset filenames. Copy and configure custom-app.json for your build system. tauri-generic.json is an optional Tauri adapter. No E2E commands are added implicitly. Extra checks and preparation hooks can be configured.",
    "notes_file": "Choose a nonempty Markdown file describing this release. Its content is published as release notes. Optional profile install_notes are appended. Application metadata updates depend on the selected adapter or preparation hooks. Required for a new local release; existing-tag Windows builds preserve the existing release notes.",
    "build_mode": "Local: full release follows the selected app profile: version preparation, configured checks, commit/merge/push, local build, publish, optional macOS workflow and asset verification. Local: Windows existing tag builds Windows without moving a tag or rerunning macOS. The original GitHub modes remain available and retain their workflow test gates.",
    "repository": (
        "Enter the GitHub repository you want to build, in owner/repo format.\n\n"
        "Example: your-username/your-app. This is the app repository, "
        "not the InternalUtils repository hosting this tool."
    ),
    "ref": "In Local: full release this is the local release branch; blank uses release-X.Y.Z from the app profile. Local Windows existing-tag mode ignores this field.\n\n" + REMOTE_BRANCH_HELP + "\n\nIn Windows-only recovery this is the workflow branch (usually main), not the application source. The existing release tag supplies the application code.",
    "tag": (
        "Local full release prepares a new stable version from this tag, such as v1.0.1, and creates the tag at the merge SHA after Windows succeeds. Local Windows existing-tag mode requires an existing tag and preserves it.\n\nIn GitHub mode, enter a release tag to create, such as v1.0.0. "
        "The tag will point to the selected remote branch's latest pushed commit.\n\n"
        "Commit and push any app version changes first; this tool does not update versions. "
        "An existing tag can be reused only when it points to the same commit. "
        "Tags are never moved or overwritten. In Windows-only recovery this tag must already exist; its original application code is built even when the workflow branch has newer commits."
    ),
    "token": (
        "Enter a GitHub personal access token with access to the target repository.\n\n"
        "Fine-grained tokens need Contents: read/write and Actions: read/write. "
        "GitHub may also require Workflows: read/write for tag creation involving workflow files.\n\n"
        "The token is masked, cleared from the form when starting, and never saved to disk. "
        "It is passed to PowerShell through a private input pipe."
    ),
    "windows_workflow": (
        "Enter the filename of your Windows build and release workflow, "
        "such as release-windows.yml, without the .github/workflows/ folder.\n\n"
        "It must exist in the target repository on both the default branch and the selected branch, "
        "be enabled, and support workflow_dispatch. "
        "The workflow controls testing, installers and publication. This field is unused in local modes."
    ),
    "mac_workflow": (
        "Enter the filename of your macOS build and release workflow, "
        "such as build-macos.yml, without the .github/workflows/ folder.\n\n"
        "It must exist on both the default branch and the selected branch, "
        "be enabled, and support workflow_dispatch. Choose a release workflow; "
        "build-macos-check.yml only validates builds and does not publish releases."
    ),
    "tag_input": (
        "Enter the workflow_dispatch input name that receives the release tag. "
        "The default is tag. Local full release uses this input only for macOS; local Windows existing-tag mode ignores it. Both GitHub workflows must accept the same input name.\n\n"
        "Leave this blank if both workflows accept no inputs and build using the dispatched Git ref. "
        "Normal builds dispatch against the release tag. Recovery dispatches against the workflow branch and requires a tag input."
    ),
}
FIELD_HELP["build_mode"] += ("\n\nFor Vault Spend's new release pipeline choose Windows: sharded draft (recommended), or Windows: serial draft as fallback. "
                            "Both wait for the complete gate and protected signing, then verify an unpublished draft; they never dispatch macOS. "
                            "Windows + macOS: sharded/serial draft does the same, and only after Windows' signed draft is verified dispatches macOS for the same tag, source and release id, "
                            "then verifies the complete draft; you approve each signing step on GitHub. Local: Windows-only unsigned draft builds Windows here and leaves an unpublished draft with the installers only (no signing files, no macOS, not a complete release). "
                            "Publication always remains a separate owner action. "
                            "For Vault Spend, Local: full release publishes immediately and the hosted pipeline can never add the signed Windows files afterwards (and Local: Windows existing tag adds unsigned installers to a published release), so the tool refuses both there; use the hosted draft modes instead.")


class HelpTooltip:
    """Hover or activate the help button; keyboard focus alone does not open it."""
    def __init__(self, widget: ttk.Button, text: str):
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        self.pending: str | None = None
        widget.bind("<Enter>", self.schedule)
        widget.bind("<Leave>", self.hide)
        widget.bind("<Return>", lambda _event: self.show())
        widget.bind("<FocusOut>", self.hide)
        widget.bind("<Escape>", self.hide)
        widget.bind("<Destroy>", self.hide)
        widget.configure(command=self.show)

    def schedule(self, _event=None) -> None:
        self.hide()
        self.pending = self.widget.after(300, self.show)

    def show(self) -> None:
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        tk.Label(self.window, text=self.text, wraplength=360, justify="left",
                 bg="#fffdf4", fg="#30352e", font=("Segoe UI", 10),
                 padx=14, pady=12, relief="solid", borderwidth=1).pack()
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = min(self.widget.winfo_rootx(), self.widget.winfo_screenwidth() - width - 8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + height > self.widget.winfo_screenheight() - 8:
            y = self.widget.winfo_rooty() - height - 6
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    def hide(self, _event=None) -> None:
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


def validate_request(request: dict[str, str]) -> None:
    if local_mode(request):
        validate_local_request(normalize_local(request))
        return
    if not request["token"].strip():
        raise ValueError("Enter your GitHub API token.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request["repository"]):
        raise ValueError("Use owner/repo for the repository.")
    tag = request["tag"]
    if (not tag or tag.startswith(("-", "/")) or tag.endswith(("/", "."))
            or any(piece in tag for piece in ("..", "@{", "//")) or tag == "@"
            or re.search(r"[\s~^:?*\[\\\x00-\x20\x7f]", tag)
            or any(p.startswith(".") or p.endswith(".lock") for p in tag.split("/"))):
        raise ValueError("Enter a valid Git tag, such as v1.2.9.")
    if request.get("build_mode", "both") not in ("both", "windows_recovery", "windows_sharded", "windows_serial_draft", "draft_sharded_macos", "draft_serial_macos", *BUILD_MODES):
        raise ValueError("Choose a supported build mode.")
    if draft_mode(request):
        if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag) or re.fullmatch(r"v1\.3\.9\d", tag):
            raise ValueError("Draft release mode requires a production vX.Y.Z tag; v1.3.90–v1.3.99 are reserved tests.")
        if request["tag_input"] != "tag":
            raise ValueError("Draft release mode requires the workflow's tag input.")
    if reserved_test_version(request["repository"], tag):
        raise ValueError(f"{tag} is a reserved test version (v1.3.90 to v1.3.99) for {request['repository']} and cannot be released by this tool.")
    if recovery_mode(request) and not request["tag_input"]:
        raise ValueError("Windows recovery requires a tag input name.")
    for key in (("windows_workflow",) if windows_only(request) else ("windows_workflow", "mac_workflow")):
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", request[key]):
            raise ValueError("Use workflow filenames, such as release-windows.yml.")
    if not windows_only(request) and request["windows_workflow"] == request["mac_workflow"]:
        raise ValueError("Choose different Windows and macOS workflows.")
    if request["tag_input"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", request["tag_input"]):
        raise ValueError("Enter a workflow input name, or leave it blank for workflows without inputs.")


def powershell_command() -> list[str]:
    script = SCRIPT.resolve()
    if not script.is_file():
        raise RuntimeError(
            f"Release script is missing: {script}\n"
            "Restore Start-GitHubRelease.ps1 from the repository and keep it beside "
            "release_ui.py and ReleaseTool.pyw. If you updated the checkout, close "
            "and reopen the utility."
        )
    try:
        with script.open("rb") as source:
            source.read(1)
    except OSError as exc:
        raise RuntimeError(f"Cannot read the release script: {script}. Check file access permissions.") from exc
    # Prefer the system executable over Store aliases; never use a shell string.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    executable = str(system) if system.is_file() else shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("PowerShell is not installed. This tool requires Windows PowerShell 5.1 or PowerShell 7.")
    return [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-InputJson"]


def powershell_environment(executable):
    """Windows PowerShell 5.1 must not inherit a PowerShell 7 PSModulePath: it would resolve Microsoft.PowerShell.Security from the PowerShell 7 package,
    fail to load it, and the dispatcher's ConvertTo-SecureString would fail. Unsetting it lets 5.1 build its own default path."""
    env = dict(os.environ)
    if Path(executable).name.lower() == "powershell.exe":
        for key in [key for key in env if key.upper() == "PSMODULEPATH"]:
            del env[key]
    return env


def stream_powershell(payload, events, popen):
    """Run the dispatcher once: the request goes to its stdin (the token is cleared from the payload afterwards), its JSON events come back.

    Returns (exit code, run events, whether an error event was seen). Every line is redacted before it is parsed or shown."""
    secret = payload["token"]
    error_seen = False
    runs = []
    process = None
    try:
        command = powershell_command()
        process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                        env=powershell_environment(command[0]),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
            process.stdin.flush()
        finally:
            process.stdin.close()
            payload["token"] = ""
        for line in process.stdout:
            safe_line = line.replace(secret, "[redacted]").strip()
            if not safe_line:
                continue
            try:
                event = json.loads(safe_line)
                if not isinstance(event, dict) or event.get("kind") not in ("log", "link", "error", "done", "run"):
                    raise ValueError("Unexpected event")
            except (ValueError, TypeError):
                event = {"kind": "log", "message": safe_line}
            if event["kind"] == "run":
                runs.append(event)
                continue
            error_seen |= event["kind"] == "error"
            events.put(event)
        code = process.wait()
        if code and not error_seen:
            events.put({"kind": "error", "message": f"PowerShell exited with code {code}."})
        return code, runs, error_seen
    finally:
        payload["token"] = ""
        if process is not None:
            if process.poll() is None:
                process.terminate()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def track_windows_draft_runs(runs, mode):
    """Gate runs are reconciled job by job; the sharded gate must also have produced its own summary for this exact run."""
    gate_jobs = ["build", "checks-rust", "checks-other", "aggregate"] + [f"shard {i} of 6" for i in range(1, 7)]
    required = (["sharded-gate / " + job for job in gate_jobs] + ["sharded-package", "sharded-sign-updates"]
                if mode == "windows_sharded" else ["release", "sign-updates"])
    for run in runs:
        run.update(track_jobs=True, required_jobs=required)
        if mode == "windows_sharded":
            run["required_artifacts"] = ["sharded-gate-summary-{attempt}"]


def run_draft_with_macos(request, events, popen, stop=None):
    """Windows gate, packaging and signing first; only then macOS joins that same draft by its release id. Nothing here publishes or approves anything."""
    secret = request["token"]
    repository, tag, mode = request["repository"], request["tag"], draft_mode(request)

    class SafeEvents:
        def put(self, event):
            events.put({key: value.replace(secret, "[redacted]") if isinstance(value, str) else value for key, value in event.items()})
    safe = SafeEvents()

    def stage(name, platform, message):
        safe.put({"kind": "stage", "stage": name, "platform": platform, "message": message})

    def refuse(message):
        safe.put({"kind": "error", "message": message})
        safe.put({"kind": "finished", "success": False})

    def finish(source_sha, release_id):
        url = verify_complete_draft(repository, tag, source_sha, release_id, secret)
        message = "Complete draft verified: Windows and macOS signed files are in the same draft (not published). Review it, then publish it yourself on GitHub."
        stage("complete_draft_ready", "macOS", message)
        safe.put({"kind": "link", "message": message, "url": url})
        safe.put({"kind": "finished", "success": True})

    try:
        validate_request(request)
        existing = existing_draft_state(repository, tag, request.get("ref", ""), secret)
        source_sha = release_id = None
        if existing is not None:
            if existing["stage"] == "complete":
                safe.put({"kind": "log", "message": f"{tag} already has Windows and macOS signed files in one draft. Nothing is dispatched; verifying it."})
                finish(existing["source_sha"], existing["release_id"])
                return
            if existing["stage"] != "windows_ready" or not existing.get("branch_matches"):
                detail = "; ".join(existing.get("problems") or []) or (
                    "The selected branch no longer points at the tagged commit; tags are never moved." if existing["stage"] == "windows_ready" else "")
                refuse(f"{tag} already has a release in this state: {existing['stage'].replace('_', ' ')}. {detail} {existing['next_step']} Nothing was dispatched or changed.".replace("  ", " "))
                return
            source_sha, release_id = existing["source_sha"], existing["release_id"]
            safe.put({"kind": "log", "message": f"Windows' signed draft for {tag} is already complete. Adding macOS to that same draft; Windows is not run again."})
        else:
            windows_request = dict(request, build_mode=mode, include_macos=True)
            code, runs, error_seen = stream_powershell(windows_request, safe, popen)
            runs = list({run["workflow"]: run for run in runs if run.get("workflow") == request["windows_workflow"]}.values())
            if code != 0 or error_seen or len(runs) != 1:
                if code == 0 and not error_seen:
                    safe.put({"kind": "error", "message": "The Windows run was not identified. Overall build success cannot be confirmed."})
                safe.put({"kind": "finished", "success": False})
                return
            runs[0].update(show_progress=True, role="windows")
            track_windows_draft_runs(runs, mode)
            if not monitor_runs(repository, secret, runs, safe, stop=stop):
                safe.put({"kind": "log", "message": "macOS was not requested because Windows' draft is not confirmed."})
                safe.put({"kind": "finished", "success": False})
                return
            source_sha = runs[0].get("source_sha")
            current = draft_state(repository, tag, source_sha, secret)
            if current["stage"] != "windows_ready":
                refuse(f"Windows' run finished, but its draft is not ready: {current['stage'].replace('_', ' ')}. {' '.join(current.get('problems', []))} {current['next_step']} macOS was not requested.")
                return
            release_id = current["release_id"]
            stage("windows_draft_ready", "Windows", "Windows draft ready for review · not published. Adding macOS to this same draft now.")
        mac_request = dict(repository=repository, tag=tag, tag_input=request["tag_input"], mac_workflow=request["mac_workflow"],
                           windows_workflow=request["windows_workflow"], source_sha=source_sha, release_id=release_id,
                           token=secret, build_mode="macos_draft")
        code, mac_runs, error_seen = stream_powershell(mac_request, safe, popen)
        mac_runs = list({run["workflow"]: run for run in mac_runs if run.get("workflow") == request["mac_workflow"]}.values())
        if code != 0 or error_seen:
            refuse("macOS was not started. Windows' draft is still intact and unpublished; nothing was replaced. Fix the reported problem, then run this mode again to resume at macOS.")
            return
        if mac_runs:
            mac_runs[0].update(show_progress=True, role="macos", track_jobs=True, required_jobs=["build-macos", "sign-updates"])
            if not monitor_runs(repository, secret, mac_runs, safe, stop=stop):
                refuse("The macOS run did not finish successfully, or its result is not confirmed. Windows' draft is still intact and unpublished. Check the run on GitHub; do not start macOS again until you have read why it stopped.")
                return
        else:
            after = draft_state(repository, tag, source_sha, secret)
            if after["stage"] != "complete" or after["release_id"] != release_id:
                refuse("No macOS run was identified and the draft does not hold macOS's signed files. " + after["next_step"])
                return
            safe.put({"kind": "log", "message": "macOS's signed files were already in the draft; macOS was not run again."})
        finish(source_sha, release_id)
    except Exception as exc:
        # Error messages from process libraries must not expose credentials either.
        safe.put({"kind": "error", "message": str(exc)})
        safe.put({"kind": "finished", "success": False})
    finally:
        request["token"] = ""


def run_release(request: dict[str, str], events: queue.Queue, popen=subprocess.Popen, stop=None) -> None:
    """Worker thread: stdin carries credentials, stdout carries JSON events."""
    if local_mode(request):
        request["build_mode"] = normalize_local(request)["build_mode"]
        run_local_release(request, events, stop=stop)
        return
    if macos_follows(request):
        run_draft_with_macos(request, events, popen, stop=stop)
        return
    secret = request["token"]
    try:
        validate_request(request)
        request["build_mode"] = draft_mode(request) or ("windows_recovery" if recovery_mode(request) else "both")
        code, runs, error_seen = stream_powershell(request, events, popen)
        expected = {request["windows_workflow"]} if windows_only(request) else {request["windows_workflow"], request["mac_workflow"]}
        runs = list({run["workflow"]: run for run in runs if run.get("workflow") in expected}.values())
        for run in runs:
            run["show_progress"] = True
        if draft_mode(request):
            track_windows_draft_runs(runs, draft_mode(request))
        identified_selected = {run["workflow"] for run in runs} == expected
        if code == 0 and not identified_selected:
            error_seen = True
            events.put({"kind": "error", "message": "All selected workflow runs were not identified. Overall build success cannot be confirmed."})
        class SafeMonitorEvents:
            def put(self, event):
                events.put({key: value.replace(secret, "[redacted]") if isinstance(value, str) else value for key, value in event.items()})
        monitored = monitor_runs(request["repository"], secret, runs, SafeMonitorEvents(), stop=stop) if runs else False
        if code == 0 and not error_seen and monitored and identified_selected and draft_mode(request):
            url = verify_windows_draft(request["repository"], request["tag"], runs[0].get("source_sha"), secret)
            events.put({"kind": "link", "message": "Windows draft verified: installers and signing companions present. Not published; review before publishing.", "url": url})
        if code == 0 and not runs:
            events.put({"kind": "error", "message": "No workflow runs were identified. Build results are unknown; check GitHub Actions."})
        events.put({"kind": "finished", "success": code == 0 and not error_seen and monitored and identified_selected})
    except Exception as exc:
        # Error messages from process libraries must not expose credentials either.
        events.put({"kind": "error", "message": str(exc).replace(secret, "[redacted]")})
        events.put({"kind": "finished", "success": False})
    finally:
        request["token"] = ""


class ReleaseApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.monitoring = False
        self.stop_event = threading.Event()
        self.inputs: dict[str, tk.StringVar] = {}
        self.links: list[str] = []
        self.entries = []
        self.help_buttons = {}
        self.help_tooltips = {}
        root.title("Release Tool")
        root.geometry("1000x840")
        root.minsize(860, 760)
        root.configure(bg="#f3f2ee")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f2ee")
        style.configure("Paper.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f2ee", foreground="#252724", font=("Segoe UI", 10))
        style.configure("Paper.TLabel", background="#ffffff")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Heading.TLabel", background="#ffffff", font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#62665f", font=("Segoe UI", 9))
        style.configure("PaperHint.TLabel", background="#ffffff", foreground="#62665f", font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#252724", insertcolor="#252724", padding=7, bordercolor="#c7c9c1", lightcolor="#ffffff", darkcolor="#ffffff")
        style.map("TEntry", bordercolor=[("focus", "#427654")], fieldbackground=[("disabled", "#eeeeea")])
        style.configure("TButton", background="#eeeee9", foreground="#30352e", bordercolor="#c7c9c1", lightcolor="#eeeee9", darkcolor="#eeeee9", padding=(12, 6), font=("Segoe UI", 9))
        style.map("TButton", background=[("active", "#e2e4db")], foreground=[("disabled", "#888d83")])
        style.configure("Start.TButton", background="#326346", foreground="#ffffff", bordercolor="#326346", lightcolor="#326346", darkcolor="#326346", font=("Segoe UI", 10, "bold"), padding=(18, 8))
        style.map("Start.TButton", background=[("disabled", "#e4e7df"), ("active", "#254e36")], foreground=[("disabled", "#72776c")])
        style.configure("Help.TButton", padding=0, borderwidth=0, background="#ffffff", foreground="#666c61", font=("Segoe UI", 9, "bold"))
        style.map("Help.TButton", background=[("active", "#e9eee5")])
        style.configure("TNotebook", background="#f3f2ee", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 8), background="#e5e6df", foreground="#555c50")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#252724")])
        style.configure("TCheckbutton", background="#ffffff", foreground="#4d5548", font=("Segoe UI", 9))
        style.configure("Horizontal.TProgressbar", background="#427654", troughcolor="#e9ebe4", borderwidth=0)

        toolbar = ttk.Frame(root, padding=(24, 16))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Release Tool", style="Title.TLabel").pack(side="left")
        self.help_button = ttk.Button(toolbar, text="Help & setup", command=self.open_help)
        self.help_button.pack(side="right")
        ttk.Label(toolbar, text="GITHUB  /  WINDOWS + macOS", style="Hint.TLabel").pack(side="right", padx=18)
        ttk.Separator(root).pack(fill="x")

        body = ttk.Frame(root, padding=(24, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=245)
        body.rowconfigure(3, weight=1)
        self.tabs = ttk.Notebook(body)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        details = ttk.Frame(self.tabs, style="Paper.TFrame", padding=20)
        settings = ttk.Frame(self.tabs, style="Paper.TFrame", padding=20)
        self.tabs.add(details, text="Release details")
        self.tabs.add(settings, text="Workflow settings")
        local_settings = ttk.Frame(self.tabs, style="Paper.TFrame", padding=20)
        local_settings.columnconfigure(0, weight=1)
        self.tabs.add(local_settings, text="Local release")
        ttk.Label(local_settings, text="Build Windows on this computer", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.add_field(local_settings, 1, "local_repo", "Application folder", "", "The local clone of the app")
        self.add_field(local_settings, 2, "app_profile", "App profile", "", "Select a configured profile; bundled JSON files are templates")
        self.add_field(local_settings, 3, "notes_file", "Release notes", "", "Markdown notes for a new release")
        self.browse_buttons = []
        for row, key, title in [(1, "local_repo", "Choose application repository"), (2, "app_profile", "Choose app profile"), (3, "notes_file", "Choose release notes")]:
            button = ttk.Button(local_settings, text="Browse…", command=lambda field=key, prompt=title: self.browse_local(field, prompt))
            button.grid(row=row, column=1, sticky="e", padx=(10, 0))
            self.browse_buttons.append(button)
        ttk.Label(local_settings, text="Full release commits, merges, pushes and publishes.\nExisting-tag Windows mode preserves the tag and macOS assets.\nCheck the profile's commands before starting.", style="PaperHint.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        details.columnconfigure(0, weight=1)
        settings.columnconfigure(0, weight=1)
        ttk.Label(details, text="Choose what to release", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        mode_row = ttk.Frame(details, style="Paper.TFrame")
        mode_row.grid(row=1, column=0, sticky="ew", pady=(3, 12))
        ttk.Label(mode_row, text="Build", style="Paper.TLabel").pack(side="left", padx=(0, 8))
        self.inputs["build_mode"] = tk.StringVar(value=BUILD_MODES[2])
        self.mode_control = ttk.Combobox(mode_row, textvariable=self.inputs["build_mode"], values=BUILD_MODES, state="readonly", width=34)
        self.mode_control.pack(side="left")
        button = ttk.Button(mode_row, text="i", width=2, style="Help.TButton")
        button.pack(side="left", padx=6)
        self.help_buttons["build_mode"] = button
        self.help_tooltips["build_mode"] = HelpTooltip(button, FIELD_HELP["build_mode"])
        self.add_field(details, 2, "repository", "Repository", "", "owner/app-repo")
        branch_tag = ttk.Frame(details, style="Paper.TFrame")
        branch_tag.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        branch_tag.columnconfigure(0, weight=1)
        branch_tag.columnconfigure(1, weight=1)
        branch = ttk.Frame(branch_tag, style="Paper.TFrame")
        branch.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tag = ttk.Frame(branch_tag, style="Paper.TFrame")
        tag.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        branch.columnconfigure(0, weight=1)
        tag.columnconfigure(0, weight=1)
        self.add_field(branch, 0, "ref", "Remote branch", "", "Blank uses the default branch")
        self.add_field(tag, 0, "tag", "Release tag", "", "Example: v1.0.0")
        self.add_field(details, 4, "token", "GitHub API token", "", "Token cleared when the request starts")
        self.show_token = tk.BooleanVar(value=False)
        self.token_toggle = ttk.Checkbutton(details, text="Show token", variable=self.show_token, command=self.toggle_token)
        self.token_toggle.grid(row=5, column=0, sticky="e", pady=(0, 6))
        ttk.Label(details, text="Commit app code changes before starting; review the selected mode.", style="PaperHint.TLabel").grid(row=6, column=0, sticky="w", pady=(8, 0))

        ttk.Label(settings, text="Connect your release workflows", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="Files live in the app's .github/workflows folder.", style="PaperHint.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 12))
        self.add_field(settings, 2, "windows_workflow", "Windows workflow", "release-windows.yml", "Filename only")
        self.add_field(settings, 3, "mac_workflow", "macOS workflow", "build-macos.yml", "Choose the workflow that publishes a release")
        self.add_field(settings, 4, "tag_input", "Tag input name", "tag", "Leave blank if both workflows use the Git ref")
        ttk.Label(settings, text="Selected workflows need an enabled manual trigger.\nUse Help & setup for token permissions and setup instructions.", style="PaperHint.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 0))

        preview = ttk.Frame(body, style="Paper.TFrame", padding=18)
        preview.grid(row=0, column=1, sticky="nsew")
        self.summary = {}
        ttk.Label(preview, text="Release target", style="Heading.TLabel").pack(anchor="w", pady=(0, 14))
        for key, label in [("repository", "REPOSITORY"), ("ref", "SOURCE BRANCH"), ("tag", "TAG")]:
            label_widget = ttk.Label(preview, text=label, style="PaperHint.TLabel")
            label_widget.pack(anchor="w")
            if key == "ref":
                self.summary_branch_label = label_widget
            variable = tk.StringVar()
            self.summary[key] = variable
            ttk.Label(preview, textvariable=variable, style="Paper.TLabel", wraplength=210).pack(anchor="w", pady=(2, 12))
        ttk.Separator(preview).pack(fill="x", pady=(0, 12))
        ttk.Label(preview, text="Requested builds", style="Heading.TLabel").pack(anchor="w", pady=(0, 8))
        self.platform_status = {}
        for platform in ("Windows", "macOS"):
            variable = tk.StringVar(value=f"{platform}  ·  Not requested")
            self.platform_status[platform] = variable
            ttk.Label(preview, textvariable=variable, style="Paper.TLabel", wraplength=210).pack(anchor="w", pady=4)
        ttk.Label(preview, text="Build results appear on GitHub.\nThis panel tracks live workflow status.", style="PaperHint.TLabel").pack(anchor="w", pady=(12, 0))

        action_row = ttk.Frame(body)
        action_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 12))
        self.start_button = ttk.Button(action_row, text="Build release", style="Start.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.actions_button = ttk.Button(action_row, text="View builds", command=self.open_actions, state="disabled")
        self.actions_button.pack(side="left", padx=(10, 8))
        self.release_button = ttk.Button(action_row, text="View release", command=self.open_release, state="disabled")
        self.release_button.pack(side="left")
        self.stop_button = ttk.Button(action_row, text="Stop monitoring", command=self.stop_monitoring, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="Ready to prepare a release")
        self.status_label = ttk.Label(action_row, textvariable=self.status, style="Hint.TLabel", wraplength=220)
        self.status_label.pack(side="right")

        log_header = ttk.Frame(body)
        log_header.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(log_header, text="Activity", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.copy_button = ttk.Button(log_header, text="Copy log", command=self.copy_log)
        self.copy_button.pack(side="right")
        log_frame = ttk.Frame(body, style="Paper.TFrame")
        log_frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.log = tk.Text(log_frame, height=6, bg="#ffffff", fg="#333b31", relief="solid", borderwidth=1, highlightthickness=0, wrap="word", font=("Consolas", 10), state="disabled", padx=12, pady=10)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.tag_configure("error", foreground="#a13226")
        self.log.tag_configure("success", foreground="#326346")
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.progress.grid_remove()
        self.footer = tk.StringVar()
        ttk.Label(body, textvariable=self.footer, style="Hint.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.append_log("Choose a repository, branch and tag, then enter your token.\nWorkflow filenames can be changed in Workflow settings.")
        for key in ("repository", "ref", "tag", "windows_workflow", "mac_workflow", "tag_input"):
            self.inputs[key].trace_add("write", self.update_summary)
        for key in ("local_repo", "app_profile", "notes_file"):
            self.inputs[key].trace_add("write", self.update_summary)
        self.inputs["build_mode"].trace_add("write", self.update_summary)
        self.update_summary()
        self.entry_by_key["repository"].focus_set()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def add_field(self, parent, row, key, label, default, hint):
        if not hasattr(self, "entry_by_key"):
            self.entry_by_key = {}
        field = ttk.Frame(parent, style="Paper.TFrame")
        field.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        field.columnconfigure(0, weight=1)
        heading = ttk.Frame(field, style="Paper.TFrame")
        heading.grid(row=0, column=0, sticky="w", pady=(0, 5))
        label_widget = ttk.Label(heading, text=label, style="Paper.TLabel")
        label_widget.pack(side="left")
        if key == "ref":
            self.branch_label = label_widget
        button = ttk.Button(heading, text="i", width=2, style="Help.TButton", takefocus=True)
        button.pack(side="left", padx=(6, 0))
        self.help_buttons[key] = button
        self.help_tooltips[key] = HelpTooltip(button, FIELD_HELP[key])
        variable = tk.StringVar(value=default)
        self.inputs[key] = variable
        entry = ttk.Entry(field, textvariable=variable, show="•" if key == "token" else "", width=15)
        entry.grid(row=1, column=0, sticky="ew")
        self.entries.append(entry)
        self.entry_by_key[key] = entry
        hint_widget = ttk.Label(field, text=hint, style="PaperHint.TLabel")
        hint_widget.grid(row=2, column=0, sticky="w", pady=(3, 0))
        if key == "ref":
            self.branch_hint = hint_widget

    def toggle_token(self):
        self.entry_by_key["token"].configure(show="" if self.show_token.get() else "•")

    def update_summary(self, *_args, reset=True):
        if self.running:
            return
        request = {key: variable.get() for key, variable in self.inputs.items()}
        local = local_mode(request)
        mode = normalize_local(request)["build_mode"] if local else draft_mode(request) or ("windows_recovery" if recovery_mode(request) else "both")
        recovery = mode == "windows_recovery"
        profile = {}
        if local:
            try:
                profile = load_profile(request.get("app_profile", ""))
            except RuntimeError:
                pass
        no_mac = bool(profile) and not profile["mac_assets"]
        self.branch_hint.configure(text="Blank uses " + profile.get("release_branch", "release-{version}") if mode in ("local_release", "local_draft") else "Original tag supplies the code" if mode == "local_windows" else "Blank uses the default branch")
        self.branch_label.configure(text="Release branch" if mode in ("local_release", "local_draft") else "Unused for existing tag" if mode == "local_windows" else "Workflow branch" if recovery else "Remote branch")
        self.summary_branch_label.configure(text="RELEASE BRANCH" if mode in ("local_release", "local_draft") else "SOURCE" if mode == "local_windows" else "WORKFLOW BRANCH" if recovery else "SOURCE BRANCH")
        self.entry_by_key["ref"].configure(state="disabled" if mode == "local_windows" else "normal")
        self.entry_by_key["windows_workflow"].configure(state="disabled" if local else "normal")
        self.entry_by_key["mac_workflow"].configure(state="disabled" if windows_only(request) or mode in ("local_windows", "local_draft") or no_mac else "normal")
        self.entry_by_key["tag_input"].configure(state="disabled" if mode in ("local_windows", "local_draft") or (local and no_mac) else "normal")
        self.entry_by_key["notes_file"].configure(state="normal" if mode in ("local_release", "local_draft") else "disabled")
        for key in ("local_repo", "app_profile"):
            self.entry_by_key[key].configure(state="normal" if local else "disabled")
        for key, button in zip(("local_repo", "app_profile", "notes_file"), self.browse_buttons):
            button.configure(state="normal" if local and (key != "notes_file" or mode in ("local_release", "local_draft")) else "disabled")
        self.footer.set("Windows-only unsigned draft: builds the Windows installers on this computer and prepares an unpublished draft with the installers only (no signing files, no macOS); nothing is published." if mode == "local_draft" else "Windows + macOS: waits for Windows to finish and sign, then adds macOS to the same unpublished draft; you approve signing on GitHub and publish yourself." if macos_follows(request) else "Build release runs the full local pipeline and publishes after Windows succeeds." if mode == "local_release" else "Builds the original tag locally and uploads Windows installers; macOS is preserved." if mode == "local_windows" else "Windows only: prepare a signed draft for review. macOS and publication are separate." if draft_mode(request) else "Only pushed code is built. Your workflows decide when the release is published.")
        for key, fallback in [("repository", "No repository selected"), ("ref", "Existing release tag" if mode == "local_windows" else ("release-" + self.inputs["tag"].get()[1:] if re.fullmatch(r"v\d+\.\d+\.\d+", self.inputs["tag"].get()) else "Release branch from tag") if mode in ("local_release", "local_draft") else "Repository default branch"), ("tag", "No tag entered")]:
            self.summary[key].set((self.inputs[key].get().strip() if not (mode == "local_windows" and key == "ref") else "") or fallback)
        if reset and hasattr(self, "target_repo"):
            for platform, variable in self.platform_status.items():
                variable.set(f"{platform}  ·  Not requested")
            self.actions_button.configure(state="disabled")
            self.release_button.configure(state="disabled")
            self.status.set("Ready to prepare a release")
            self.status_label.configure(foreground="#62665f")

    def browse_local(self, key, title):
        if key == "local_repo":
            result = filedialog.askdirectory(title=title, parent=self.root)
        else:
            result = filedialog.askopenfilename(title=title, parent=self.root, filetypes=[("App profile", "*.json")] if key == "app_profile" else [("Release notes", "*.md"), ("Text files", "*.txt")])
        if result:
            self.inputs[key].set(result)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))

    def append_log(self, message: str, kind: str = "log") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n", "error" if kind == "error" else "success" if kind == "done" else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self) -> None:
        if self.running:
            return
        request = {key: var.get().strip() for key, var in self.inputs.items()}
        try:
            validate_request(request)
            if not local_mode(request):
                powershell_command()
        except (ValueError, RuntimeError) as exc:
            self.status.set("Check release details")
            self.status_label.configure(foreground="#a13226")
            self.append_log(str(exc), "error")
            for key in ("repository", "tag", "token"):
                if not self.inputs[key].get().strip():
                    self.tabs.select(0)
                    self.entry_by_key[key].focus_set()
                    break
            return
        self.target_repo = request["repository"]
        self.target_tag = request["tag"]
        self.running = True
        self.monitoring = False
        self.stop_event.clear()
        self.inputs["token"].set("")
        self.show_token.set(False)
        self.toggle_token()
        self.links.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.append_log(f"Release request: {self.target_repo} / {self.target_tag}")
        self.local = local_mode(request)
        self.local_kind = normalize_local(request)["build_mode"] if self.local else ""
        self.draft = bool(draft_mode(request))
        self.macos_draft = macos_follows(request)
        self.recovery = windows_only(request) or self.local_kind in ("local_windows", "local_draft") or (self.local and not load_profile(request["app_profile"])["mac_assets"])
        for platform, variable in self.platform_status.items():
            variable.set(f"{platform}  ·  Not requested" if self.recovery and platform == "macOS" else f"{platform}  ·  Waiting")
        self.mode_control.configure(state="disabled")
        for button in self.browse_buttons:
            button.configure(state="disabled")
        for entry in self.entries:
            entry.configure(state="disabled")
        self.token_toggle.configure(state="disabled")
        self.start_button.configure(state="disabled", text="Requesting…")
        self.actions_button.configure(state="normal")
        self.release_button.configure(state="normal")
        self.progress.grid()
        self.progress.start(12)
        self.status.set("Checking repository and workflows…")
        self.status_label.configure(foreground="#62665f")
        threading.Thread(target=run_release, args=(request, self.events, subprocess.Popen, self.stop_event), daemon=True).start()

    def stop_monitoring(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status.set("Stopping after the current step…" if getattr(self, "local", False) else "Stopping local monitoring…")

    STAGE_LABELS = {"building": "Building", "awaiting_signing_approval": "Waiting for signing approval", "windows_draft_ready": "Draft ready · not published",
                    "macos_running": "Running", "complete_draft_ready": "Draft ready · not published", "failed": "Failed", "unconfirmed": "Not confirmed"}
    STAGE_STATUS = {"building": "Building the Windows draft…", "awaiting_signing_approval": "Waiting for you to approve signing on GitHub",
                    "windows_draft_ready": "Windows draft verified · adding macOS", "macos_running": "macOS is running…",
                    "complete_draft_ready": "Complete draft verified · finishing", "failed": "A build failed · see activity", "unconfirmed": "Result not confirmed · check GitHub"}

    def update_platform(self, event):
        if event["kind"] == "stage":
            platform = event.get("platform")
            if platform in self.platform_status and event.get("stage") in self.STAGE_LABELS:
                self.platform_status[platform].set(f"{platform}  ·  {self.STAGE_LABELS[event['stage']]}")
                self.status.set(self.STAGE_STATUS[event["stage"]])
                self.status_label.configure(foreground="#a13226" if event["stage"] in ("failed", "unconfirmed") else "#62665f")
            return
        if event["kind"] == "status":
            labels = {"in_progress": "Running", "queued": "Queued", "waiting": "Waiting for approval", "pending": "Pending", "requested": "Requested", "unknown": "Status unknown"}
            text = str(event.get("conclusion") or ("Awaiting result" if event["state"] == "completed" else labels.get(event["state"], event["state"].replace("_", " ")))).replace("_", " ").capitalize()
            for key, platform in [("windows_workflow", "Windows"), ("mac_workflow", "macOS")]:
                if self.inputs[key].get().strip() == event.get("workflow"):
                    self.platform_status[platform].set(f"{platform}  ·  {text}")
            return
        message = str(event.get("message", ""))
        for key, platform in [("windows_workflow", "Windows"), ("mac_workflow", "macOS")]:
            if not message.startswith(self.inputs[key].get().strip() + ":"):
                continue
            if event["kind"] == "error":
                text = "Request failed"
            elif "already started" in message:
                text = "Existing run found"
            elif event["kind"] == "link":
                text = "Build requested"
            else:
                continue
            self.platform_status[platform].set(f"{platform}  ·  {text}")

    def poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event["kind"] == "monitoring":
                    self.monitoring = True
                    self.stop_button.configure(state="normal")
                    self.status.set("Running local release…" if getattr(self, "local", False) and event["message"].startswith("Local Windows") else "Monitoring GitHub builds…")
                    self.stop_button.configure(text="Stop after step" if getattr(self, "local", False) else "Stop monitoring")
                    self.start_button.configure(text="Monitoring…")
                    self.append_log(event["message"])
                elif event["kind"] == "finished":
                    self.running = False
                    self.monitoring = False
                    self.stop_button.configure(state="disabled")
                    self.progress.stop()
                    self.progress.grid_remove()
                    self.progress["value"] = 0
                    self.start_button.configure(state="normal", text="Build release")
                    self.token_toggle.configure(state="normal")
                    for entry in self.entries:
                        entry.configure(state="normal")
                    self.mode_control.configure(state="readonly")
                    self.update_summary(reset=False)
                    self.status.set(("Complete draft ready for review · not published" if getattr(self, "macos_draft", False) else "Windows-only unsigned draft ready · not published" if getattr(self, "local_kind", "") == "local_draft" else "Windows draft ready for review · not published" if getattr(self, "draft", False) else "Windows installers verified" if getattr(self, "local_kind", "") == "local_windows" else "Release verified: configured assets" if getattr(self, "local_kind", "") == "local_release" else "Windows build completed successfully" if getattr(self, "recovery", False) else "Both builds completed successfully") if event["success"] else "Monitoring stopped · check GitHub" if self.stop_event.is_set() else "Release not successful · see activity")
                    self.status_label.configure(foreground="#326346" if event["success"] else "#a13226")
                    for platform, variable in self.platform_status.items():
                        if variable.get().endswith("Waiting"):
                            variable.set(f"{platform}  ·  Status unavailable" if event["success"] else f"{platform}  ·  Not requested")
                else:
                    self.update_platform(event)
                    self.append_log(str(event.get("message", "")), event["kind"])
                    url = event.get("url", "")
                    if event["kind"] in ("link", "status") and url not in self.links and urlparse(url).hostname == "github.com" and urlparse(url).scheme == "https":
                        tag = f"link{len(self.links)}"
                        self.links.append(url)
                        self.log.configure(state="normal")
                        self.log.insert("end", url + "\n", tag)
                        self.log.tag_configure(tag, foreground="#285f42", underline=True)
                        self.log.tag_bind(tag, "<Button-1>", lambda _, target=url: webbrowser.open(target))
                        self.log.configure(state="disabled")
                        self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def open_actions(self) -> None:
        webbrowser.open(f"https://github.com/{self.target_repo}/actions")

    def open_help(self) -> None:
        if not HELP_PAGE.is_file():
            messagebox.showerror("Help page missing", "Keep help.html beside release_ui.py when copying the tool.", parent=self.root)
            return
        webbrowser.open(HELP_PAGE.resolve().as_uri())

    def open_release(self) -> None:
        from urllib.parse import quote
        webbrowser.open(f"https://github.com/{self.target_repo}/releases/tag/{quote(self.target_tag, safe='')}")

    def close(self) -> None:
        if self.running and self.monitoring:
            self.stop_event.set()
            self.root.destroy()
            return
        if self.running:
            messagebox.showinfo("Release request in progress", "Wait for the requests to finish. Builds already started on GitHub continue independently.", parent=self.root)
            return
        self.inputs["token"].set("")
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ReleaseApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
