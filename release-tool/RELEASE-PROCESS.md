# Release build process ("push out a release")

How Claude produces a release when told to "push out a release" (or "push out the new build", "cut a
release", "ship it"). Written as input for the GitHub Release Tool: every step, command, tool and
check, in order. Vault Spend values are used as the worked example; the per-app values are collected
in [Per-app settings](#per-app-settings) so the same steps can drive other apps.

The app's end-to-end UI suite is deliberately left out. It is specific to Vault Spend, and a release
tool for several apps should treat any app's own release tests as a configurable gate (step 4).

---

## 0. How the request is treated

- The phrase is a **standing approval for the whole pipeline**, start to finish: bump, check, commit,
  merge, build, publish, macOS build, verify. Claude does not stop to ask before each step. (A
  "confirm before publishing" pause was tried once and the owner rejected it.)
- It still **stops on a real failure**: a failing check, a missing asset, a version mismatch. A
  failure is re-run once on its own to confirm it is real before it blocks the release.
- Publishing is visible to users straight away: the app's update check reads
  `GET /repos/<owner>/<repo>/releases/latest`, so a published, non-draft, non-prerelease release
  is offered to every installed copy on its next launch.

## 1. Tools and accounts

| Tool | Version used | Used for |
|---|---|---|
| Git | any recent | branch, commit, merge, push, tag checks |
| GitHub CLI `gh` | 2.98.0 | create release, upload assets, start/watch workflows, verify |
| Node.js + npm | Node 20 (CI uses 20/22) | `npm install`, `npm test`, `npm run lint`, `npx tsc`, `npx tauri build` |
| Rust toolchain (rustup, stable) | cargo 1.98.0 | `cargo check/fmt/clippy/test`, the Tauri build |
| Tauri CLI | from the app's `devDependencies` (`npx tauri`) | the release bundle |
| PowerShell | 5.1 or 7 | runs the Windows build (see below) |

**GitHub authentication.** `gh auth status` must show a login with the `repo` and `workflow` scopes
(classic token). The fine-grained equivalent is Contents read/write plus Actions read/write on the
target repository. Check it with:

```powershell
gh auth status
```

**Windows build machine prerequisites** (Tauri plus OpenSSL; from `docs/BUILDING.md`):

- Visual Studio Build Tools with the **Desktop development with C++** workload.
- WebView2 runtime (part of Windows 11).
- **Strawberry Perl** (`winget install StrawberryPerl.StrawberryPerl`). OpenSSL's build needs a real
  Windows Perl; Git's bundled Perl breaks it.
- Run the build from **PowerShell**, not Git Bash. Git Bash puts Git's own `perl` first on `PATH`.
- Keep the build folder path **short**. OpenSSL's scripts fail over 260 characters with
  `Can't locate Text/Template.pm`. A deep worktree or `CARGO_TARGET_DIR` triggers it.

**macOS.** There is no Mac. Tauri cannot cross-compile macOS from Windows, so the macOS bundle is
always built on a GitHub-hosted `macos-latest` runner (step 9).

**Signing.** None. Both platforms ship unsigned by choice. Windows SmartScreen and macOS Gatekeeper
warnings are expected and covered by the install notes in the release text.

## 2. Preconditions

```powershell
git status --short                       # must be clean (untracked files that are not part of the release are fine)
git fetch origin --tags
git branch --show-current                # the release branch, e.g. release-1.3.0
gh release view v<prev> --repo <owner>/<repo> --json tagName   # the previous release, to diff against
git ls-remote --tags origin "v<new>"     # must print nothing: the new tag must not exist yet
```

- Work happens on a branch named `release-X.Y.Z`, merged into `main` at publish time (step 6).
- Choose the version (SemVer): patch for fixes only, minor for new features.
- Gather what changed for the notes:

```powershell
git log --oneline v<prev>..HEAD
git diff --stat v<prev>..HEAD
```

## 3. Bump the version everywhere

All of these must name the same version. The release-metadata unit test (step 4) fails if any of them
drift.

| File | Field |
|---|---|
| `package.json` | `"version"` |
| `src-tauri/tauri.conf.json` | `"version"` |
| `src-tauri/Cargo.toml` | `version = "…"` under `[package]` |
| `package-lock.json` | top-level `"version"` and `packages[""].version` (regenerated) |
| `Cargo.lock` | the app crate's entry, `name = "vaultspend"` (regenerated) |

Edit the first three by hand, then regenerate the lockfiles:

```powershell
npm install                  # rewrites package-lock.json with the new version
cargo check --workspace      # rewrites Cargo.lock with the new crate version
```

Then add the release's in-app text:

- **`src/changelog.ts`**: add a `"X.Y.Z": [ … ]` entry to `CHANGELOG`. These are short, plain-language
  lines condensed from the GitHub notes. The app shows them once per version in its "What's New"
  dialog; a version without an entry shows nothing.
- **`src/releaseMetadata.test.ts`**: set `CANDIDATE = "X.Y.Z"` and update the assertions that check
  the new notes mention what shipped. Keep a "preserves the <previous> release notes" test for the
  version before.

## 4. Check gate (no E2E)

Run all of these; every one must pass before anything is committed or published:

```powershell
npx tsc --noEmit                                       # type check
npm run lint                                           # eslint
npm test                                               # vitest unit tests, includes the release-metadata test
cargo fmt --all -- --check                             # Rust formatting
cargo clippy --workspace --all-targets -- -D warnings  # Rust lints
cargo test --workspace                                 # Rust tests
```

Also confirm the versions agree, as a direct check that does not depend on the test:

```powershell
node -p "require('./package.json').version"
node -p "require('./src-tauri/tauri.conf.json').version"
Select-String -Path src-tauri/Cargo.toml -Pattern '^version = "(.+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value }
```

On a failure: re-run that one check alone. If it still fails, stop and report it. Never ship past it,
and never silently skip it.

## 5. Write the GitHub release notes

Write a Markdown file (kept outside the repository, for example in a scratch folder). It is written
for people who are not financially literate: plain words, no jargon. Structure used for every
release:

```markdown
<App> X.Y.Z <one-sentence summary of the release>.

## What's new

- **<Feature>.** <What it does for the user, where to find it.>

## Fixes and improvements

- **<Problem that was fixed>** <what used to happen, what happens now>.

## Install notes

Both builds are unsigned:
- **Windows**: SmartScreen may warn — click "More info" → "Run anyway".
- **macOS**: Gatekeeper will block the first launch — right-click the app → Open, or run `xattr -dr com.apple.quarantine "/Applications/<Product Name>.app"` in Terminal.

<App> tells you when a newer version exists; it never installs one on its own.
```

The `src/changelog.ts` entry from step 3 is a condensed copy of the same content.

## 6. Commit, merge to `main`, push

```powershell
git add package.json package-lock.json src-tauri/tauri.conf.json src-tauri/Cargo.toml Cargo.lock src/changelog.ts src/releaseMetadata.test.ts
git commit -m "Release X.Y.Z" -m "<what changed in the release files; the gate results>"
git push origin release-X.Y.Z

git checkout main
git pull --ff-only origin main
git merge --no-ff release-X.Y.Z -m "Merge release-X.Y.Z: <App> X.Y.Z"
git push origin main
git rev-parse HEAD                      # the release commit: record this SHA for steps 8 and 10
```

The release tag goes on this merge commit. Past tags: `v1.2.9` → `95b4124 Merge release-1.2.9: Vault
Spend 1.2.9`, `v1.2.8` → `d1b2e45 Merge branch 'release-1.2.8'`.

## 7. Build the Windows installers (locally)

From PowerShell, at the repository root, on the merge commit:

```powershell
npx tauri build          # release build: no --debug, no --no-bundle
```

- `beforeBuildCommand` in `tauri.conf.json` runs `npm run build` first (`tsc && vite build`, plus
  the mobile bundle for Vault Spend), so the frontend is rebuilt automatically.
- `bundle.targets` is `"all"`, which on Windows produces both installers:

| Installer | Path |
|---|---|
| NSIS | `target/release/bundle/nsis/<productName>_<version>_x64-setup.exe` |
| MSI | `target/release/bundle/msi/<productName>_<version>_x64_en-US.msi` |

For Vault Spend: `target/release/bundle/nsis/Vault Spend_1.3.0_x64-setup.exe` and
`target/release/bundle/msi/Vault Spend_1.3.0_x64_en-US.msi`.

Check both exist **and carry the new version** in their names. The bundle folders keep installers
from older builds, so never pick "the newest file" or a wildcard; build the exact expected file names
from `productName` and the version.

## 8. Create the GitHub release (this also creates the tag)

```powershell
gh release create vX.Y.Z `
  "target/release/bundle/nsis/<productName>_X.Y.Z_x64-setup.exe" `
  "target/release/bundle/msi/<productName>_X.Y.Z_x64_en-US.msi" `
  --repo <owner>/<repo> `
  --target <release commit SHA from step 6> `
  --title "<App> X.Y.Z" `
  --notes-file <notes.md>
```

- `gh release create` creates the tag `vX.Y.Z` on GitHub at `--target` and publishes the release
  (not a draft, not a prerelease) with the two Windows installers attached.
- Pass the **commit SHA**, not `main`. A branch name resolves to whatever `main` is at that moment.
  (`v1.2.9` used `--target main`; `v1.2.8` used the SHA.)
- GitHub replaces spaces in asset names with dots: `Vault Spend_1.3.0_x64-setup.exe` is listed as
  `Vault.Spend_1.3.0_x64-setup.exe`.

## 9. Build and attach the macOS bundle (GitHub Actions)

`.github/workflows/build-macos.yml` builds a universal (Intel + Apple Silicon) bundle on
`macos-latest` with `tauri-apps/tauri-action@v0` (`args: --target universal-apple-darwin`). It
attaches the result to the existing release for the tag.

**Current state:** on `main` the workflow is `workflow_dispatch` only. A tag push no longer starts it,
so it must be started explicitly. (Up to 1.2.9 it also ran on `v*` tag pushes, and the release in
step 8 started it automatically.)

```powershell
gh workflow run build-macos.yml --repo <owner>/<repo> --ref vX.Y.Z -f tag=vX.Y.Z

# find the run that was just started
gh run list --repo <owner>/<repo> --workflow build-macos.yml --event workflow_dispatch --limit 5 `
  --json databaseId,headBranch,headSha,status,createdAt

# watch it to the end; a non-zero exit means it failed
gh run watch <run-id> --repo <owner>/<repo> --exit-status
```

- A `workflow_dispatch` workflow must exist on the repository's **default branch** to be startable,
  and the version that runs is the one at `--ref` (the tag).
- Pick the run whose `headBranch` is the tag and whose `headSha` is the release commit.
- Watch it to completion; do not fire and forget. It takes about 11–13 minutes.
- It attaches two assets: `<productName>_X.Y.Z_universal.dmg` and `<productName>_universal.app.tar.gz`.

**Windows alternative:** `release-windows.yml` (dispatch-only, `tag` input) builds and publishes the
Windows installers on `windows-latest` instead of locally. It runs the app's own tests and E2E suite
before `tauri-action` with `--bundles nsis,msi`. Started the same way:
`gh workflow run release-windows.yml --ref vX.Y.Z -f tag=vX.Y.Z`. When CI builds Windows, the release
must already exist or `tauri-action` creates it, and step 8 then uploads no files.

## 10. Verify the published release

```powershell
gh release view vX.Y.Z --repo <owner>/<repo> --json tagName,name,isDraft,isPrerelease,targetCommitish,assets `
  -q '"\(.tagName) | \(.name) | draft=\(.isDraft) pre=\(.isPrerelease)", (.assets[] | "  \(.name) \(.size)")'

git ls-remote --tags origin vX.Y.Z          # must equal the release commit SHA from step 6
gh api repos/<owner>/<repo>/releases/latest -q .tag_name   # must be vX.Y.Z (what installed apps will see)
```

Expected for Vault Spend: four assets, none of them empty.

| Asset | From |
|---|---|
| `Vault.Spend_X.Y.Z_x64-setup.exe` | step 7/8 |
| `Vault.Spend_X.Y.Z_x64_en-US.msi` | step 7/8 |
| `Vault.Spend_X.Y.Z_universal.dmg` | step 9 |
| `Vault.Spend_universal.app.tar.gz` | step 9 |

Also `isDraft=false`, `isPrerelease=false`, and the release name is `<App> X.Y.Z`. Then refresh local
tags with `git fetch --tags` and report: version, release URL, release commit, assets, check results,
and the macOS run link.

## 11. Recovery

| Problem | Fix |
|---|---|
| A check in step 4 fails | Re-run it alone. Still failing: stop, fix on the release branch, restart from step 4. |
| A Windows installer is missing or has the wrong version | Fix and rebuild (step 7), then `gh release upload vX.Y.Z "<file>" --repo <owner>/<repo> --clobber`. |
| The macOS run failed | Read it with `gh run view <run-id> --log-failed`. Temporary failure: `gh run rerun <run-id> --failed`. Code problem: the tag fixes the code, so a fix needs a new version, or the tag moved by hand. |
| A wrong asset was attached | `gh release delete-asset vX.Y.Z "<asset name>" --repo <owner>/<repo> --yes`, then upload the right one. |
| The whole release must be withdrawn | `gh run cancel <run-id>` for any running build, then `gh release delete vX.Y.Z --repo <owner>/<repo> --cleanup-tag --yes`. Revert the merge on `main` (`git revert -m 1 <merge sha>` + push) and keep the work on `release-X.Y.Z`. Used once, for 1.2.5. |

Tags are never moved silently. A tag that already points at a different commit means stop and ask.

## Per-app settings

Everything app-specific that the steps above use. A multi-app tool needs these per app.

| Setting | Vault Spend value |
|---|---|
| Repository | `OneTrueAsian/vault-spend` |
| Product name (`tauri.conf.json` `productName`) | `Vault Spend` |
| Release title | `Vault Spend X.Y.Z` |
| Tag format | `vX.Y.Z` (stable only; the Windows workflow rejects anything else) |
| Release branch | `release-X.Y.Z`, merged `--no-ff` into `main` |
| Version files | `package.json`, `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml` |
| Lockfiles | `package-lock.json` (`npm install`), `Cargo.lock` (`cargo check --workspace`) |
| Lockfile crate name | `vaultspend` |
| In-app changelog | `src/changelog.ts` (`CHANGELOG["X.Y.Z"]`) |
| Release-metadata test | `src/releaseMetadata.test.ts` (`CANDIDATE`) |
| Check gate | `npx tsc --noEmit`, `npm run lint`, `npm test`, `cargo fmt --all -- --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace` |
| Windows build | local `npx tauri build`, or `release-windows.yml` |
| Windows assets | `nsis/<productName>_<v>_x64-setup.exe`, `msi/<productName>_<v>_x64_en-US.msi` |
| macOS workflow | `build-macos.yml`, input `tag`, dispatch-only |
| macOS assets | `<productName>_<v>_universal.dmg`, `<productName>_universal.app.tar.gz` |
| Expected asset count | 4 |
| Signing | none (unsigned on both platforms) |
| Update check | notify-only; reads `releases/latest` |

## Command checklist

```text
gh auth status
git status --short
git fetch origin --tags
git log --oneline v<prev>..HEAD
git ls-remote --tags origin vX.Y.Z
<edit version in package.json, tauri.conf.json, Cargo.toml>
npm install
cargo check --workspace
<edit src/changelog.ts and src/releaseMetadata.test.ts>
npx tsc --noEmit
npm run lint
npm test
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
<write notes.md>
git add … ; git commit -m "Release X.Y.Z"
git push origin release-X.Y.Z
git checkout main ; git pull --ff-only origin main
git merge --no-ff release-X.Y.Z -m "Merge release-X.Y.Z: <App> X.Y.Z"
git push origin main
git rev-parse HEAD
npx tauri build                       (PowerShell, Strawberry Perl on PATH, short path)
gh release create vX.Y.Z <exe> <msi> --target <sha> --title "<App> X.Y.Z" --notes-file notes.md
gh workflow run build-macos.yml --ref vX.Y.Z -f tag=vX.Y.Z
gh run list --workflow build-macos.yml --event workflow_dispatch --limit 5
gh run watch <run-id> --exit-status
gh release view vX.Y.Z --json assets,isDraft,isPrerelease
git ls-remote --tags origin vX.Y.Z
gh api repos/<owner>/<repo>/releases/latest -q .tag_name
git fetch --tags
```
