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

REMOTE_BRANCH_HELP = (
    "Choose the remote branch containing the code you want to release.\n\n"
    "For example: branch release-1.3.0 with tag v1.3.0. "
    "Use main only if its code is ready to release.\n\n"
    "A blank branch uses the repository's default branch. "
    "The tool tags the latest commit already pushed to GitHub; local changes are not included.\n\n"
    "Both workflow files must exist on the selected branch and on the repository's default branch."
)


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
                 bg="#1e293b", fg="#e2e8f0", font=("Segoe UI", 10),
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
        root.title("GitHub Release Tool")
        root.geometry("780x810")
        root.minsize(680, 730)
        root.configure(bg="#0f172a")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#0f172a")
        style.configure("TLabel", background="#0f172a", foreground="#e2e8f0", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"), foreground="#ffffff")
        style.configure("Hint.TLabel", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#1e293b", foreground="#ffffff", insertcolor="#ffffff", padding=8)
        style.configure("TButton", background="#334155", foreground="#ffffff", padding=9, font=("Segoe UI", 10))
        style.map("TButton", background=[("active", "#475569"), ("disabled", "#1e293b")])
        style.configure("Start.TButton", background="#2563eb", font=("Segoe UI", 11, "bold"))
        style.configure("Help.TButton", padding=1, font=("Segoe UI", 10, "bold"), foreground="#93c5fd")
        style.map("Start.TButton", background=[("active", "#1d4ed8"), ("disabled", "#334155")])
        frame = ttk.Frame(root, padding=26)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Build & release", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Start Windows and macOS builds on GitHub.", style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 20))
        fields = [
            ("repository", "Repository", "OneTrueAsian/vault-spend"),
            ("ref", "Remote branch", ""),
            ("tag", "Release tag", ""),
            ("token", "GitHub API token", ""),
            ("windows_workflow", "Windows workflow", "release-windows.yml"),
            ("mac_workflow", "macOS workflow", "build-macos.yml"),
            ("tag_input", "Tag input name", "tag"),
        ]
        self.entries = []
        for row, (key, label, default) in enumerate(fields, 2):
            label_frame = ttk.Frame(frame)
            label_frame.grid(row=row, column=0, sticky="w", padx=(0, 18), pady=6)
            ttk.Label(label_frame, text=label).pack(side="left")
            if key == "ref":
                self.branch_help_button = ttk.Button(label_frame, text="i", width=2, style="Help.TButton", takefocus=True)
                self.branch_help_button.pack(side="left", padx=(7, 0))
                self.branch_help = HelpTooltip(self.branch_help_button, REMOTE_BRANCH_HELP)
            variable = tk.StringVar(value=default)
            self.inputs[key] = variable
            entry = ttk.Entry(frame, textvariable=variable, show="•" if key == "token" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            self.entries.append(entry)
        ttk.Label(frame, text="Blank branch uses the repo default. Token is kept only in memory.\nWorkflows must accept manual runs; leave tag input blank if they use the ref.",
                  style="Hint.TLabel", wraplength=690).grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 14))
        self.start_button = ttk.Button(frame, text="Start release builds", style="Start.TButton", command=self.start)
        self.start_button.grid(row=10, column=0, columnspan=2, sticky="ew")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(12, 8))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(frame, textvariable=self.status).grid(row=12, column=0, columnspan=2, sticky="w", pady=(0, 8))
        frame.rowconfigure(13, weight=1)
        self.log = tk.Text(frame, height=8, bg="#020617", fg="#cbd5e1", relief="flat", wrap="word", font=("Consolas", 10), state="disabled", padx=12, pady=12)
        self.log.grid(row=13, column=0, columnspan=2, sticky="nsew")
        actions = ttk.Frame(frame)
        actions.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.actions_button = ttk.Button(actions, text="Open GitHub Actions", command=self.open_actions, state="disabled")
        self.actions_button.pack(side="left")
        self.release_button = ttk.Button(actions, text="Open release", command=self.open_release, state="disabled")
        self.release_button.pack(side="left", padx=8)
        ttk.Label(frame, text="Only code already on GitHub is built. Publication follows your workflows' settings.", style="Hint.TLabel", wraplength=690).grid(row=15, column=0, columnspan=2, sticky="w", pady=(12, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
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
            messagebox.showerror("Check release details", str(exc), parent=self.root)
            return
        self.target_repo = request["repository"]
        self.target_tag = request["tag"]
        self.running = True
        self.inputs["token"].set("")
        self.links.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for entry in self.entries:
            entry.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.actions_button.configure(state="normal")
        self.release_button.configure(state="normal")
        self.progress.start(12)
        self.status.set("Requesting release builds…")
        threading.Thread(target=run_release, args=(request, self.events), daemon=True).start()

    def poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event["kind"] == "finished":
                    self.running = False
                    self.progress.stop()
                    self.progress["value"] = 0
                    self.start_button.configure(state="normal")
                    for entry in self.entries:
                        entry.configure(state="normal")
                    self.status.set("Builds requested — follow GitHub for results" if event["success"] else "Request failed — see the log")
                else:
                    self.append_log(str(event.get("message", "")))
                    url = event.get("url", "")
                    if event["kind"] == "link" and urlparse(url).hostname == "github.com" and urlparse(url).scheme == "https":
                        tag = f"link{len(self.links)}"
                        self.links.append(url)
                        self.log.configure(state="normal")
                        self.log.insert("end", url + "\n", tag)
                        self.log.tag_configure(tag, foreground="#60a5fa", underline=True)
                        self.log.tag_bind(tag, "<Button-1>", lambda _, target=url: webbrowser.open(target))
                        self.log.configure(state="disabled")
                        self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def open_actions(self) -> None:
        webbrowser.open(f"https://github.com/{self.target_repo}/actions")

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
