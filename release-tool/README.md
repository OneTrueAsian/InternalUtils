# Release Tool

Build and publish Tauri releases using the process in [RELEASE-PROCESS.md](RELEASE-PROCESS.md).
The default **Local: full release** mode builds Windows on your computer and
uses GitHub Actions only for macOS. This reuses the local Cargo cache instead
of installing and rebuilding Windows dependencies on a fresh GitHub runner.
First-time local builds can still take several minutes; no completion time is guaranteed.

## Launch and prerequisites

```powershell
python .\release-tool\ReleaseTool.pyw
```

Requires Windows, Python 3.10+ with Tkinter, PowerShell 5.1/7, Git, GitHub CLI,
Node/npm, Rust and the application's Tauri CLI dependency. Windows builds need
Visual Studio C++ build tools and the WebView2 runtime. Apps using vendored
OpenSSL also need Strawberry Perl and a short repository/target path. The
Vault Spend profile puts `C:\Strawberry\perl\bin` first when installed; set
`perl_bin` for another installation. It rejects build paths longer than its
configurable `build_path_limit` (90 by default).

The supplied token is used through `GH_TOKEN` for GitHub CLI and the scoped Git
credential helper, never through command-line arguments or saved files. Build
commands do not receive it. Classic tokens need `repo` and `workflow`; use the
equivalent Contents/Actions write access for fine-grained tokens, with Workflows
permission where GitHub requires it. SSH Git remotes also need working SSH authentication.

## New release: full local pipeline

1. Select **Local: full release** on Release details. Enter repository, a new
   stable tag such as `v1.0.1`, and token. Leave Release branch blank to use
   the profile's `release-{version}` pattern. The release branch must differ
   from the default branch.
2. Open **Local release**. Select the application's local Git clone, app JSON
   profile, and a Markdown release-notes file. Commit application code changes
   first; tracked changes block startup. Unrelated untracked files are left alone.
3. Review **Workflow settings**: the macOS filename and tag input name matter.
   Windows is built locally, so its workflow filename is unused.
4. Click **Build release**. This authorizes the whole pipeline: changes to version
   files and release metadata, checks, commits, merge/push, local build, immediate
   publication and the macOS build. There are no repeated approval prompts.

The pipeline follows these steps:

- Verify local origin matches the requested GitHub repository, authenticate,
  fetch tags and reject an existing new-release tag. Show changes since the
  previous latest release; prevent publishing an older version as latest.
- Update `package.json`, `src-tauri/tauri.conf.json` and the Rust package version;
  regenerate both lockfiles using profile commands. For the Vault Spend profile,
  add the new in-app changelog entry, update `CANDIDATE` and its expected notes,
  and retain the previous release's notes test.
- Run type check, lint, unit tests, Rust format/clippy checks and Rust tests.
  A failing command is repeated once alone, then stops the pipeline if it still fails.
- Commit `Release X.Y.Z` on the release branch, push it, merge into the default
  branch with `--no-ff`, and push. If incoming default-branch changes alter the
  tested tree, repeat the gates on the merged tree; otherwise reuse their results.
- Run `npx tauri build` through PowerShell. Require two fresh, nonempty installers
  with exact product/version filenames; never select the newest file or a wildcard.
- Create the stable GitHub release with the exact merge SHA as `--target` and
  attach Windows installers. The tag is created here, after the build succeeds.
- Dispatch the manual macOS workflow against that tag and monitor the exact run.
- Verify all four expected nonempty assets, stable publication, the tag SHA,
  and `releases/latest`. Success is reported only after these checks.

**No E2E commands are added by default**, as requested in the source process.
Add an application's additional required gates to `check_commands`. No test is
silently skipped after failure. A failed pipeline leaves prepared files, commits
or published assets in place for diagnosis; it never resets your work or withdraws
a release automatically. A prepared, clean release version can resume before tag
creation when its notes and candidate metadata match the supplied notes.

