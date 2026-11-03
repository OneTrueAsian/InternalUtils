"""Profile-driven local build and GitHub release pipeline.

Runtime commands are authorized by Build release, not executed on import.
No UI/E2E gates are added implicitly; each application's gates live in its profile.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from urllib.parse import quote

from release_monitor import monitor_runs
from release_errors import ReleaseError, CommandError, inside
from release_metadata import check_versions, prepare_versions, product_name, changelog_entry, custom_versions

HERE = Path(__file__).resolve().parent
LOCAL_MODES = ("local_release", "local_windows")


def load_profile(path):
    try:
        profile_path = Path(path).expanduser()
        if not profile_path.is_absolute():
            profile_path = HERE / profile_path
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise ReleaseError("Choose a readable JSON app profile from Local release settings.") from None
    if not isinstance(profile, dict):
        raise ReleaseError("An app profile must be a JSON object.")
    adapter = profile.get("adapter")
    if adapter not in ("tauri", "custom"):
        raise ReleaseError("Profile adapter must be tauri or custom.")
    profile.setdefault("mac_assets", [])
    required = ("default_branch", "release_branch", "check_commands", "windows_assets") + (("cargo_manifest", "cargo_lock") if adapter == "tauri" else ("product_name", "version_files", "build_command", "required_tools"))
    for key in required:
        if not profile.get(key):
            raise ReleaseError(f"App profile requires {key}.")
    for key in ("default_branch", "release_branch") + (("cargo_manifest", "cargo_lock") if adapter == "tauri" else ("product_name",)):
        if not isinstance(profile[key], str):
            raise ReleaseError(f"Profile {key} must be a string.")
    for key in ("windows_assets", "mac_assets"):
        if not isinstance(profile[key], list) or not all(isinstance(pattern, str) for pattern in profile[key]):
            raise ReleaseError(f"{key} must be a list of exact filename patterns.")
    for key in ("check_commands", "lock_commands", "setup_commands", "prepare_commands", "build_command"):
        commands = [profile[key]] if key == "build_command" and key in profile else profile.get(key, [])
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ReleaseError(f"{key} must contain command argument arrays, not shell strings.")
    workflows = profile.setdefault("post_release_workflows", [])
    if not isinstance(workflows, list) or not all(
            isinstance(item, dict) and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", str(item.get("workflow", "")))
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(item.get("tag_input", ""))) for item in workflows):
        raise ReleaseError("post_release_workflows must list objects with a workflow filename and a tag_input name.")
    signed = profile.setdefault("signed_update_assets", [])
    suffixes = profile.setdefault("signed_update_suffixes", [".manifest.json", ".signature.json"])
    for key, value in (("signed_update_assets", signed), ("signed_update_suffixes", suffixes)):
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ReleaseError(f"{key} must be a list of exact filename patterns.")
    if not isinstance(profile.get("required_tools", []), list) or not all(isinstance(tool, str) and tool for tool in profile.get("required_tools", [])):
        raise ReleaseError("required_tools must be a list of executable names.")
    if adapter == "custom":
        if not isinstance(profile["version_files"], list):
            raise ReleaseError("version_files must be a list of JSON file/key objects.")
        for item in profile["version_files"]:
            if not isinstance(item, dict) or not isinstance(item.get("file"), str) or not item["file"] or not isinstance(item.get("keys"), list) or not item["keys"] or not all(isinstance(key, list) and key and all(isinstance(part, str) for part in key) for key in item["keys"]):
                raise ReleaseError("Each version_files entry needs file and keys (arrays of JSON property names).")

    return profile


def validate_local_request(request):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request.get("repository", "")):
        raise ReleaseError("Use owner/repo for the repository.")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", request.get("tag", "")):
        raise ReleaseError("Local releases require a stable version tag such as v1.0.0.")
    if not request.get("token"):
        raise ReleaseError("Enter your GitHub API token.")
    folder = Path(request.get("local_repo", "")).expanduser()
    if not request.get("local_repo") or not folder.is_dir():
        raise ReleaseError("Choose an existing local application repository folder.")
    if request.get("build_mode") not in LOCAL_MODES:
        raise ReleaseError("Choose a local release mode.")
    profile = load_profile(request.get("app_profile", ""))
    if request["build_mode"] == "local_release":
        notes = Path(request.get("notes_file", "")).expanduser()
        if not request.get("notes_file") or not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
            raise ReleaseError("Choose a nonempty Markdown release-notes file for a new release.")
        if profile["mac_assets"] and not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", request.get("mac_workflow", "")):
            raise ReleaseError("Choose the macOS release workflow filename.")
        if profile["mac_assets"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", request.get("tag_input", "")):
            raise ReleaseError("The local pipeline requires a macOS tag input name.")
    return profile


class Runner:
    """Stream commands without a shell expression or token in the argument list."""
    def __init__(self, root, token, events, stop=None):
        self.root = root
        self.token = token
        self.events = events
        self.stop = stop or threading.Event()

    def emit(self, kind, message, **fields):
        self.events.put(dict(kind=kind, message=str(message).replace(self.token, "[redacted]"), **fields))

    def run(self, args, *, check=True, retry=False, quiet=False, data=None, ignore_stop=False, env_extra=None):
        args = [str(arg) for arg in args]
        if self.stop.is_set() and not ignore_stop:
            raise ReleaseError("Local release stopped after the current step. Published assets and pushed commits are retained.")
        self.emit("log", "$ " + subprocess.list2cmdline(args))
        env = dict(os.environ)
        env.update(getattr(self, "build_env", {}))
        env.update(env_extra or {})
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_DEBUG", "GIT_TRACE", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE"):
            env.pop(key, None)
        env["GH_PROMPT_DISABLED"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        executable = Path(args[0]).name.lower().removesuffix(".exe")
        if executable in ("gh", "git"):
            env["GH_TOKEN"] = self.token
        if executable == "git":
            # Scoped Git credential helper: no saved PAT or global git configuration.
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            env[f"GIT_CONFIG_KEY_{count}"] = "credential.https://github.com.helper"
            env[f"GIT_CONFIG_VALUE_{count}"] = ""
            env[f"GIT_CONFIG_KEY_{count + 1}"] = "credential.https://github.com.helper"
            env[f"GIT_CONFIG_VALUE_{count + 1}"] = "!gh auth git-credential"
            env["GIT_CONFIG_COUNT"] = str(count + 2)
        command = args
        stdin = data
        if executable in ("npm", "npx", "cargo") or (shutil.which(args[0]) or args[0]).lower().endswith((".cmd", ".bat")):
            system = Path(env.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            command = [str(system) if system.is_file() else shutil.which("pwsh") or "powershell",
                       "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "Run-LocalCommand.ps1")]
            stdin = json.dumps(dict(command=args, directory=str(self.root))) + "\n"
        for attempt in range(2 if retry else 1):
            process = subprocess.Popen(command, cwd=self.root, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                process.stdin.write(stdin or "")
                process.stdin.close()
                lines = []
                for line in process.stdout:
                    lines.append(line)
                    if not quiet:
                        self.emit("log", line.rstrip())
                code = process.wait()
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait()
            output = "".join(lines)
            if code == 0 or not check:
                return code, output
            if attempt == 0 and retry:
                self.emit("log", "This check failed. Rerunning that command alone once before stopping.")
                if self.stop.is_set():
                    raise ReleaseError("Local release stopped; the failed command was not repeated.")
                continue
            raise CommandError(f"Command failed ({code}): {subprocess.list2cmdline(args)}\n{output[-4000:].replace(self.token, '[redacted]')}")

    def text(self, args, **kwargs):
        return self.run(args, quiet=True, **kwargs)[1].strip()

    def api(self, repository, path, body=None, missing=False):
        resource = f"repos/{repository}" + (f"/{path}" if path else "")
        args = ["gh", "api", resource, "-H", "X-GitHub-Api-Version: 2026-03-10"]
        if body is not None:
            args += ["--method", "POST", "--input", "-"]
        code, output = self.run(args, quiet=True, check=not missing, data=json.dumps(body) if body is not None else None)
        if code:
            if missing and "HTTP 404" in output:
                return None
            raise CommandError(output.replace(self.token, "[redacted]"))
        return json.loads(output) if output.strip() else {}


@contextmanager
def repository_lock(runner):
    path = Path(runner.text(["git", "rev-parse", "--git-path", "internalutils-release.lock"]))
    if not path.is_absolute():
        path = runner.root / path
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError:
        raise ReleaseError(f"Another local release owns {path}. If that process has exited, remove its stale lock before retrying.") from None
    with handle:
        handle.write(json.dumps(dict(pid=os.getpid())))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def remote_tag(runner, repository, tag):
    ref = runner.api(repository, "git/ref/tags/" + quote(tag, safe=""), missing=True)
    if ref is None:
        return None
    obj = ref["object"]
    for _ in range(8):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = runner.api(repository, "git/tags/" + obj["sha"])["object"]
    raise ReleaseError("Release tag does not resolve to a commit.")


def tracked_changes(runner):
    changes = runner.text(["git", "status", "--porcelain", "--untracked-files=no"])
    if not changes:
        return ""
    # Git's clean filters can make a file content-identical while status keeps
    # a stale index entry after a build rewrites CRLF as LF. Never normalize
    # when the index or worktree contains an actual content change.
    for args in (["git", "diff", "--cached", "--quiet", "--"], ["git", "diff", "--quiet", "--"]):
        code, _ = runner.run(args, check=False, quiet=True, ignore_stop=True)
        if code == 1:
            return changes
        if code != 0:
            raise ReleaseError("Could not verify tracked file contents; index refresh was not attempted.")
    before = runner.text(["git", "write-tree"], ignore_stop=True)
    runner.emit("log", "Tracked contents are unchanged; refreshing Git index entries after line-ending normalization.")
    runner.run(["git", "add", "--update", "--", ":/"], ignore_stop=True)
    if runner.text(["git", "write-tree"], ignore_stop=True) != before:
        raise ReleaseError("Files changed while refreshing the index. Changes were retained; publishing stopped.")
    return runner.text(["git", "status", "--porcelain", "--untracked-files=no"], ignore_stop=True)


def check_origin(runner, repository):
    origin = runner.text(["git", "remote", "get-url", "origin"])
    name = re.sub(r"\.git$", "", origin.rstrip("/"))
    if name.lower() not in (f"https://github.com/{repository}".lower(), f"git@github.com:{repository}".lower(), f"ssh://git@github.com/{repository}".lower()):
        raise ReleaseError("Local origin does not match the repository entered in the tool.")
    changes = tracked_changes(runner)
    if changes:
        raise ReleaseError(f"The application repository has tracked changes in {runner.root}:\n{changes}\nCommit or resolve these files before starting. If git diff shows no content changes, check Git's line-ending/index state; substantive changes are retained and block the release.")


def preflight_tools(root, profile):
    for name in dict.fromkeys(["git", "gh"] + profile.get("required_tools", ["node", "npm", "npx", "cargo"] if profile.get("adapter", "tauri") == "tauri" else [])):
        if not shutil.which(name):
            raise ReleaseError(f"Missing local build tool: {name}.")
    if os.name != "nt":
        raise ReleaseError("Local Windows release builds require a Windows machine.")
    build_env = {}
    if profile.get("requires_strawberry_perl"):
        configured = profile.get("perl_bin") or r"C:\Strawberry\perl\bin"
        if (Path(configured) / "perl.exe").is_file():
            build_env["PATH"] = str(configured) + os.pathsep + os.environ.get("PATH", "")
        perl = shutil.which("perl", path=build_env.get("PATH"))
        if not perl or "strawberry" not in perl.lower():
            raise ReleaseError("Put Strawberry Perl first on PATH; Git's Perl cannot build this app's OpenSSL.")
        limit = profile.get("build_path_limit", 90)
        target = Path(profile.get("target_dir") or os.environ.get("CARGO_TARGET_DIR") or root / "target")
        if not target.is_absolute():
            target = root / target
        if max(len(str(root)), len(str(target.resolve()))) > limit:
            raise ReleaseError(f"Build paths are too long for this app's OpenSSL setup (profile limit {limit}). Use a shorter repository/target folder.")
    if profile.get("target_dir") and profile.get("adapter", "tauri") == "tauri":
        target = Path(profile["target_dir"])
        build_env["CARGO_TARGET_DIR"] = str((target if target.is_absolute() else root / target).resolve())
    return build_env


def expected_assets(root, profile, product, version):
    if not product or re.search(r'[<>:"/\\|?*#\x00-\x1f]', product):
        raise ReleaseError("Product name cannot safely form Windows installer filenames.")
    values = dict(product=product, version=version, architecture=profile.get("architecture", "x64"))
    target = Path(profile.get("target_dir") or os.environ.get("CARGO_TARGET_DIR") or root / "target")
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    files = []
    for pattern in profile["windows_assets"]:
        path = (target / pattern.format(**values)).resolve()
        if not path.is_relative_to(target) or "#" in str(path) or any(ch in pattern for ch in "*?"):
            raise ReleaseError("Windows asset patterns must be exact filenames under the target folder.")
        files.append(path)
    windows = [path.name.replace(" ", ".") for path in files]
    mac = [pattern.format(**values).replace(" ", ".") for pattern in profile["mac_assets"]]
    names = windows + mac
    if len(set(names)) != len(names) or any(not name or name != Path(name).name or any(ch in name for ch in "*?#") for name in names):
        raise ReleaseError("Asset patterns must produce unique, exact release filenames.")
    return files, windows, mac


def signed_update_files(profile, product, version):
    values = dict(product=product, version=version, architecture=profile.get("architecture", "x64"))
    names = [pattern.format(**values).replace(" ", ".") for pattern in profile["signed_update_assets"]]
    return [name + suffix for name in names for suffix in profile["signed_update_suffixes"]]


def check_dispatch_workflow(runner, root, repository, filename, label):
    try:
        workflow = inside(root, ".github/workflows/" + filename).read_text(encoding="utf-8")
    except OSError:
        raise ReleaseError(f"{label} workflow file .github/workflows/{filename} was not found in the application repository.") from None
    if not re.search(r"(?m)^\s*workflow_dispatch\s*:", workflow) or re.search(r"(?m)^\s+push\s*:", workflow):
        raise ReleaseError(f"{label} publishing must be manual-only workflow_dispatch.")
    if runner.api(repository, "actions/workflows/" + filename).get("state") != "active":
        raise ReleaseError(f"{label} workflow must be registered and enabled on the default branch.")


def verify_release(runner, repository, tag, sha, expected, latest=False):
    release = runner.api(repository, "releases/tags/" + quote(tag, safe=""))
    if release.get("draft") or release.get("prerelease") or release.get("tag_name") != tag:
        raise ReleaseError("Release is missing or is draft/prerelease; final publication is not verified.")
    assets = release.get("assets", [])
    by_name = {asset["name"]: asset for asset in assets}
    if any(name not in by_name or by_name[name].get("size", 0) <= 0 for name in expected):
        raise ReleaseError("Release is missing expected nonempty assets: " + ", ".join(name for name in expected if name not in by_name or by_name[name].get("size", 0) <= 0))
    if latest and (set(by_name) != set(expected) or len(assets) != len(expected)):
        raise ReleaseError("New release must contain exactly the configured release assets.")
    if remote_tag(runner, repository, tag) != sha:
        raise ReleaseError("Remote release tag no longer points at the recorded release commit.")
    if latest and runner.api(repository, "releases/latest").get("tag_name") != tag:
        raise ReleaseError("releases/latest does not point at this version.")
    return release


def check_profile_source(runner, profile, source):
    """Check required metadata in the actual source commit before switching or setup."""
    if profile["adapter"] == "custom":
        paths = [item["file"] for item in profile["version_files"]]
    else:
        paths = ["package.json", "src-tauri/tauri.conf.json", profile["cargo_manifest"], "package-lock.json", profile["cargo_lock"]]
    for relative in dict.fromkeys(paths):
        path = inside(runner.root, relative).relative_to(runner.root).as_posix()
        code, _ = runner.run(["git", "cat-file", "-e", source + ":" + path], check=False, quiet=True)
        if code:
            raise ReleaseError(f"Selected app profile does not match the release source: {path} is missing. Choose a configured profile for this application; bundled profiles are templates. No checkout or build was performed.")


def local_pipeline(request, events, stop=None, runner_factory=Runner, monitor=monitor_runs):
    profile = validate_local_request(request)
    token = request["token"]
    request["token"] = ""
    root = Path(request["local_repo"]).expanduser().resolve()
    runner = runner_factory(root, token, events, stop)
    full = request["build_mode"] == "local_release"
    repository, tag = request["repository"], request["tag"]
    version = tag[1:]
    restore = None
    runner.build_env = preflight_tools(root, profile)
    runner.emit("monitoring", "Local Windows pipeline started. Stop waits for the current command; completed pushes/publication are retained.")
    with repository_lock(runner):
        try:
            check_origin(runner, repository)
            runner.run(["gh", "auth", "status"])
            runner.api(repository, "")
            runner.run(["git", "fetch", "origin", "--tags"])
            existing_sha = remote_tag(runner, repository, tag)
            if full:
                if existing_sha is not None:
                    raise ReleaseError("New release tag already exists. Use the local Windows existing-tag mode; tags are never moved.")
                if runner.api(repository, "releases/tags/" + tag, missing=True) is not None:
                    raise ReleaseError("A release with this tag already exists.")
                check_profile_source(runner, profile, "HEAD")
                branch = request.get("ref") or profile["release_branch"].format(version=version)
                if branch == profile["default_branch"]:
                    raise ReleaseError("Choose a release branch different from the default branch, or leave it blank for release-X.Y.Z.")
                runner.run(["git", "check-ref-format", "--branch", branch])
                current = runner.text(["git", "branch", "--show-current"])
                if current != branch:
                    exists, _ = runner.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch], check=False, quiet=True)
                    runner.run(["git", "switch", branch] if exists == 0 else ["git", "switch", "-c", branch])
                previous = runner.api(repository, "releases/latest", missing=True)
                if previous:
                    old_tag = previous["tag_name"]
                    if re.fullmatch(r"v\d+\.\d+\.\d+", old_tag) and tuple(map(int, version.split("."))) <= tuple(map(int, old_tag[1:].split("."))):
                        raise ReleaseError("The new release must be newer than releases/latest.")
                    runner.run(["git", "log", "--oneline", previous["tag_name"] + "..HEAD"])
                    runner.run(["git", "diff", "--stat", previous["tag_name"] + "..HEAD"])
                notes = Path(request["notes_file"]).expanduser().read_text(encoding="utf-8")
                product = product_name(root, profile)
                if profile.get("install_notes"):
                    notes += "\n\n" + profile["install_notes"] + "\n"
                release_files = prepare_versions(root, profile, version, notes)
                for command in profile.get("lock_commands", []):
                    runner.run([part.replace("{version}", version).replace("{tag}", tag) for part in command])
                for command in profile.get("prepare_commands", []):
                    runner.run([part.replace("{version}", version).replace("{tag}", tag) for part in command])
                check_versions(root, profile, version)
                # Notes are generated outside the repository; credentials are never written.
                import tempfile
                notes_dir = Path(profile.get("notes_dir") or root.parent / ".release-tool-notes")
                if not notes_dir.is_absolute():
                    notes_dir = root / notes_dir
                notes_dir = notes_dir.resolve()
                if notes_dir.is_relative_to(root):
                    raise ReleaseError("Generated release notes must live outside the application repository.")
                notes_dir.mkdir(parents=True, exist_ok=True)
                notes_handle = tempfile.NamedTemporaryFile(mode="w", dir=notes_dir, suffix=".md", prefix="release-notes-", delete=False, encoding="utf-8")
                with notes_handle:
                    notes_handle.write(notes)
                notes_path = Path(notes_handle.name)
            else:
                if existing_sha is None:
                    raise ReleaseError("Local Windows existing-tag mode requires an existing tag.")
                existing_release = runner.api(repository, "releases/tags/" + tag, missing=True)
                if existing_release is None or existing_release.get("draft") or existing_release.get("prerelease"):
                    raise ReleaseError("Existing-tag Windows mode requires an existing published stable release page.")
                check_profile_source(runner, profile, existing_sha)
                original_branch = runner.text(["git", "branch", "--show-current"])
                original_sha = runner.text(["git", "rev-parse", "HEAD"])
                restore = ["git", "switch", original_branch] if original_branch else ["git", "switch", "--detach", original_sha]
                runner.run(["git", "switch", "--detach", existing_sha])
                sha = existing_sha
                for command in profile.get("setup_commands", [["npm", "ci"]] if profile.get("adapter", "tauri") == "tauri" else []):
                    runner.run([part.replace("{version}", version).replace("{tag}", tag) for part in command])
                check_versions(root, profile, version)
                product = product_name(root, profile)
            for command in profile["check_commands"]:
                runner.run([part.replace("{version}", version).replace("{tag}", tag) for part in command], retry=True)
            check_versions(root, profile, version)
            if full:
                if profile["mac_assets"]:
                    check_dispatch_workflow(runner, root, repository, request["mac_workflow"], "macOS")
                for item in profile["post_release_workflows"]:
                    check_dispatch_workflow(runner, root, repository, item["workflow"], item["workflow"])
                changed = set(runner.text(["git", "diff", "HEAD", "--name-only"]).splitlines())
                if changed - set(release_files):
                    raise ReleaseError("Preparation changed files outside the profile's release files: " + ", ".join(sorted(changed - set(release_files))))
                for file in release_files:
                    if not inside(root, file).is_file():
                        raise ReleaseError(f"Preparation did not produce the configured release file: {file}")
                runner.run(["git", "add", "--", *release_files])
                runner.run(["git", "commit", "--allow-empty", "-m", f"Release {version}", "-m", "Release versions and notes updated; configured release gates passed."])
                runner.run(["git", "push", "origin", branch])
                runner.run(["git", "switch", profile["default_branch"]])
                runner.run(["git", "pull", "--ff-only", "origin", profile["default_branch"]])
                runner.run(["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}: {product} {version}"])
                check_versions(root, profile, version)
                # Repeat gates only if incoming default-branch changes alter the tested tree.
                differs, _ = runner.run(["git", "diff", "--quiet", branch, "HEAD"], check=False, quiet=True)
                if differs not in (0, 1):
                    raise ReleaseError("Cannot compare the tested release tree to the merge result.")
                if differs:
                    runner.emit("log", "The default branch added changes to the merge; checking the merged tree.")
                    for command in profile["check_commands"]:
                        runner.run([part.replace("{version}", version).replace("{tag}", tag) for part in command], retry=True)
                runner.run(["git", "push", "origin", profile["default_branch"]])
                sha = runner.text(["git", "rev-parse", "HEAD"])
            runner.emit("log", f"Release source: {repository} / {tag} / {sha}")
            local_files, windows, mac = expected_assets(root, profile, product, version)
            before = {file: file.stat().st_mtime_ns if file.exists() else None for file in local_files}
            runner.emit("status", "Windows: building locally", workflow=request.get("windows_workflow", "release-windows.yml"), state="in_progress", conclusion=None)
            target_path = Path(profile.get("target_dir") or root / "target")
            if not target_path.is_absolute():
                target_path = root / target_path
            env_extra = {"CARGO_TARGET_DIR": str(target_path.resolve())} if profile.get("target_dir") and profile.get("adapter", "tauri") == "tauri" else None
            runner.run([part.replace("{version}", version).replace("{tag}", tag) for part in profile.get("build_command", ["npx", "tauri", "build"])], retry=True, env_extra=env_extra)
            for file in local_files:
                if not file.is_file() or file.stat().st_size == 0 or file.stat().st_mtime_ns == before[file]:
                    raise ReleaseError(f"Build did not produce a fresh nonempty installer: {file}")
            if runner.text(["git", "rev-parse", "HEAD"]) != sha or tracked_changes(runner):
                raise ReleaseError("The checkout changed during the build. Nothing will be published.")
            if remote_tag(runner, repository, tag) != (None if full else sha):
                raise ReleaseError("The release tag changed or was created by another process; publishing stopped.")
            if full:
                current_latest = runner.api(repository, "releases/latest", missing=True)
                if current_latest and re.fullmatch(r"v\d+\.\d+\.\d+", current_latest.get("tag_name", "")) and tuple(map(int, version.split("."))) <= tuple(map(int, current_latest["tag_name"][1:].split("."))):
                    raise ReleaseError("A newer release was published during the build. Publication stopped to avoid replacing releases/latest with an older version.")
                runner.run(["gh", "release", "create", tag, *map(str, local_files), "--repo", repository, "--target", sha,
                            "--title", f"{product} {version}", "--notes-file", str(notes_path), "--latest"])
            else:
                release = runner.api(repository, "releases/tags/" + tag, missing=True)
                if release is None:
                    raise ReleaseError("Existing-tag Windows mode needs an existing release page. It never creates or moves tags.")
                current = {asset["name"]: asset for asset in release.get("assets", [])}
                upload = []
                for file, name in zip(local_files, windows):
                    asset = current.get(name)
                    if asset:
                        digest = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
                        if asset.get("digest") == digest:
                            continue
                        if not profile.get("replace_windows_assets", False):
                            raise ReleaseError("A different Windows asset already exists. Set replace_windows_assets=true in the app profile to replace Windows assets explicitly.")
                    upload.append(str(file))
                if upload:
                    args = ["gh", "release", "upload", tag, *upload, "--repo", repository]
                    if profile.get("replace_windows_assets", False):
                        args.append("--clobber")
                    runner.run(args)
            verify_release(runner, repository, tag, sha, windows)
            runner.emit("status", "Windows: installers published", workflow=request.get("windows_workflow", "release-windows.yml"), state="completed", conclusion="success")
            runner.emit("link", "Release page", url=f"https://github.com/{repository}/releases/tag/{tag}")
            dispatches = ([(request["mac_workflow"], request["tag_input"], "macOS")] if mac else []) + [(item["workflow"], item["tag_input"], item["workflow"]) for item in profile["post_release_workflows"]]
            if full and dispatches:
                runs = []
                for workflow, tag_input, label in dispatches:
                    query = f"actions/workflows/{workflow}/runs?head_sha={sha}&per_page=100"
                    previous_ids = [run["id"] for run in runner.api(repository, query).get("workflow_runs", [])]
                    requested_at = datetime.now(timezone.utc).isoformat()
                    result = runner.api(repository, f"actions/workflows/{workflow}/dispatches", dict(ref=tag, inputs={tag_input: tag}))
                    runs.append(dict(workflow=workflow, head_sha=sha, tag=tag, requested_at=requested_at,
                                     previous_run_ids=previous_ids, run_id=result.get("workflow_run_id"), url=result.get("html_url", "")))
                    runner.emit("link", f"{label}: build requested", url=runs[-1]["url"])
                if not monitor(repository, token, runs, events, stop=runner.stop):
                    for run in runs:
                        if run.get("run_id"):
                            runner.emit("log", f"To diagnose: gh run view {run['run_id']} --repo {repository} --log-failed. For a temporary failure: gh run rerun {run['run_id']} --repo {repository} --failed.")
                    raise ReleaseError("A GitHub release build did not complete successfully. Windows assets remain published; no release was withdrawn.")
            if full:
                verify_release(runner, repository, tag, sha, windows + mac + signed_update_files(profile, product, version), latest=True)
                runner.run(["git", "fetch", "origin", "--tags"])
            if not full and restore:
                tracked_changes(runner)
                runner.run(restore, ignore_stop=True)
                restore = None
            runner.emit("done", f"Verified {tag} at {sha}: " + ", ".join(windows + mac + signed_update_files(profile, product, version) if full else windows))
            return True
        finally:
            if full and "notes_path" in locals():
                notes_path.unlink(missing_ok=True)
            if restore:
                try:
                    tracked_changes(runner)
                    runner.run(restore, ignore_stop=True)
                except Exception as exc:
                    runner.emit("error", f"Could not restore the original checkout: {exc}. Local files are retained; nothing is discarded.")


def run_local_release(request, events, stop=None):
    token = request.get("token", "")
    class SafeEvents:
        def put(self, event):
            events.put({key: value.replace(token, "[redacted]") if token and isinstance(value, str) else value for key, value in event.items()})
    try:
        success = local_pipeline(request, SafeEvents(), stop=stop)
        events.put(dict(kind="finished", success=success))
    except Exception as exc:
        events.put(dict(kind="error", message=str(exc).replace(token, "[redacted]") if token else str(exc)))
        events.put(dict(kind="finished", success=False))
    finally:
        request["token"] = ""
