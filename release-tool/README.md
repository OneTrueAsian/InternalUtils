# GitHub Release Tool

Prepare, build and publish GitHub releases for repositories using a per-application JSON profile. The tool provides release orchestration; each application supplies its build scripts, tests, version metadata and artifact names.

## Setup

Run on Windows with Python 3.10+ (including Tkinter), PowerShell, Git and GitHub CLI. Install only the additional build tools required by your selected profile. Node, Rust, Tauri, Perl, installer formats and signing rules are not universal requirements.

```powershell
python .\release-tool\ReleaseTool.pyw
```

Copy `profiles/custom-app.json` to a profile for your application and edit its settings. The bundled Python build/test commands are examples, not scripts supplied for your app. `profiles/tauri-generic.json` is an optional starting point for Tauri apps.

## Create a release

1. Commit application code changes before starting; tracked files must be clean.
2. Enter `owner/app-repo`, a new stable tag such as `v1.0.1`, and your GitHub token.
3. Select **Local: full release**, the application checkout, your customized profile, and a nonempty Markdown release-notes file.
4. If the profile declares macOS assets, enter its publishing workflow filename and tag input. Otherwise these fields are disabled.
5. Click **Build release**. This authorizes the configured preparation/checks, release commit, merge/push, build and publication.

The tool checks the repository origin, compares with the previous release, updates configured version metadata, runs configured preparation and validation commands, merges the release branch into the configured default branch, and builds locally. It publishes the exact artifacts at the recorded commit SHA. When configured, it dispatches and monitors the macOS publishing workflow. Success requires the configured nonempty assets, a stable published release, the correct tag commit and `releases/latest`.

No E2E gate, changelog format, unsigned-install warning or fixed asset count is imposed on custom profiles. Configure the release checks and install notes appropriate to your application.

## Application profiles

| Setting | Purpose |
| --- | --- |
| `adapter` | `custom` for explicit JSON metadata; `tauri` for optional standard Tauri metadata support |
| `product_name` | Application name used in release titles and artifact templates |
| `default_branch` | Branch receiving the release merge, such as `main` or `trunk` |
| `release_branch` | Release branch template, for example `release-{version}` |
| `version_files` | Custom adapter JSON files and property paths to update and verify |
| `required_tools` | Additional executable names needed by your build system; Git and GitHub CLI are always checked |
| `lock_commands` | New-release dependency/lock preparation commands |
| `setup_commands` | Dependency setup for existing-tag builds |
| `prepare_commands` | Application-specific version/changelog preparation hooks |
| `check_commands` | Required release gates; a failing command is retried once, then stops the pipeline |
| `build_command` | Local build command as an argument array |
| `additional_release_files` | Extra generated files eligible for the scoped release commit |
| `target_dir` | Artifact output directory; asset paths are relative to it |
| `windows_assets` | Exact local artifact paths; any positive count is supported |
| `mac_assets` | Expected remote macOS asset names; `[]` disables macOS dispatch |
| `install_notes` | Optional app-specific text appended to the supplied release notes |
| `replace_windows_assets` | Explicit permission to replace different existing local-build assets; defaults to false |

Custom version metadata supports multiple JSON files and nested properties:

```json
"version_files": [
  {"file": "config/app.json", "keys": [["release", "version"]]},
  {"file": "package-lock.json", "keys": [["version"], ["packages", "", "version"]]}
]
```

For XML/TOML/plain-text versions, use a JSON release manifest as the authoritative version and configure preparation commands to update the other files. Include their paths in `additional_release_files` and configure checks that verify consistency. The optional Tauri adapter handles its standard package/config/Cargo metadata; its dependencies and metadata conventions apply only when explicitly selected.

Commands are arrays, for example `["dotnet", "publish", "-c", "Release"]` or `["python", "scripts/build.py", "--version", "{version}"]`. They run from the application root. `{version}` and `{tag}` are substituted as data, without evaluating shell expressions. Artifact templates support `{product}`, `{version}` and `{architecture}`. Wildcards, escaping paths and duplicate release asset names are rejected.

Use trusted profiles and repositories: their commands execute locally. Profiles must not contain credentials. The token remains in memory, is supplied only to Git/GitHub CLI or the remote dispatch script, and is redacted from activity logs.

## GitHub workflows and permissions

A classic token needs repository access (`repo`) and workflow access (`workflow`). Fine-grained tokens need Contents and Actions read/write on each target repository; updating workflow files may require Workflows write. Organization policies may require approval or SSO authorization.

An optional macOS publishing workflow must exist on the default branch and the release source, use manual-only `workflow_dispatch`, accept the configured tag input, and publish the declared assets to that tag's release. No workflow files or build scripts are generated automatically.

The **Windows + macOS** mode dispatches the two configured GitHub workflows instead of building locally. Each workflow defines its own tests and publication behavior. **Windows only · existing tag** is a legacy recovery mode requiring a compatible workflow with `recovery` and `source_sha` inputs and recovery run naming; it is not a prerequisite for normal releases.

## Results and recovery

Activity records commands, failures and build links. **Stop after step** waits for the current command and stops before the next; it retains pushed commits and published assets. Stopping workflow monitoring does not cancel GitHub jobs.

**Local: Windows existing tag** builds the existing tag's commit and adds the profile's local artifacts to an existing stable release, restoring the original checkout afterward. It preserves the tag, release notes and other assets. Different existing artifacts are rejected unless replacement is explicitly enabled in the profile.

If a gate fails twice, fix it before retrying. If publication succeeded but an optional workflow failed, inspect or rerun that workflow; do not run new-release mode again with an existing tag. The tool never moves tags or withdraws releases automatically. See [the general release process](RELEASE-PROCESS.md) and [offline Help](help.html).

Current limits: local builds run on Windows; local-release tags use stable `vX.Y.Z`; optional remote publishing is exposed as a macOS workflow. Profiles adapt commands and assets but do not create application build systems or deploy to web/cloud environments.

## Verification

```powershell
python -m unittest discover -s release-tool/tests -v
```

Tests use temporary fixtures and fake GitHub responses. They verify orchestration, metadata adapters, asset checks, failure handling and UI behavior; they do not publish a release or validate your application's build environment.
