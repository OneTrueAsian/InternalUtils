# GitHub Release Tool

A Windows desktop UI that uses `Start-GitHubRelease.ps1` to create a release tag
and request **both Windows and macOS** GitHub Actions workflows. Builds run on
GitHub, and your workflows publish the release assets.

## Features

- Light desktop layout with release details and workflow settings on separate tabs.
- Live target summary and individual Windows/macOS build status through completion.
- Windows-only recovery of existing tags using a separate workflow branch.
- Inline validation errors, scrollable activity log, clickable run links and log copying.
- Token visibility toggle that resets when starting, with no saved credentials.
- Hover/click help for every field, with keyboard activation and a bundled offline user guide.
- Configurable repository, branch, tag, workflow filenames and shared tag input name.
- Resume an existing tag at the same commit, without overwriting other tags.

## Launch

Requires Windows, Python 3.10+ with Tkinter (included with the standard Windows
Python installer), and Windows PowerShell 5.1 or PowerShell 7. No pip packages
are needed.

Keep `Start-GitHubRelease.ps1`, `release_ui.py`, `release_monitor.py`,
`ReleaseTool.pyw` and `help.html`
together. The launcher checks that the PowerShell script exists and can be read
before starting. After updating the checkout, restart any open utility window.

Double-click `ReleaseTool.pyw`, or run:

```powershell
python .\release-tool\ReleaseTool.pyw
```

Enter the target `owner/repo`, remote branch, release tag, and GitHub API token.
The repository field starts blank. A blank branch uses the target repo's default
branch. The **Workflow settings** tab contains editable filenames, initially
`release-windows.yml` and `build-macos.yml`, and the tag input name.
Click **Build release**; follow the clickable run links or use **View builds**
and **View release**. A live target summary lets you check the repository, branch
and tag before starting. After dispatch, the UI polls the exact Windows and macOS runs every 15 seconds
until they complete. In normal mode, success requires both workflows; recovery
mode requires only Windows.
Failures and cancellations show their run links, failed job/step names and
short redacted error excerpts when available.
**Stop monitoring** stops local polling while GitHub builds continue. **Copy log** copies the redacted activity log.

Click **Help & setup** in the toolbar to open the bundled [user guide](help.html) in your
browser. It covers setup, token permissions, every form field, a release example,
retries and troubleshooting. The guide also works offline.

The UI clears the token field when starting. A token copy remains in worker memory
while checking build status and is released when monitoring ends or the app closes. Credentials go to PowerShell via a
private stdin pipe, never command-line arguments, saved preferences or files.
The log redacts the supplied token. Credentials still exist briefly in process
memory; Python strings cannot guarantee secure erasure.

## Target repository setup

This utility orchestrates existing workflows. It does not install workflows or
compile the target app itself. Each target workflow must:

- Exist on GitHub's default branch and at the selected remote commit.
- Be enabled, and declare `workflow_dispatch:` in block YAML format.
- Use manual dispatch only for publishing; remove `push` triggers on both branches.
- Accept an input named `tag` (or the input name you enter), or accept no inputs
  when the UI's **Tag input name** is blank.
- Build the supplied tag and create/update the corresponding GitHub Release.

For example, `release-windows.yml` and `build-macos.yml` can both accept a
`tag` input. Choose workflows that publish release assets. A build-check workflow
that only uploads test artifacts will not publish a release.

## Release prerequisites

Before clicking **Build release**:

1. Commit and push both release workflow files under `.github/workflows/` to the
   app repository's default branch and selected release branch. Local files alone
   do not register a workflow on GitHub. Use the exact published filenames in the UI.
2. Confirm both workflows are enabled in GitHub Actions, support `workflow_dispatch`,
   and accept the selected tag input name (or no inputs when that field is blank).
