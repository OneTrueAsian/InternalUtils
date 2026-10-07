"""Local Windows release pipeline described in RELEASE-PROCESS.md.

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

HERE = Path(__file__).resolve().parent
LOCAL_MODES = ("local_release", "local_windows")


class ReleaseError(RuntimeError):
    pass


class CommandError(ReleaseError):
    pass


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReleaseError(f"Profile path escapes the repository: {relative}")
    return path


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
    adapter = profile.setdefault("adapter", "tauri")
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


def package_section(text):
    match = re.search(r"(?ms)^\[package\]\s*\n(.*?)(?=^\[|\Z)", text)
    if not match:
        raise ReleaseError("Cargo manifest requires a [package] section.")
    return match


def cargo_field(text, field):
    match = re.search(rf'^\s*{field}\s*=\s*"([^"\n]+)"', package_section(text).group(1), re.M)
    if not match:
        raise ReleaseError(f"Cargo package requires a literal {field} field.")
    return match.group(1)


def product_name(root, profile):
    if profile.get("product_name"):
        return profile["product_name"]
    return json.loads(inside(root, "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["productName"]


def custom_versions(root, profile, version=None):
    """Read/prepare explicit nested JSON properties; validate every edit before writing."""
    edits, values = {}, []
    for item in profile["version_files"]:
        path = inside(root, item["file"])
        try:
            data = edits.get(path) or json.loads(path.read_text(encoding="utf-8"))
            for key in item["keys"]:
                parent = data
                for part in key[:-1]:
                    parent = parent[part]
                current = parent[key[-1]]
                if not isinstance(current, str) or not re.fullmatch(r"\d+\.\d+\.\d+", current):
                    raise ValueError()
                values.append(current)
                if version is not None:
                    parent[key[-1]] = version
            edits[path] = data
        except (OSError, ValueError, KeyError, TypeError):
            raise ReleaseError(f"Cannot read stable version properties in {item['file']}.") from None
    if version is not None:
        for path, data in edits.items():
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return values


def check_versions(root, profile, version, locks=True):
    if profile.get("adapter") == "custom":
        if any(value != version for value in custom_versions(root, profile)):
            raise ReleaseError("Version mismatch in configured version_files.")
        return
    for file in ("package.json", "src-tauri/tauri.conf.json"):
        if json.loads(inside(root, file).read_text(encoding="utf-8"))["version"] != version:
            raise ReleaseError(f"Version mismatch in {file}.")
    manifest = inside(root, profile["cargo_manifest"]).read_text(encoding="utf-8")
    if cargo_field(manifest, "version") != version:
        raise ReleaseError("Rust package version mismatch.")
    if locks:
        npm = json.loads(inside(root, "package-lock.json").read_text(encoding="utf-8"))
        if npm.get("version") != version or npm.get("packages", {}).get("", {}).get("version") != version:
            raise ReleaseError("package-lock.json version mismatch.")
        crate = profile.get("cargo_crate") or cargo_field(manifest, "name")
        cargo = inside(root, profile["cargo_lock"]).read_text(encoding="utf-8")
        packages = re.split(r"(?m)^\[\[package\]\]", cargo)
        found = [entry for entry in packages if re.search(rf'(?m)^name = "{re.escape(crate)}"$', entry)]
        if len(found) != 1 or not re.search(rf'(?m)^version = "{re.escape(version)}"$', found[0]):
            raise ReleaseError(f"Cargo.lock version mismatch for {crate}.")


def notes_entries(notes):
    entries = []
    collect = False
    for line in notes.splitlines():
        if line.startswith("## "):
            collect = line[3:].strip().lower() in ("what's new", "what’s new", "fixes and improvements")
        elif collect and re.match(r"^[-*] ", line):
            entries.append(re.sub(r"[*`]", "", line[2:]).strip())
    if not entries:
        raise ReleaseError("Release notes need bullets under What's new or Fixes and improvements for the in-app changelog.")
    return entries


def changelog_entry(source, version):
    match = re.search(rf'"{re.escape(version)}"\s*:\s*\[', source)
    if not match:
        return None
    start = match.end() - 1
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth == 0:
                try:
                    notes = json.loads(re.sub(r",\s*\]$", "]", source[start:index + 1]))
                    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
                        raise ValueError()
                    return notes
                except ValueError:
                    raise ReleaseError("Changelog entries must be literal JSON-style string arrays; configure explicit preparation hooks for another layout.") from None
    raise ReleaseError("Changelog entry has an unclosed array.")


def release_paths(root, paths):
    return list(dict.fromkeys(inside(root, path).relative_to(root).as_posix() for path in paths))


def prepare_versions(root, profile, version, notes):
    if profile.get("adapter") == "custom":
        current = custom_versions(root, profile)
        if len(set(current)) != 1:
            raise ReleaseError("Configured version files disagree; align them before releasing.")
        if tuple(map(int, version.split('.'))) < tuple(map(int, current[0].split('.'))):
            raise ReleaseError("A new release version must not be older than the current application version.")
        paths = release_paths(root, [item["file"] for item in profile["version_files"]] + profile.get("additional_release_files", []))
        custom_versions(root, profile, version)
        return paths
    paths = ["package.json", "src-tauri/tauri.conf.json", profile["cargo_manifest"], "package-lock.json", profile["cargo_lock"]]
    old = json.loads(inside(root, "package.json").read_text(encoding="utf-8"))["version"]
    if old == version:
        check_versions(root, profile, version, locks=False)
        if profile.get("changelog_file"):
            entries = notes_entries(notes)
            source = inside(root, profile["changelog_file"]).read_text(encoding="utf-8")
            if changelog_entry(source, version) != entries:
                raise ReleaseError("This version is already prepared, but its in-app notes differ from the supplied release notes. Align them before retrying.")
            paths.append(profile["changelog_file"])
        if profile.get("metadata_test"):
            source = inside(root, profile["metadata_test"]).read_text(encoding="utf-8")
            if not re.search(rf'const CANDIDATE = "{re.escape(version)}";', source):
                raise ReleaseError("Prepared release candidate metadata does not match this version.")
            paths.append(profile["metadata_test"])
        return release_paths(root, paths + profile.get("additional_release_files", []))
    if tuple(map(int, version.split('.'))) <= tuple(map(int, old.split('.'))):
        raise ReleaseError("A new release version must be greater than the current application version.")
    edits = {}
    for file in paths[:2]:
        path = inside(root, file)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = version
        edits[path] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path = inside(root, profile["cargo_manifest"])
    source = path.read_text(encoding="utf-8")
    section = package_section(source)
    body, count = re.subn(r'(?m)^(\s*version\s*=\s*)"[^"]+"', lambda m: m.group(1) + json.dumps(version), section.group(1), count=1)
    if count != 1:
        raise ReleaseError("Cannot locate the Rust package version.")
    edits[path] = source[:section.start(1)] + body + source[section.end(1):]
    if profile.get("changelog_file"):
        entries = notes_entries(notes)
        path = inside(root, profile["changelog_file"])
        source = path.read_text(encoding="utf-8")
        if re.search(rf'"{re.escape(version)}"\s*:', source):
            raise ReleaseError("The new version already has a changelog entry; review it before retrying.")
        previous_notes = changelog_entry(source, old)
        if previous_notes is None:
            raise ReleaseError("Cannot find the previous release's changelog entry.")
        marker = source.rfind("};")
        if marker < 0:
            raise ReleaseError("Unsupported changelog structure; configure prepare_commands for this app.")
        new_entry = "  " + json.dumps(version) + ": " + json.dumps(entries, ensure_ascii=False, indent=2).replace("\n", "\n  ") + ",\n"
        edits[path] = source[:marker] + new_entry + source[marker:]
        paths.append(profile["changelog_file"])
        if profile.get("metadata_test"):
            path = inside(root, profile["metadata_test"])
            source = path.read_text(encoding="utf-8")
            source, count = re.subn(r'const CANDIDATE = "[^"]+";', lambda _: "const CANDIDATE = " + json.dumps(version) + ";", source, count=1)
            if count != 1:
                raise ReleaseError("Cannot find CANDIDATE in the release-metadata test.")
            pattern = r'  it\(`is the \$\{CANDIDATE\}.*?\n  \}\);'
            replacement = '  it(`is the ${CANDIDATE} release candidate and its notes cover what shipped`, () => {\n    expect(pkg).toBe(CANDIDATE);\n    expect(CHANGELOG[CANDIDATE]).toEqual(' + json.dumps(entries, ensure_ascii=False) + ');\n  });'
            source, count = re.subn(pattern, lambda _: replacement, source, count=1, flags=re.S)
            if count != 1:
                raise ReleaseError("Unsupported candidate test structure; configure prepare_commands for this app.")
            marker = source.rfind("\n});")
            if marker < 0:
                raise ReleaseError("Cannot find release-metadata test's describe block.")
            preserve = '\n  it("preserves the ' + old + ' release notes", () => {\n    expect(CHANGELOG[' + json.dumps(old) + ']).toEqual(' + json.dumps(previous_notes, ensure_ascii=False) + ');\n  });\n'
            edits[path] = source[:marker] + preserve + source[marker:]
            paths.append(profile["metadata_test"])
    for path, content in edits.items():
        path.write_text(content, encoding="utf-8")
    return release_paths(root, paths + profile.get("additional_release_files", []))


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


def check_origin(runner, repository):
    origin = runner.text(["git", "remote", "get-url", "origin"])
    name = re.sub(r"\.git$", "", origin.rstrip("/"))
    if name.lower() not in (f"https://github.com/{repository}".lower(), f"git@github.com:{repository}".lower(), f"ssh://git@github.com/{repository}".lower()):
        raise ReleaseError("Local origin does not match the repository entered in the tool.")
    if runner.text(["git", "status", "--porcelain", "--untracked-files=no"]):
        raise ReleaseError("The application repository has tracked changes. Commit or resolve them before starting.")


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
                if profile.get("unsigned", profile.get("adapter", "tauri") == "tauri") and "## Install notes" not in notes:
                    notes += "\n\n## Install notes\n\nBoth builds are unsigned:\n- **Windows**: SmartScreen may warn — click More info, then Run anyway.\n- **macOS**: right-click the app and choose Open, or run `xattr -dr com.apple.quarantine \"/Applications/" + product + ".app\"`.\n"
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
                    workflow = inside(root, ".github/workflows/" + request["mac_workflow"]).read_text(encoding="utf-8")
                    if not re.search(r"(?m)^\s*workflow_dispatch\s*:", workflow) or re.search(r"(?m)^\s+push\s*:", workflow):
                        raise ReleaseError("macOS publishing must be manual-only workflow_dispatch.")
                    if runner.api(repository, "actions/workflows/" + request["mac_workflow"]).get("state") != "active":
                        raise ReleaseError("macOS workflow must be registered and enabled on the default branch.")
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
            if runner.text(["git", "rev-parse", "HEAD"]) != sha or runner.text(["git", "status", "--porcelain", "--untracked-files=no"]):
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
            if full and mac:
                query = f"actions/workflows/{request['mac_workflow']}/runs?head_sha={sha}&per_page=100"
                previous_ids = [run["id"] for run in runner.api(repository, query).get("workflow_runs", [])]
                requested_at = datetime.now(timezone.utc).isoformat()
                result = runner.api(repository, f"actions/workflows/{request['mac_workflow']}/dispatches", dict(ref=tag, inputs={request["tag_input"]: tag}))
                run = dict(workflow=request["mac_workflow"], head_sha=sha, tag=tag, requested_at=requested_at,
                           previous_run_ids=previous_ids, run_id=result.get("workflow_run_id"), url=result.get("html_url", ""))
                runner.emit("link", "macOS: build requested", url=run["url"])
                if not monitor(repository, token, [run], events, stop=runner.stop):
                    if run.get("run_id"):
                        runner.emit("log", f"To diagnose: gh run view {run['run_id']} --repo {repository} --log-failed. For a temporary failure: gh run rerun {run['run_id']} --repo {repository} --failed.")
                    raise ReleaseError("macOS build did not complete successfully. Windows assets remain published; no release was withdrawn.")
            if full:
                verify_release(runner, repository, tag, sha, windows + mac, latest=True)
                runner.run(["git", "fetch", "origin", "--tags"])
            if not full and restore:
                runner.run(restore, ignore_stop=True)
                restore = None
            runner.emit("done", f"Verified {tag} at {sha}: " + ", ".join(windows + mac if full else windows))
            return True
        finally:
            if full and "notes_path" in locals():
                notes_path.unlink(missing_ok=True)
            if restore:
                try:
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
