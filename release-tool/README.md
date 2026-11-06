# GitHub Release Tool

Prepare, build and publish GitHub releases for repositories using a per-application JSON profile. The tool provides release orchestration; each application supplies its build scripts, tests, version metadata and artifact names.

## Setup

Run on Windows with Python 3.10+ (including Tkinter), PowerShell, Git and GitHub CLI. Install only the additional build tools required by your selected profile. Node, Rust, Tauri, Perl, installer formats and signing rules are not universal requirements.

```powershell
python .\release-tool\ReleaseTool.pyw
```

Copy `profiles/custom-app.json` to a profile for your application and edit its settings. The bundled Python build/test commands are examples, not scripts supplied for your app. `profiles/tauri-generic.json` is an optional starting point for Tauri apps.

## Create a release

For `OneTrueAsian/vault-spend`, versions `v1.3.90` to `v1.3.99` are reserved for tests. Every mode (remote, recovery, draft and local) refuses them before any tag, workflow or local file is touched, including spellings with leading zeros such as `v1.3.095`. If `releases/latest` ever points at one, the local pipeline stops instead of comparing against it. Other applications can use those versions.

### Vault Spend: hosted Windows draft

For Vault Spend's sharded pipeline, select **Windows: sharded draft** in the Build dropdown. Enter `OneTrueAsian/vault-spend`, the pushed release branch, its production `vX.Y.Z` tag, and your token. Leave **Windows workflow** as `release-windows.yml` and **Tag input** as `tag`. Version metadata must already match the tag. Local folder/profile/notes fields are not used in this mode.

The tool pins `source_sha` to the pushed commit and dispatches `sharded=true`, `test_release=false`, `diagnostic_only=false`, and `recovery=false`. **Windows: serial draft** sends `sharded=false` as the fallback. Reserved test tags `v1.3.90`–`v1.3.99` are refused. These modes require a compatible draft-only workflow on the selected source and a registered workflow on the default branch.

Activity shows build, Rust checks, other checks, six shards, aggregation, packaging and signing job/step changes. The monitor waits through GitHub's protected signing approval; approve it on GitHub when ready. GitHub's run status can lag behind its jobs, so a run that reports success while any job still shows as running is not accepted: Activity names the jobs it is waiting for, once, and keeps checking (if they never settle, monitoring ends as 'not confirmed' with a link to the run, never as success). For the sharded draft the run's own gate summary (the `sharded-gate-summary-<attempt>` artifact) must also exist, unexpired and nonempty, for that exact run, attempt and commit. A required job that was skipped, cancelled or failed is never accepted, and nothing is ever re-run or cancelled by the tool. A successful run is accepted only after all required jobs succeeded and the authenticated draft contains nonempty EXE/MSI installers and their manifest/signature companions at the requested tag commit. Vault Spend filenames must match the requested version. This is a metadata/presence check, not independent cryptographic verification of downloaded assets; the workflow owns digest/signature verification.

Completion says **Windows draft ready for review · not published**. Publication remains a separate owner action. The Windows-only draft modes never dispatch macOS. The legacy **Windows + macOS** mode still refuses to pair a draft-producing Windows workflow with macOS, because macOS must not start alongside Windows: use the complete-draft modes below.

### Vault Spend: complete draft (Windows first, then macOS)