## Release notes

Write notes outside the application repository. Bullets under **What's new** and
**Fixes and improvements** become the in-app notes for the Vault Spend adapter.
The tool does not invent features or release descriptions. Example:

```markdown
My App 1.0.1 makes account setup easier.

## What's new

- **Account setup.** Clearer instructions help you create an account.

## Fixes and improvements

- Fixed the saved account name disappearing after a restart.
```

Unsigned installation notes are appended when absent. Generated publication
notes live in a sibling `.release-tool-notes` folder (or `notes_dir`), outside the
app repository and outside Windows Temp by default, and are removed afterward.
Your input notes file is never overwritten.

## Existing release: local Windows only

Select **Local: Windows existing tag**, enter the existing tag and token, then
choose the local app folder and profile. Branch and notes fields are unused.
The tool temporarily checks out the tag's original commit, runs the configured
checks, builds Windows locally, uploads its installers, verifies them and restores
your original checkout. It does not bump versions, commit, merge, move tags or
start macOS. Existing macOS assets and release notes are preserved.

An existing published stable release page is required. Matching Windows assets
with identical SHA-256 digests are retained. If a different asset of the same
name exists, the tool stops; set `replace_windows_assets: true` in your profile
to explicitly allow `gh release upload --clobber`. That flag deletes matching
assets before re-uploading them, so a failed upload can leave them missing.

## Per-app profiles

Use `profiles/tauri-vault-spend.json` for Vault Spend's workspace, changelog and
metadata-test conventions. Use `profiles/tauri-generic.json` as a starting point
for another Tauri app. Neither profile stores a personal repository or token.
Copy a profile outside this utility before customizing it.

| Setting | Purpose |
| --- | --- |
| `default_branch`, `release_branch` | Merge destination and release-branch pattern |
| `cargo_manifest`, `cargo_lock`, optional `cargo_crate` | Rust package and lockfile layout |
| `changelog_file`, `metadata_test` | Optional supported TypeScript release-metadata adapter |
| `lock_commands`, `setup_commands` | Lockfile regeneration / existing-tag dependency setup |
| `prepare_commands`, `additional_release_files` | App-specific preparation hooks and their tracked output files |
| `check_commands` | Required gates, each an argument array, e.g. `["npm", "test"]` |
| `build_command` | Defaults to `["npx", "tauri", "build"]` |
| `windows_assets`, `mac_assets`, `architecture` | Exact versioned output/name patterns |
| optional `target_dir` | Cargo build cache used by checks and builds |
| `requires_strawberry_perl`, `perl_bin`, `build_path_limit` | OpenSSL Windows prerequisites |
| `unsigned`, optional `notes_dir` | Installation guidance and generated-note location |
| `replace_windows_assets` | Explicit replacement of existing Windows installers |

Commands are argument arrays, not interpolated shell strings. Preparation hooks
can use `{version}` and `{tag}` placeholders. Profile commands run locally with
your permissions, so review profiles before using them. The built-in adapter
expects standard Tauri JSON versions and literal Rust `[package]` version/name;
the optional changelog adapter expects a literal `CHANGELOG` record and the
documented `CANDIDATE` test convention. Other metadata layouts need explicit hooks.

## GitHub-only alternatives

The original **Windows + macOS** and **Windows only · existing tag** modes remain.
They dispatch remote workflows through `Start-GitHubRelease.ps1`; the latter uses
the updated Windows recovery workflow on a separate workflow branch. They retain
the app workflow's own gates, including its desktop E2E step, and can take longer.
Do not select these modes to use the new local process.

Remote release workflows must be enabled, registered on the default branch,
exist on the selected source, and use manual dispatch only. Recovery requires
`recovery`, `source_sha` and tag inputs plus `Recover Windows <tag>` run naming.
Tokens are passed to the remote PowerShell dispatcher through stdin.

## Failures, stopping and recovery