3. Where the workflow requires matching versions, align the release tag and app
   version: `v1.0.0` corresponds to `1.0.0`. For Tauri, update `package.json`,
   `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, and the app version entries
   in `package-lock.json` and `Cargo.lock`. Naming a branch does not bump its version.
4. Add the new release notes/What's New entry, update any release-candidate checks,
   and pass the app repository's required release tests.
5. Commit and push the prepared release branch, then select it in the UI. The default
   branch may still have an older app version; select the branch you intend to release.
6. Verify token access and use a new tag, or an existing tag at the same commit.
   A tag created before these prerequisites were committed will not include them.

The tool does not install workflows, update versions or write release notes.
The **Help & setup** page includes this checklist and troubleshooting for missing
workflows, version mismatches and release metadata failures.

GitHub's requirement for workflows on the default branch is documented in
[Manually running a workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

Create a fine-grained personal access token with access to the **target** repository
with **Contents: read/write** and **Actions: read/write**. GitHub may also require
**Workflows: read/write** for reference creation involving workflow files; see
[Git references API permissions](https://docs.github.com/en/rest/git/refs#create-a-reference).
For multiple apps, you can select more repositories or all repositories owned by
the token's selected resource owner. For a classic token, use `repo` (and `workflow` where required). Organization
tokens may need additional owner approval or SSO authorization.

## Tag and retry behavior

In normal mode, the tool tags the current commit of the selected **remote** branch. It does not
commit, push, bump versions or include local changes. Set and commit the app's
version before launching the release. Publication, draft/prerelease settings,
signing and test gates remain controlled by the target workflows.

Existing tags pointing to the same commit are reusable; conflicting tags are
rejected and never moved or deleted. Annotated tags are resolved to their commit.
Both workflows are checked before creating the tag. If one dispatch fails, the
other is still attempted, and the tag remains available for retry.

Before each dispatch, the tool checks for an existing queued/running/successful
push or manual run for that exact tag and commit and reuses its link. Detection
requires manual-only publishing workflows to prevent delayed tag-push runs from
racing the dispatch. Avoid starting the same release from two clients simultaneously.
Failed/cancelled runs can be requested again by retrying with the same tag and
original branch commit. Retrying a failed release may dispatch failed workflows again.

“Build requested” means dispatch succeeded. The desktop UI then checks completion;
“Both builds completed successfully” requires both run conclusions to be `success`.
Monitoring stops after two hours, or after three consecutive status-read failures
for a run; those results are reported as unknown, not successful.
The CLI dispatches workflows and prints structured run metadata; live monitoring
is provided by the desktop UI. A release page may initially be absent. The UI never sends a token
to build runners; the workflows use their own `GITHUB_TOKEN`/repository secrets.

## PowerShell CLI

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release-tool\Start-GitHubRelease.ps1 -Repository owner/repo -Ref main -Tag v1.2.3
```

The token is requested with a masked prompt. The execution-policy flag applies
only to this process. Output is newline-delimited JSON, also consumed by the UI.

## Offline checks

```powershell
python -m unittest discover -s release-tool/tests -v
```

These checks exercise the real PowerShell script through a fake GitHub transport,
credential handling, tag conflicts/resumption, both dispatches, automatic macOS
runs, partial failures, inline validation, live summaries, token visibility,
redacted log copying and Tkinter form state. They never contact GitHub or start
a build. A real GitHub release run is required to validate the target repo's build
environment and packaging workflow.

## Retries and failure diagnostics

Publishing workflows must use manual-only workflow_dispatch on both default and source branches. Remove tag push triggers to avoid duplicate publishing. Retrying a failed tag builds the same commit: commit code fixes, update versions and release notes, and select a new tag. Existing tags and assets are preserved. Monitoring tolerates 60 seconds of clock skew and excludes runs seen before dispatch. Failure diagnostics include redacted GitHub error details and short job-log excerpts when available. Actions read permission covers these checks.

## Windows-only recovery for an existing release

Select **Windows only · existing tag** in the Build selector. The branch field
becomes **Workflow branch**: enter the branch containing the updated Windows
workflow (usually `main`). The **Release tag** is the existing application tag,
for example `v1.0.0`; it can point to a different commit from the workflow branch.
macOS settings are disabled, macOS is not dispatched, and success requires only
the selected Windows build. The tag is never created, moved or deleted in this mode.

The Windows workflow must be registered on the default branch and exist on the
workflow branch. It must accept the selected tag input plus boolean `recovery`
and string `source_sha`. It must name recovery runs `Recover Windows <tag>` using
`inputs.tag` so monitoring can distinguish releases dispatched on the same branch.
The tool supplies the original tag SHA. The workflow must check out that tag,
verify its commit equals `source_sha`, retain version and release gates, and
publish only Windows installers to the corresponding existing release.

Vault Spend's updated `release-windows.yml` supports this contract. It repairs
only the hardcoded driver directory in an old E2E harness inside the runner;
application sources stay at the original tag. All test gates still run. This
mode cannot incorporate application bug fixes into an old release tag.

```text
Build:             Windows only · existing tag
Repository:        owner/app-repo
Workflow branch:   main
Release tag:       v1.0.0
Windows workflow:  release-windows.yml
Tag input name:    tag
```

Publish the updated workflow to the workflow/default branch before using
recovery. A workflow present only locally cannot be dispatched. Normal
**Windows + macOS** mode retains the original branch/tag matching rules.

CLI recovery: `powershell -NoProfile -ExecutionPolicy Bypass -File .\release-tool\Start-GitHubRelease.ps1 -Repository owner/app-repo -Ref main -Tag v1.0.0 -BuildMode windows_recovery`. The token is prompted securely.
