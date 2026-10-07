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
from tkinter import ttk, messagebox
from urllib.parse import urlparse
import webbrowser

SCRIPT = Path(__file__).with_name("Start-GitHubRelease.ps1")
HELP_PAGE = Path(__file__).with_name("help.html")

REMOTE_BRANCH_HELP = (
    "Choose the remote branch containing the code you want to release.\n\n"
    "For example: branch release-1.0.0 with tag v1.0.0. "
    "Use main only if its code is ready to release.\n\n"
    "A blank branch uses the repository's default branch. "
    "The tool tags the latest commit already pushed to GitHub; local changes are not included.\n\n"
    "Both workflow files must exist on the selected branch and on the repository's default branch."
)

FIELD_HELP = {
    "repository": (
        "Enter the GitHub repository you want to build, in owner/repo format.\n\n"
        "Example: your-username/your-app. This is the app repository, "
        "not the InternalUtils repository hosting this tool."
    ),
    "ref": REMOTE_BRANCH_HELP,
    "tag": (
        "Enter the release tag to create, such as v1.0.0. "
        "The tag will point to the selected remote branch's latest pushed commit.\n\n"
        "Commit and push any app version changes first; this tool does not update versions. "
        "An existing tag can be reused only when it points to the same commit. "
        "Tags are never moved or overwritten."
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
        "The workflow controls testing, installers and publication."
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
        "The default is tag. Both workflows must accept the same input name.\n\n"
        "Leave this blank if both workflows accept no inputs and build using the dispatched Git ref. "
        "The workflow is always dispatched against the release tag."
    ),
}


