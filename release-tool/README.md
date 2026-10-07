# GitHub Release Tool

A Windows desktop UI that uses `Start-GitHubRelease.ps1` to create a release tag
and request **both Windows and macOS** GitHub Actions workflows. Builds run on
GitHub, and your workflows publish the release assets.

## Launch

Requires Windows, Python 3.10+ with Tkinter (included with the standard Windows
Python installer), and Windows PowerShell 5.1 or PowerShell 7. No pip packages
are needed.

Double-click `ReleaseTool.pyw`, or run:

```powershell
python .\release-tool\ReleaseTool.pyw
```

Enter the target `owner/repo`, remote branch, release tag, and GitHub API token.
The defaults point at `OneTrueAsian/vault-spend`, `release-windows.yml`, and
`build-macos.yml`. A blank branch uses the target repo's default branch. Workflow
filenames are editable. Click **Start release builds**; follow the clickable run
links or the **Open GitHub Actions** and **Open release** buttons.

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

For example, the existing Vault Spend macOS release workflow accepts `tag`.
Its Windows release workflow created alongside this work still needs to be
committed and pushed in Vault Spend before this tool can dispatch it.
`build-macos-check.yml` is a validation workflow that does not publish releases;
use `build-macos.yml` for release builds.

GitHub's requirement for workflows on the default branch is documented in
[Manually running a workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

Create a fine-grained personal access token restricted to the **target** repository
with **Contents: read/write** and **Actions: read/write**. GitHub may also require
**Workflows: read/write** for reference creation involving workflow files; see
[Git references API permissions](https://docs.github.com/en/rest/git/refs#create-a-reference).
For a classic token, use `repo` (and `workflow` where required). Organization
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
push or manual run for that exact tag and commit and reuses its link. This covers
Vault Spend's macOS tag trigger. Detection is best effort: a concurrent run or a
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
runs, partial failures, and Tkinter form state. They never contact GitHub or start
a build. A real GitHub release run is required to validate the target repo's build
environment and packaging workflow.
