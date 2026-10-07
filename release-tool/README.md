# GitHub Release Tool

A Windows desktop UI that uses `Start-GitHubRelease.ps1` to create a release tag
and request **both Windows and macOS** GitHub Actions workflows. Builds run on
GitHub, and your workflows publish the release assets.

## Features

- Light desktop layout with release details and workflow settings on separate tabs.
- Live target summary and individual Windows/macOS dispatch status.
- Inline validation errors, scrollable activity log, clickable run links and log copying.
- Token visibility toggle that resets when starting, with no saved credentials.
- Hover/focus help for every field and a bundled offline user guide.
- Configurable repository, branch, tag, workflow filenames and shared tag input name.
- Resume an existing tag at the same commit, without overwriting other tags.

## Launch

Requires Windows, Python 3.10+ with Tkinter (included with the standard Windows
Python installer), and Windows PowerShell 5.1 or PowerShell 7. No pip packages
are needed.

Keep `Start-GitHubRelease.ps1`, `release_ui.py`, `ReleaseTool.pyw` and `help.html`
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
and tag before starting. Windows and macOS statuses describe dispatch requests,
not the eventual build results. **Copy log** copies the redacted activity log.

Click **Help & setup** in the toolbar to open the bundled [user guide](help.html) in your
browser. It covers setup, token permissions, every form field, a release example,
retries and troubleshooting. The guide also works offline.

The UI clears the token field when starting. Credentials go to PowerShell via a
private stdin pipe, never command-line arguments, saved preferences or files.
The log redacts the supplied token. Credentials still exist briefly in process
memory; Python strings cannot guarantee secure erasure.

## Target repository setup

This utility orchestrates existing workflows. It does not install workflows or
compile the target app itself. Each target workflow must:

- Exist on GitHub's default branch and at the selected remote commit.
- Be enabled, and declare `workflow_dispatch:` in block YAML format.
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

The tool tags the current commit of the selected **remote** branch. It does not
commit, push, bump versions or include local changes. Set and commit the app's
version before launching the release. Publication, draft/prerelease settings,
signing and test gates remain controlled by the target workflows.

Existing tags pointing to the same commit are reusable; conflicting tags are
rejected and never moved or deleted. Annotated tags are resolved to their commit.
Both workflows are checked before creating the tag. If one dispatch fails, the
other is still attempted, and the tag remains available for retry.

Before each dispatch, the tool checks for an existing queued/running/successful
push or manual run for that exact tag and commit and reuses its link. Detection
is best effort: a concurrent run or a
delayed GitHub tag event can still create a duplicate. For deterministic manual
orchestration, configure the target release workflows with `workflow_dispatch`
only and avoid starting the same release from two clients simultaneously.
Failed/cancelled runs can be requested again by retrying with the same tag and
original branch commit.

“Builds requested” means dispatch succeeded, not that builds or publication
finished. A release page may initially be absent. The UI never sends a token
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