class HelpTooltip:
    """Hover or focus the help button; Escape dismisses the instructions."""
    def __init__(self, widget: ttk.Button, text: str):
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        self.pending: str | None = None
        widget.bind("<Enter>", self.schedule)
        widget.bind("<Leave>", self.hide)
        widget.bind("<FocusIn>", self.schedule)
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
    for key in ("windows_workflow", "mac_workflow"):
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", request[key]):
            raise ValueError("Use workflow filenames, such as release-windows.yml.")
    if request["windows_workflow"] == request["mac_workflow"]:
        raise ValueError("Choose different Windows and macOS workflows.")
    if request["tag_input"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", request["tag_input"]):
        raise ValueError("Enter a workflow input name, or leave it blank for workflows without inputs.")


def powershell_command() -> list[str]:
    # Prefer the system executable over Store aliases; never use a shell string.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    executable = str(system) if system.is_file() else shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("PowerShell is not installed. This tool requires Windows PowerShell 5.1 or PowerShell 7.")
    return [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-InputJson"]


def run_release(request: dict[str, str], events: queue.Queue, popen=subprocess.Popen) -> None:
    """Worker thread: stdin carries credentials, stdout carries JSON events."""
    secret = request["token"]
    error_seen = False
    process = None
    try:
        validate_request(request)
        process = popen(powershell_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            process.stdin.flush()
        finally:
            process.stdin.close()
            request["token"] = ""
        for line in process.stdout:
            safe_line = line.replace(secret, "[redacted]").strip()
            if not safe_line:
                continue
            try:
                event = json.loads(safe_line)
                if not isinstance(event, dict) or event.get("kind") not in ("log", "link", "error", "done"):
                    raise ValueError("Unexpected event")
            except (ValueError, TypeError):
                event = {"kind": "log", "message": safe_line}
            error_seen |= event["kind"] == "error"
            events.put(event)
        code = process.wait()
        if code and not error_seen:
            events.put({"kind": "error", "message": f"PowerShell exited with code {code}."})
        events.put({"kind": "finished", "success": code == 0 and not error_seen})
    except Exception as exc:
        # Error messages from process libraries must not expose credentials either.
        events.put({"kind": "error", "message": str(exc).replace(secret, "[redacted]")})
        events.put({"kind": "finished", "success": False})
    finally:
        request["token"] = ""
        if process is not None:
            if process.poll() is None:
                process.terminate()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


class ReleaseApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.running = False
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
        details.columnconfigure(0, weight=1)
        settings.columnconfigure(0, weight=1)
        ttk.Label(details, text="Choose what to release", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(details, text="Use a branch already pushed to GitHub.", style="PaperHint.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 12))
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
        ttk.Label(details, text="Workflows and app versions must already be committed.", style="PaperHint.TLabel").grid(row=6, column=0, sticky="w", pady=(8, 0))

        ttk.Label(settings, text="Connect your release workflows", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="Files live in the app's .github/workflows folder.", style="PaperHint.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 12))
        self.add_field(settings, 2, "windows_workflow", "Windows workflow", "release-windows.yml", "Filename only")
        self.add_field(settings, 3, "mac_workflow", "macOS workflow", "build-macos.yml", "Choose the workflow that publishes a release")
        self.add_field(settings, 4, "tag_input", "Tag input name", "tag", "Leave blank if both workflows use the Git ref")
        ttk.Label(settings, text="Both workflows need an enabled manual trigger.\nUse Help & setup for token permissions and setup instructions.", style="PaperHint.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 0))

        preview = ttk.Frame(body, style="Paper.TFrame", padding=18)
        preview.grid(row=0, column=1, sticky="nsew")
        self.summary = {}
        ttk.Label(preview, text="Release target", style="Heading.TLabel").pack(anchor="w", pady=(0, 14))
        for key, label in [("repository", "REPOSITORY"), ("ref", "SOURCE BRANCH"), ("tag", "TAG")]:
            ttk.Label(preview, text=label, style="PaperHint.TLabel").pack(anchor="w")
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
        ttk.Label(preview, text="Build results appear on GitHub.\nThis panel tracks dispatch requests.", style="PaperHint.TLabel").pack(anchor="w", pady=(12, 0))

        action_row = ttk.Frame(body)
        action_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 12))
        self.start_button = ttk.Button(action_row, text="Build release", style="Start.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.actions_button = ttk.Button(action_row, text="View builds", command=self.open_actions, state="disabled")
        self.actions_button.pack(side="left", padx=(10, 8))
        self.release_button = ttk.Button(action_row, text="View release", command=self.open_release, state="disabled")
        self.release_button.pack(side="left")
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
        ttk.Label(body, text="Only pushed code is built. Your workflows decide when the release is published.", style="Hint.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.append_log("Choose a repository, branch and tag, then enter your token.\nWorkflow filenames can be changed in Workflow settings.")
        for key in ("repository", "ref", "tag", "windows_workflow", "mac_workflow", "tag_input"):
            self.inputs[key].trace_add("write", self.update_summary)
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
        ttk.Label(heading, text=label, style="Paper.TLabel").pack(side="left")
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
        ttk.Label(field, text=hint, style="PaperHint.TLabel").grid(row=2, column=0, sticky="w", pady=(3, 0))

    def toggle_token(self):
        self.entry_by_key["token"].configure(show="" if self.show_token.get() else "•")

    def update_summary(self, *_args):
        if self.running:
            return
        for key, fallback in [("repository", "No repository selected"), ("ref", "Repository default branch"), ("tag", "No tag entered")]:
            self.summary[key].set(self.inputs[key].get().strip() or fallback)
        if hasattr(self, "target_repo"):
            for platform, variable in self.platform_status.items():
                variable.set(f"{platform}  ·  Not requested")
            self.actions_button.configure(state="disabled")
            self.release_button.configure(state="disabled")
            self.status.set("Ready to prepare a release")
            self.status_label.configure(foreground="#62665f")

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
        self.inputs["token"].set("")
        self.show_token.set(False)
        self.toggle_token()
        self.links.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.append_log(f"Release request: {self.target_repo} / {self.target_tag}")
        for platform, variable in self.platform_status.items():
            variable.set(f"{platform}  ·  Waiting")
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
        threading.Thread(target=run_release, args=(request, self.events), daemon=True).start()

    def update_platform(self, event):
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
                if event["kind"] == "finished":
                    self.running = False
                    self.progress.stop()
                    self.progress.grid_remove()
                    self.progress["value"] = 0
                    self.start_button.configure(state="normal", text="Build release")
                    self.token_toggle.configure(state="normal")
                    for entry in self.entries:
                        entry.configure(state="normal")
                    self.status.set("Builds requested · view results on GitHub" if event["success"] else "Request failed · see activity below")
                    self.status_label.configure(foreground="#326346" if event["success"] else "#a13226")
                    for platform, variable in self.platform_status.items():
                        if variable.get().endswith("Waiting"):
                            variable.set(f"{platform}  ·  Status unavailable" if event["success"] else f"{platform}  ·  Not requested")
                else:
                    self.update_platform(event)
                    self.append_log(str(event.get("message", "")), event["kind"])
                    url = event.get("url", "")
                    if event["kind"] == "link" and urlparse(url).hostname == "github.com" and urlparse(url).scheme == "https":
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
