"""Version preparation adapters. Optional Tauri metadata support is not a tool prerequisite."""
import json
import re
from release_errors import ReleaseError, inside

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