Select **Windows + macOS: sharded draft** (or **Windows + macOS: serial draft**), and fill **macOS workflow** (`build-macos.yml`) beside the Windows workflow. The tool inspects the macOS workflow at the source commit and refuses, before any tag is created, unless it follows the draft policy: it joins the existing draft by release id, keeps it a draft, and runs the release check (Vault Spend's `build-macos.yml` after VS-20). A workflow that creates or publishes its own release is never dispatched against a draft.

What happens, in order, and what Activity and the status line say at each point:

1. **Building**: the Windows gate, packaging and signing run, with the same job tracking, gate-summary evidence and sanitization as the Windows-only draft modes.
2. **Waiting for signing approval**: a protected job is waiting. Approve the `release-signing` environment yourself on GitHub; the tool only shows the wait and never approves, bypasses or automates it.
3. **Windows draft ready · not published**: only after the Windows run succeeded with every required job and the draft was read again with authentication: exactly one unpublished production release for the tag, at the tested source commit, holding Windows' installers with nonempty signed manifests and signatures. The release id is recorded.
4. **macOS running**: only then is macOS dispatched, for the same tag, source commit and recorded release id. It builds, then waits for its own signing approval (shown the same way).
5. **Complete draft ready for review · not published**: only after the macOS run succeeded with both of its jobs and the draft was read again: the same release id, still a draft, Windows' files plus the macOS DMG, manifest and signature, all nonempty.

If Windows fails, macOS is not requested. If any run cannot be read or monitoring times out or is stopped, the result is shown as **not confirmed** with a link, never as success or as a ready draft. A green run whose draft does not verify (partial files, another release, a moved tag) is a failure. Nothing here publishes, approves, deletes, replaces or cancels anything.

**Recovery.** Run the same mode again; the tool reconciles what exists before dispatching anything. A complete Windows draft resumes at macOS without running Windows again. A complete draft is only verified. macOS files already present are never dispatched again and never replaced; partial macOS files, a published release, several releases for the tag, a moved tag or a branch that no longer points at the tag stop the run with the reason and the safe next step (inspect the run on GitHub; remove partial macOS files yourself if you decide to). An active macOS run is tracked, not duplicated, and a completed green macOS run that left no macOS files is reported, not repeated.

### Local: Windows-only unsigned draft

**Local: Windows-only unsigned draft** (mode `local_draft`) runs the same preparation, gates, commit/merge/push and local Windows build as **Local: full release**, then creates an **unpublished draft** instead of publishing. GitHub creates the tag only when you publish. The tool verifies it as a draft (one unpublished production release at the recorded commit holding exactly the configured nonempty assets; no tag or a tag at that commit; `releases/latest` is not this version) and says **Windows-only unsigned draft ready · not published**. That check is metadata inspection (file names, sizes, target commit, draft state and tag state read from GitHub); it does not verify any signature or digest. macOS and post-release workflows are not requested in this mode: they need the tag, and Vault Spend's macOS step needs Windows' signed set from the hosted pipeline. Use the hosted complete-draft mode for a draft with macOS. The installers are built on this computer, so the draft has **no signing manifests or signatures** from the hosted pipeline: it is a Windows-only unsigned draft, not a complete release, and is not eligible for macOS. **Local: full release** is unchanged and still publishes by design.

> **Vault Spend restriction (VS-21 audit; owner decision of 6 November 2026, implemented).** For `OneTrueAsian/vault-spend` the tool refuses **Local: full release** and **Local: Windows existing tag**; other applications and the labelled **Local: Windows-only unsigned draft** are unchanged. The reason: **Local: full release** publishes the locally built installers as `releases/latest` immediately (`gh release create ... --latest`). Vault Spend's `release-windows.yml` refuses a tag that already has a release, so the signed Windows files (manifests and signatures) can never be added afterwards; if the profile lists them in `signed_update_assets` the tool then reports failure only after the release is public, and if it does not list them the tool reports success for a release that lacks them. **Local: Windows existing tag** would add unsigned installers to a published release. Use the hosted complete-draft modes for Vault Spend.

Draft dispatches do not reuse an old same-commit workflow run, whose inputs may have selected a different gate. Diagnose failures before retrying. A populated existing draft may be refused by the workflow; inspect it and select a new version/source as appropriate. The tool never deletes drafts, replaces assets or moves tags to recover automatically.

### Generic local release

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
| `post_release_workflows` | Extra manual-only workflows to dispatch against the new tag after publication, as `{"workflow": "file.yml", "tag_input": "tag"}`; they are monitored with the macOS build. Defaults to none |
| `signed_update_assets` | Exact release filenames (same placeholders as `mac_assets`) that must each have the files named by `signed_update_suffixes` before the release counts as complete. Defaults to none |
| `signed_update_suffixes` | Suffixes of those companion files; defaults to `.manifest.json` and `.signature.json` |

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

Every remote build the tool starts (Windows and macOS, in every build mode, and the local pipeline's macOS and signing builds) also shows its job and step progress in Activity: one line when a job or step starts, and one line when it finishes with how long it took, never repeated. Steps that have not started are not listed. This is step progress only, by the owner's choice: running build output is not streamed (GitHub does not stream it reliably), and a failed job's log is read once, after the failure, for up to 10 error lines. Every displayed name and log line has terminal control sequences removed and the token redacted. If GitHub cannot list the steps, Activity says so once and the run status is still monitored.

Activity records commands, failures and build links. **Stop after step** waits for the current command and stops before the next; it retains pushed commits and published assets. Stopping workflow monitoring does not cancel GitHub jobs.

**Local: Windows existing tag** builds the existing tag's commit and adds the profile's local artifacts to an existing stable release, restoring the original checkout afterward. It preserves the tag, release notes and other assets. Different existing artifacts are rejected unless replacement is explicitly enabled in the profile.

If a gate fails twice, fix it before retrying. If publication succeeded but an optional workflow failed, inspect or rerun that workflow; do not run new-release mode again with an existing tag. The tool never moves tags or withdraws releases automatically. See [the general release process](RELEASE-PROCESS.md) and [offline Help](help.html).

Current limits: local builds run on Windows; local-release tags use stable `vX.Y.Z`; optional remote publishing is exposed as a macOS workflow. Profiles adapt commands and assets but do not create application build systems or deploy to web/cloud environments.

## Verification

From Git Bash, in the InternalUtils repository root (the command used for the recorded evidence):

```
cd /e/misc/Programming/InternalUtils && python -B -m unittest discover -s release-tool/tests -v
```

From PowerShell: `python -B -m unittest discover -s release-tool/tests -v`. The tool starts Windows PowerShell 5.1 without a PowerShell 7 `PSModulePath` (`release_ui.powershell_environment`), so this also works from a PowerShell 7 session; before that fix the dispatcher tests failed there with "module Microsoft.PowerShell.Security could not be loaded".

Tests use temporary fixtures and fake GitHub responses. They verify orchestration, metadata adapters, asset checks, failure handling and UI behavior; they do not publish a release or validate your application's build environment.
