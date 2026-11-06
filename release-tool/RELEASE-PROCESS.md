# General release process

## Vault Spend hosted draft pipeline

Choose **Windows: sharded draft** (or **Windows: serial draft** for fallback), the pushed release branch and a matching production tag. The remote workflow runs all gates, packages and signs into an unpublished draft. The tool tracks required jobs and verifies draft asset presence at the pinned tag. Approve the `release-signing` environment on GitHub; publishing is a separate owner action. macOS is deliberately not dispatched until its draft policy is migrated. Do not use the generic local publishing recipe below as a substitute for this hosted acceptance path. See the [draft mode instructions](README.md#vault-spend-hosted-windows-draft).

## Profile-driven local publishing

Every repository supplies a reviewed application profile. No application name, toolchain, changelog format, signing policy or asset count is assumed.

1. Select the target repository, local clone, profile, stable `vX.Y.Z` tag and release notes.
2. Verify repository access, matching Git origin, clean tracked files and absence of the new tag.
3. Resolve the profile's release branch and inspect changes since the previous release.
4. Update the profile's version metadata. Run its dependency/lock preparation and application-specific hooks.
5. Verify metadata consistency and run all configured release gates. Retry a failed gate once; stop if it still fails.
6. Commit only configured release files. Push the release branch, merge into the configured default branch, validate incoming merge changes when needed, and push.
7. Record the commit SHA. Run the configured local build and require fresh, nonempty artifacts at every configured exact path.
8. Publish a stable GitHub release and create its tag at that SHA, with the prepared notes and local artifacts.
9. When configured, dispatch the optional macOS publishing workflow for that tag and monitor its result.
10. Verify all declared assets, stable publication, the pinned tag commit and the latest-release endpoint.

Starting the release authorizes this pipeline. Failures stop subsequent steps; pushes and published assets are retained for diagnosis. Tags are never moved automatically.

For existing releases, local existing-tag mode builds and uploads the configured local artifacts without changing version metadata or moving the tag. It restores the original checkout afterward and preserves unrelated assets. Replacement of different existing local artifacts requires an explicit profile setting.

For a failed optional remote build, inspect the linked run and rerun it after diagnosing the cause. Code fixes generally need a new tagged commit. Asset deletion, release withdrawal and tag changes remain deliberate manual operations.

See [setup and profile configuration](README.md) for supported metadata adapters, dependencies, workflow requirements and current platform limits.