**Stop after step** stops a local pipeline before its next command, without killing
the current build or undoing a push/publication. During macOS monitoring it stops
polling; the GitHub build continues. Logs retain command output and redact the token.
**Copy log** copies the activity log; it is not automatically written to disk.

After a check failure, fix the cause on the release branch and commit the prepared
files before restarting. Never bypass a failing gate. After a partial published
release, the tag is immutable: use local Windows existing-tag mode for Windows
assets, and rerun a temporary macOS failure from its GitHub run page or:

```powershell
gh run view RUN_ID --repo owner/app-repo --log-failed
gh run rerun RUN_ID --repo owner/app-repo --failed
```

Withdrawal and asset deletion are deliberate manual recovery actions described
in [RELEASE-PROCESS.md](RELEASE-PROCESS.md); the tool never performs them automatically.
A repository lock prevents two local releases at once. If the process crashed,
confirm it exited before removing `.git/internalutils-release.lock`.

## Offline verification

```powershell
python -m unittest discover -s release-tool/tests -v
```

Tests use disposable files and fake GitHub/command transports. They cover the
full pipeline, blockers, tag pinning, exact installers, four-asset/latest checks,
partial publication, existing-tag recovery, command quoting and token handling.
They never build/publish a real app or prove that an application's checks will pass.


## Using another repository or application

Select its `owner/repo`, local checkout, and an app profile in **Local release**. Profiles contain no account or token. Copy a bundled profile to a new JSON file and select that file in the UI:

- `profiles/tauri-generic.json`: Tauri/Node/Rust apps. Set Cargo paths, product name if needed, commands, target directory and exact artifact names. Optional changelog/test paths are specific to the configured app; leave them empty or supply your own preparation command.
- `profiles/custom-app.json`: other build systems, including Python, Electron or .NET when their commands and metadata are configured. The example uses Python and a Windows ZIP; its build script is an example command you must replace or implement in your app.
- `profiles/tauri-vault-spend.json`: the Vault Spend preset.

Custom profiles set `adapter` to `custom`, `product_name`, `default_branch`, `release_branch`, `required_tools`, `check_commands`, `build_command`, `target_dir`, `windows_assets`, and `version_files`. Each version file is JSON with explicit property paths, for example:

```json
"version_files": [{"file": "config/app.json", "keys": [["release", "version"]]}]
```

Multiple files and properties are supported, including lockfile properties such as `["packages", "", "version"]`. The tool updates only those properties, verifies that all configured versions agree, and stages only these files plus `additional_release_files`. For TOML/XML/plain-text metadata, put the authoritative version in a JSON file and use `prepare_commands` to update other files; add those outputs to `additional_release_files` and add consistency checks to `check_commands`.

Commands are argument arrays, for example `["dotnet", "publish", "-c", "Release"]`; `{version}` and `{tag}` are substituted in each argument. Commands run from the local repository. Configure dependency installation in `setup_commands` (existing-tag builds) and `lock_commands` (new releases). Required tools always include Git and GitHub CLI; custom profiles add only the tools you list. There are no implicit Node, Rust or E2E requirements for custom apps.

List exact Windows filenames relative to `target_dir`; use `{product}`, `{version}`, and `{architecture}` where appropriate. Asset counts are configurable. Set `mac_assets` to `[]` for a Windows-only application: macOS settings are ignored and no macOS workflow is dispatched. Otherwise list exact macOS release asset names and provide a manual-only publishing workflow that accepts the UI's tag input. Final checks verify exactly the configured assets, their nonzero sizes, the pinned tag commit, and `releases/latest`.

All local release profiles currently use stable `vX.Y.Z` tags. The build scripts, installers, test gates and macOS publishing workflow must already exist in the target app. A profile configures the pipeline; it does not generate another application's build system. Use only profiles and repositories you trust: their commands execute locally and Build release authorizes commits, pushes and publication.
