import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_release as local


class World:
    def __init__(self, root, full=True):
        self.root = root
        self.full = full
        self.calls = []
        self.tag = None if full else 'c' * 40
        self.head = 'a' * 40
        self.release = None if full else dict(tag_name='v1.0.0', draft=False, prerelease=False, assets=[dict(name='Sample.App_1.0.0_universal.dmg', size=3)])
        self.fail_gate = False
        self.missing_msi = False
        self.bad_origin = False
        self.mac_fail = False
        self.wrong_latest = False
        self.origin = 'owner/app'
        self.latest_tags = []
        self.fail_push = False
        self.tag_after_release = None
        self.publish_after_create = False
        self.drop_draft_asset = False
        self.signed_files = []
        self.runner = None

    def factory(self, root, token, events, stop):
        world = self
        class FakeRunner(local.Runner):
            def run(self, args, **kwargs):
                args = list(map(str, args))
                world.calls.append((args, kwargs))
                if self.stop.is_set() and not kwargs.get('ignore_stop'):
                    raise local.ReleaseError('Stopped')
                if args[:3] == ['git', 'remote', 'get-url']:
                    return 0, 'https://github.com/other/app.git' if world.bad_origin else f'https://github.com/{world.origin}.git'
                if args[:3] == ['git', 'branch', '--show-current']:
                    return 0, 'release-1.0.1' if world.full else 'main'
                if args[:3] == ['git', 'rev-parse', 'HEAD']:
                    return 0, world.head
                if args[:3] == ['git', 'show-ref', '--verify']:
                    return 1, ''
                if args[:2] == ['git', 'switch']:
                    world.head = args[-1] if '--detach' in args else 'a' * 40
                if args[:2] == ['git', 'merge']:
                    world.head = 'b' * 40
                if args[:2] == ['git', 'push'] and world.fail_push:
                    raise local.CommandError('git push failed (exit 1)')
                if args == ['npm', 'install']:
                    version = json.loads((root / 'package.json').read_text())['version']
                    (root / 'package-lock.json').write_text(json.dumps(dict(version=version, packages={'':dict(version=version)})))
                if args == ['cargo', 'check', '--workspace']:
                    version = json.loads((root / 'package.json').read_text())['version']
                    (root / 'Cargo.lock').write_text(f'[[package]]\nname = "sample"\nversion = "{version}"\n')
                if args == ['npm', 'test'] and world.fail_gate:
                    raise local.CommandError('Unit tests failed')
                if args == ['npx', 'tauri', 'build']:
                    version = json.loads((root / 'package.json').read_text())['version']
                    for folder, name in [('nsis', f'Sample App_{version}_x64-setup.exe'), ('msi', f'Sample App_{version}_x64_en-US.msi')]:
                        if folder == 'msi' and world.missing_msi:
                            continue
                        path = root / 'target/release/bundle' / folder / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b'new installer')
                if args[:3] == ['gh', 'release', 'create']:
                    drafted = '--draft' in args
                    uploaded = [dict(name=Path(name).name.replace(' ', '.'), size=13) for name in args[4:args.index('--repo')]]
                    if world.drop_draft_asset:
                        uploaded = uploaded[:-1]
                    # A draft has no tag until it is published, and GitHub does not return it from the by-tag lookup.
                    world.tag = world.tag_after_release or (None if drafted else args[args.index('--target') + 1])
                    world.release = dict(tag_name=args[3], draft=drafted and not world.publish_after_create, prerelease=False, target_commitish=args[args.index('--target') + 1],
                                         html_url='https://github.com/owner/app/releases/tag/untagged-1', assets=uploaded)
                if args[:3] == ['gh', 'release', 'upload']:
                    paths = args[4:args.index('--repo')]
                    world.release['assets'].extend(dict(name=Path(name).name.replace(' ', '.'), size=13) for name in paths)
                return 0, ''

            def api(self, repository, path, body=None, missing=False):
                world.calls.append((['API', path], dict(body=body)))
                if path.startswith('git/ref/tags/'):
                    return dict(object=dict(type='commit', sha=world.tag)) if world.tag else None
                if path.startswith('releases?per_page=100&page='):
                    return [world.release] if world.release and int(path.rsplit('=', 1)[1]) == 1 else []
                if path.startswith('releases/tags/'):
                    return None if world.release and world.release.get('draft') else world.release
                if path == 'releases/latest':
                    if world.latest_tags:
                        tag = world.latest_tags.pop(0)
                        return dict(tag_name=tag, draft=False, prerelease=False, assets=[]) if tag else None
                    if not world.release or world.release.get('draft'):
                        return None
                    return dict(world.release, tag_name='v0.9.0') if world.wrong_latest else world.release
                if path.endswith('/dispatches'):
                    return dict(workflow_run_id=44, html_url='https://github.com/owner/app/actions/runs/44')
                if '/runs?' in path:
                    return dict(workflow_runs=[])
                if path.startswith('actions/workflows/'):
                    return dict(state='active')
                return {}
        self.runner = FakeRunner(root, token, events, stop)
        return self.runner

    def monitor(self, repository, token, runs, events, stop):
        self.calls.append((['MONITOR'], dict(runs=runs)))
        if self.mac_fail:
            return False
        version = self.release['tag_name'][1:]
        self.release['assets'].extend([dict(name=f'Sample.App_{version}_universal.dmg', size=10),dict(name='Sample.App_universal.app.tar.gz', size=10)])
        self.release['assets'].extend(dict(name=name, size=10) for name in self.signed_files)
        return True


class LocalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='local-release-tests-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / 'app'
        self.root.mkdir()
        (self.root / 'src-tauri').mkdir()
        (self.root / 'src').mkdir()
        (self.root / '.github/workflows').mkdir(parents=True)
        (self.root / '.github/workflows/build-macos.yml').write_text('on:\n  workflow_dispatch:\n    inputs:\n      tag:\n')
        (self.root / 'package.json').write_text('{"version":"1.0.0"}')
        (self.root / 'src-tauri/tauri.conf.json').write_text('{"productName":"Sample App","version":"1.0.0"}')
        (self.root / 'src-tauri/Cargo.toml').write_text('[package]\nname = "sample"\nversion = "1.0.0"\n[dependencies]\nother = "2.0.0"\n')
        (self.root / 'package-lock.json').write_text(json.dumps(dict(version='1.0.0', packages={'':dict(version='1.0.0')})))
        (self.root / 'Cargo.lock').write_text('[[package]]\nname = "sample"\nversion = "1.0.0"\n')
        (self.root / 'src/changelog.ts').write_text('export const CHANGELOG: Record<string, string[]> = {\n  "1.0.0": ["Original notes",],\n};\n')
        (self.root / 'src/releaseMetadata.test.ts').write_text('const CANDIDATE = "1.0.0";\ndescribe("release metadata", () => {\n  it(`is the ${CANDIDATE} release candidate`, () => {\n    expect(pkg).toBe(CANDIDATE);\n    expect(notes).toMatch(/Original/);\n  });\n});\n')
        profile = json.loads((local.HERE / 'tests/fixtures/tauri-metadata.json').read_text())
        profile['requires_strawberry_perl'] = False
        self.profile = Path(self.folder.name) / 'profile.json'
        self.profile.write_text(json.dumps(profile))
        self.notes = Path(self.folder.name) / 'notes.md'
        self.notes.write_text("Sample App 1.0.1\n\n## What's new\n\n- **New feature.** Clear notes.\n\n## Fixes and improvements\n\n- Fixed a problem.\n", encoding='utf-8')
        self.request = dict(repository='owner/app', ref='', tag='v1.0.1', token='offline-secret', local_repo=str(self.root), app_profile=str(self.profile), notes_file=str(self.notes), build_mode='local_release', windows_workflow='release-windows.yml', mac_workflow='build-macos.yml', tag_input='tag')
        self.events = queue.Queue()
        self.world = World(self.root)

    def execute(self):
        with patch.object(local, 'preflight_tools', return_value={}), patch.object(local, 'repository_lock', return_value=nullcontext()):
            return local.local_pipeline(self.request, self.events, runner_factory=self.world.factory, monitor=self.world.monitor)

    def commands(self):
        return [entry[0] for entry in self.world.calls]

    def test_full_pipeline_uses_merge_sha_and_verifies_four_assets(self):
        self.assertTrue(self.execute())
        commands = self.commands()
        create = next(args for args in commands if args[:3] == ['gh', 'release', 'create'])
        self.assertEqual(create[create.index('--target') + 1], 'b' * 40)
        self.assertEqual(commands.count(['npm', 'test']), 1)
        self.assertTrue(any(args[:3] == ['git', 'merge', '--no-ff'] for args in commands))
        self.assertEqual(len(self.world.release['assets']), 4)
        dispatch = next(options for args, options in self.world.calls if args[-1].endswith('/dispatches'))
        self.assertEqual(dispatch['body'], dict(ref='v1.0.1', inputs={'tag':'v1.0.1'}))
        self.assertEqual(self.request['token'], '')
        self.assertNotIn('offline-secret', str(list(self.events.queue)))
        local.check_versions(self.root, local.load_profile(self.profile), '1.0.1')
        self.assertIn('"Original notes"', (self.root / 'src/changelog.ts').read_text())
        self.assertIn('preserves the 1.0.0', (self.root / 'src/releaseMetadata.test.ts').read_text())

    def test_custom_app_full_pipeline_uses_nested_versions_one_asset_no_mac(self):
        profile = json.loads((local.HERE / 'profiles/custom-app.json').read_text())
        profile.update(product_name='Other App', default_branch='trunk',
                       version_files=[dict(file='config/app.json', keys=[['release', 'version']])],
                       check_commands=[['python', '-m', 'unittest']],
                       build_command=['python', 'scripts/build.py', '--version', '{version}'])
        self.profile.write_text(json.dumps(profile))
        (self.root / 'config').mkdir()
        manifest = self.root / 'config/app.json'
        manifest.write_text('{"release":{"version":"1.0.0"},"keep":true}')
        self.request.update(mac_workflow='', tag_input='')
        original_factory = self.world.factory
        def factory(*args):
            runner = original_factory(*args)
            original_run = runner.run
            def run(command, **kwargs):
                if command[:2] == ['python', 'scripts/build.py']:
                    self.assertEqual(command[-1], '1.0.1')
                    path = self.root / 'dist/Other App-1.0.1-windows.zip'
                    path.parent.mkdir()
                    path.write_bytes(b'custom application package')
                return original_run(command, **kwargs)
            runner.run = run
            return runner
        self.world.factory = factory
        self.assertTrue(self.execute())
        self.assertEqual(json.loads(manifest.read_text()), {'release': {'version': '1.0.1'}, 'keep': True})
        self.assertEqual(len(self.world.release['assets']), 1)
        self.assertIn(['git', 'push', 'origin', 'trunk'], self.commands())
        self.assertFalse(any(command[0] in ('npm', 'npx', 'cargo', 'MONITOR') or command[-1].endswith('/dispatches') for command in self.commands()))
        staged = next(command for command in self.commands() if command[:2] == ['git', 'add'])
        self.assertEqual(staged[3:], ['config/app.json'])

    def test_profile_requires_explicit_adapter(self):
        profile = json.loads(self.profile.read_text())
        profile.pop('adapter')
        self.profile.write_text(json.dumps(profile))
        with self.assertRaisesRegex(local.ReleaseError, 'adapter'):
            local.load_profile(self.profile)

    def test_custom_profile_requires_only_its_configured_build_tools(self):
        profile = local.load_profile(local.HERE / 'profiles/custom-app.json')
        seen = []
        with patch.object(local.shutil, 'which', side_effect=lambda tool: seen.append(tool) or tool), patch.object(local.os, 'name', 'nt'):
            local.preflight_tools(self.root, profile)
        self.assertEqual(seen, ['git', 'gh', 'python'])

    def test_custom_versions_validate_all_files_before_any_write(self):
        profile = local.load_profile(local.HERE / 'profiles/custom-app.json')
        (self.root / 'app.json').write_text('{"version":"1.0.0"}')
        profile['version_files'].append(dict(file='missing.json', keys=[['version']]))
        with self.assertRaisesRegex(local.ReleaseError, 'missing.json'):
            local.prepare_versions(self.root, profile, '1.0.1', '')
        self.assertEqual(json.loads((self.root / 'app.json').read_text())['version'], '1.0.0')

    def test_prepared_version_can_resume_before_tag_creation(self):
        profile = local.load_profile(self.profile)
        local.prepare_versions(self.root, profile, '1.0.1', self.notes.read_text(encoding='utf-8'))
        self.assertTrue(self.execute())
        self.assertEqual(self.world.tag, 'b' * 40)

    def test_failing_gate_never_commits_builds_or_publishes(self):
        self.world.fail_gate = True
        with self.assertRaises(local.CommandError):
            self.execute()
        for args in self.commands():
            self.assertNotEqual(args[:2], ['git', 'commit'])
            self.assertNotEqual(args, ['npx', 'tauri', 'build'])
            self.assertNotEqual(args[:3], ['gh', 'release', 'create'])

    def test_missing_exact_installer_stops_before_publication(self):
        self.world.missing_msi = True
        with self.assertRaisesRegex(local.ReleaseError, 'fresh nonempty installer'):
            self.execute()
        self.assertFalse(any(args[:3] == ['gh','release','create'] for args in self.commands()))

    def test_existing_tag_never_moves_or_bumps(self):
        self.world.tag = 'c' * 40
        before = (self.root / 'package.json').read_bytes()
        with self.assertRaisesRegex(local.ReleaseError, 'already exists'):
            self.execute()
        self.assertEqual(before, (self.root / 'package.json').read_bytes())

    def test_wrong_profile_stops_before_checkout_or_dependency_setup(self):
        self.request.update(build_mode='local_windows', tag='v1.0.0', notes_file='')
        self.world = World(self.root, full=False)
        factory = self.world.factory
        def missing_factory(*args):
            runner = factory(*args)
            original_run = runner.run
            def run(command, **kwargs):
                if command[:3] == ['git', 'cat-file', '-e']:
                    return 1, 'missing metadata'
                return original_run(command, **kwargs)
            runner.run = run
            return runner
        self.world.factory = missing_factory
        with self.assertRaisesRegex(local.ReleaseError, 'profile does not match'):
            self.execute()
        self.assertFalse(any(args[:2] == ['git', 'switch'] or args == ['npm', 'ci'] for args in self.commands()))

    def test_local_windows_preserves_mac_assets_and_restores_branch(self):
        self.request.update(build_mode='local_windows', tag='v1.0.0', notes_file='')
        self.world = World(self.root, full=False)
        self.assertTrue(self.execute())
        self.assertIn('Sample.App_1.0.0_universal.dmg', [asset['name'] for asset in self.world.release['assets']])
        self.assertFalse(any(args[-1].endswith('/dispatches') for args in self.commands()))
        self.assertFalse(any(args[:2] == ['git','commit'] for args in self.commands()))
        self.assertEqual(self.world.tag, 'c' * 40)
        self.assertEqual(self.commands()[-1], ['git','switch','main'])

    def test_mac_failure_keeps_windows_release_and_is_not_success(self):
        self.world.mac_fail = True
        with self.assertRaisesRegex(local.ReleaseError, 'Windows assets remain published'):
            self.execute()
        self.assertEqual(len(self.world.release['assets']), 2)
        self.assertFalse(any('delete' in args for args in self.commands()))

    SIGNING = dict(
        post_release_workflows=[dict(workflow='release-windows.yml', tag_input='tag')],
        signed_update_assets=['{product}_{version}_{architecture}-setup.exe', '{product}_{version}_{architecture}_en-US.msi'],
        signed_update_suffixes=['.manifest.json', '.signature.json'],
    )

    def enable_signing(self, workflow=True):
        profile = json.loads(self.profile.read_text())
        profile.update(self.SIGNING)
        self.profile.write_text(json.dumps(profile))
        if workflow:
            (self.root / '.github/workflows/release-windows.yml').write_text('on:\n  workflow_dispatch:\n    inputs:\n      tag:\n')
        names = ['Sample.App_1.0.1_x64-setup.exe', 'Sample.App_1.0.1_x64_en-US.msi']
        self.world.signed_files = [name + suffix for name in names for suffix in ('.manifest.json', '.signature.json')]

    def test_post_release_workflow_is_dispatched_with_the_mac_build_and_signed_files_are_verified(self):
        self.enable_signing()
        self.assertTrue(self.execute())
        dispatches = [args[1] for args, options in self.world.calls if args[0] == 'API' and args[1].endswith('/dispatches')]
        self.assertEqual(dispatches, ['actions/workflows/build-macos.yml/dispatches', 'actions/workflows/release-windows.yml/dispatches'])
        bodies = [options['body'] for args, options in self.world.calls if args[0] == 'API' and args[1].endswith('/dispatches')]
        self.assertEqual(bodies, [dict(ref='v1.0.1', inputs={'tag': 'v1.0.1'})] * 2)
        monitored = [options['runs'] for args, options in self.world.calls if args == ['MONITOR']]
        self.assertEqual([[run['workflow'] for run in runs] for runs in monitored], [['build-macos.yml', 'release-windows.yml']])
        self.assertEqual(len(self.world.release['assets']), 8)
        self.assertTrue(all(run['show_progress'] for runs in monitored for run in runs), 'macOS and post-release builds show step progress')

    def dispatch_calls(self):
        return [args[1] for args, _ in self.world.calls if args[0] == 'API' and args[1].endswith('/dispatches')]

    def test_a_failed_push_stops_everything_after_it_and_is_never_reported_as_success(self):
        self.world.fail_push = True
        with self.assertRaisesRegex(local.ReleaseError, 'git push failed'):
            self.execute()
        self.assertFalse([args for args in self.commands() if args[:3] == ['gh', 'release', 'create'] or args[:2] == ['npx', 'tauri']])
        self.assertEqual(self.dispatch_calls(), [])
        self.assertFalse(any(event['kind'] == 'done' for event in self.events.queue))

    def test_no_build_is_dispatched_when_the_remote_tag_does_not_point_at_the_released_commit(self):
        self.world.tag_after_release = 'f' * 40
        with self.assertRaisesRegex(local.ReleaseError, 'Remote release tag no longer points'):
            self.execute()
        self.assertEqual(self.dispatch_calls(), [])
        self.assertFalse(any(args == ['MONITOR'] for args in self.commands()))

    def test_a_dispatch_is_never_reported_as_a_finished_release(self):
        self.world.mac_fail = True
        with self.assertRaisesRegex(local.ReleaseError, 'did not complete successfully'):
            self.execute()
        self.assertTrue(self.dispatch_calls(), 'the build was requested')
        self.assertFalse(any(event['kind'] == 'done' for event in self.events.queue))

    def test_release_without_the_signed_update_files_is_not_reported_as_success(self):
        self.enable_signing()
        self.world.signed_files = self.world.signed_files[:2]
        with self.assertRaisesRegex(local.ReleaseError, r'x64_en-US\.msi\.manifest\.json'):
            self.execute()

    def test_vault_spend_shaped_local_full_release_publishes_latest_first_then_fails_without_windows_signed_files(self):
        """VS-21 audit. The hosted release-windows.yml refuses a tag that already has a release (assert-draft-release.mjs --stage before), so after the
        local pipeline has published, the signed Windows files can never arrive. The fake hosted run adds none, as the real one would."""
        self.enable_signing()
        self.world.signed_files = []
        with self.assertRaisesRegex(local.ReleaseError, r'missing expected nonempty assets.*manifest\.json'):
            self.execute()
        create = next(args for args in self.commands() if args[:3] == ['gh', 'release', 'create'])
        self.assertIn('--latest', create)
        self.assertNotIn('--draft', create)
        self.assertFalse(self.world.release['draft'], 'already public when the failure is reported')
        self.assertFalse([a['name'] for a in self.world.release['assets'] if a['name'].endswith(('.manifest.json', '.signature.json'))])
        self.assertFalse(any(event['kind'] == 'done' for event in self.events.queue))

    def test_signed_files_are_not_required_when_the_profile_does_not_list_any(self):
        self.assertTrue(self.execute())
        self.assertEqual(len(self.world.release['assets']), 4)

    def test_post_release_workflow_must_be_manual_only_and_present_before_the_build(self):
        self.enable_signing(workflow=False)
        with self.assertRaises(local.ReleaseError):
            self.execute()
        self.assertNotIn(['npx', 'tauri', 'build'], self.commands())
        (self.root / '.github/workflows/release-windows.yml').write_text('on:\n  push:\n  workflow_dispatch:\n')
        self.world = World(self.root)
        self.enable_signing(workflow=False)
        self.request['token'] = 'offline-secret'
        with self.assertRaisesRegex(local.ReleaseError, 'manual-only'):
            self.execute()
        self.assertNotIn(['npx', 'tauri', 'build'], self.commands())

    def test_signing_profile_keys_are_validated(self):
        profile = json.loads(self.profile.read_text())
        for key, bad in [('post_release_workflows', 'release-windows.yml'),
                         ('post_release_workflows', [dict(workflow='../x.yml', tag_input='tag')]),
                         ('post_release_workflows', [dict(workflow='release-windows.yml', tag_input='bad name')]),
                         ('signed_update_assets', 'setup.exe'),
                         ('signed_update_suffixes', [''])]:
            with self.subTest(key=key, bad=bad):
                self.profile.write_text(json.dumps(dict(profile, **{key: bad})))
                with self.assertRaises(local.ReleaseError):
                    local.load_profile(str(self.profile))

    def test_wrong_latest_is_not_reported_as_success(self):
        self.world.wrong_latest = True
        with self.assertRaisesRegex(local.ReleaseError, 'releases/latest'):
            self.execute()

    def test_line_ending_only_changes_refresh_index_without_changing_tree(self):
        runner = local.Runner(self.root, 'offline-secret', self.events)
        with patch.object(runner, 'text', side_effect=[' M manifest.json', 'same-tree', 'same-tree', '']), patch.object(runner, 'run', return_value=(0, '')) as run:
            self.assertEqual(local.tracked_changes(runner), '')
        self.assertIn(['git', 'add', '--update', '--', ':/'], [call.args[0] for call in run.call_args_list])

    def test_substantive_changes_are_never_staged_by_index_refresh(self):
        runner = local.Runner(self.root, 'offline-secret', self.events)
        with patch.object(runner, 'text', return_value=' M manifest.json'), patch.object(runner, 'run', side_effect=[(0, ''), (1, '')]) as run:
            self.assertEqual(local.tracked_changes(runner), ' M manifest.json')
        self.assertFalse(any(call.args[0][:2] == ['git', 'add'] for call in run.call_args_list))

    def test_concurrent_index_content_change_stops_release(self):
        runner = local.Runner(self.root, 'offline-secret', self.events)
        with patch.object(runner, 'text', side_effect=[' M manifest.json', 'old-tree', 'new-tree']), patch.object(runner, 'run', return_value=(0, '')):
            with self.assertRaisesRegex(local.ReleaseError, 'Files changed'):
                local.tracked_changes(runner)

    def test_dirty_checkout_error_names_repository_and_changed_file(self):
        runner = local.Runner(self.root, 'offline-secret', self.events)
        with patch.object(runner, 'text', side_effect=['https://github.com/owner/app.git', ' M src/config.json']), patch.object(runner, 'run', return_value=(1, '')):
            with self.assertRaises(local.ReleaseError) as error:
                local.check_origin(runner, 'owner/app')
        self.assertIn(str(self.root), str(error.exception))
        self.assertIn('src/config.json', str(error.exception))
        self.assertIn('substantive changes are retained', str(error.exception))

    def reserved_world(self, tag, mode='local_release'):
        self.request.update(repository='OneTrueAsian/vault-spend', tag=tag, build_mode=mode, token='offline-secret')
        self.world = World(self.root, full=mode in ('local_release', 'local_draft'))
        self.world.origin = 'OneTrueAsian/vault-spend'

    def assert_nothing_prepared(self):
        self.assertEqual(json.loads((self.root / 'package.json').read_text())['version'], '1.0.0')
        self.assertFalse([args for args in self.commands() if args[:2] in (['git', 'commit'], ['git', 'merge'], ['git', 'push'], ['gh', 'release'])])
        self.assertFalse([args for args, kwargs in self.world.calls if args[0] == 'API' and kwargs.get('body')])

    def test_vault_spend_reserved_test_versions_are_refused_before_any_local_or_remote_work(self):
        for mode in ('local_release', 'local_windows'):
            for repository in ('OneTrueAsian/vault-spend', 'onetrueasian/Vault-Spend'):
                for tag in ('v1.3.90', 'v1.3.95', 'v1.3.99', 'v1.3.095', 'v01.3.95'):
                    with self.subTest(mode=mode, repository=repository, tag=tag):
                        self.reserved_world(tag, mode)
                        self.request['repository'] = repository
                        with self.assertRaisesRegex(local.ReleaseError, 'reserved test version'):
                            self.execute()
                        self.assertEqual(self.world.calls, [])
                        self.assert_nothing_prepared()

    def test_reserved_band_is_only_vault_spends_and_supported_versions_still_validate(self):
        for repository, tag in [('OneTrueAsian/vault-spend', 'v1.3.89'), ('OneTrueAsian/vault-spend', 'v1.3.100'), ('OneTrueAsian/vault-spend', 'v1.3.3'),
                                ('OneTrueAsian/vault-spend', 'v1.4.95'), ('OneTrueAsian/vault-spend', 'v1.2.95'), ('owner/app', 'v1.3.95'), ('someone/vault-spend', 'v1.3.95')]:
            with self.subTest(repository=repository, tag=tag):
                # Vault Spend's publishing local modes are refused outright (VaultSpendLocalRestrictionTests); its labelled draft mode still validates its versions.
                local.validate_local_request(dict(self.request, repository=repository, tag=tag, build_mode='local_draft' if repository == 'OneTrueAsian/vault-spend' else self.request['build_mode']))

    def test_a_reserved_latest_release_is_refused_not_used_to_judge_what_is_newer(self):
        for tag in ('v1.3.4', 'v1.3.100'):
            with self.subTest(tag=tag):
                self.reserved_world(tag, 'local_draft')
                self.world.latest_tags = ['v1.3.95']
                with self.assertRaisesRegex(local.ReleaseError, 'releases/latest points at v1.3.95.*reserved test version'):
                    self.execute()
                self.assert_nothing_prepared()

    def test_a_reserved_release_appearing_as_latest_during_the_build_stops_publication(self):
        self.reserved_world('v1.3.4', 'local_draft')
        self.world.latest_tags = [None, 'v1.3.95']
        with self.assertRaisesRegex(local.ReleaseError, 'releases/latest points at v1.3.95.*reserved test version'):
            self.execute()
        self.assertFalse(any(args[:3] == ['gh', 'release', 'create'] for args in self.commands()))

    def test_other_apps_still_compare_against_a_latest_release_in_that_range(self):
        self.world.latest_tags = ['v1.3.95']
        with self.assertRaisesRegex(local.ReleaseError, 'newer than releases/latest'):
            self.execute()

    def test_wrong_repository_stops_before_remote_requests(self):
        self.world.bad_origin = True
        with self.assertRaisesRegex(local.ReleaseError, 'origin does not match'):
            self.execute()
        self.assertFalse(any(args[0] == 'API' for args in self.commands()))

    def test_changelog_parser_preserves_brackets_and_escaped_quotes_in_notes(self):
        notes = ['A [label] stays intact', 'Keep "quoted" words and a backslash \\']
        source = 'const CHANGELOG = {"1.0.0": ' + json.dumps(notes)[:-1] + ',]};'
        self.assertEqual(local.changelog_entry(source, '1.0.0'), notes)

    def test_missing_or_nonobject_profile_has_an_actionable_error(self):
        with self.assertRaisesRegex(local.ReleaseError, 'readable JSON'):
            local.load_profile(str(self.profile) + '.missing')
        self.profile.write_text('[]')
        with self.assertRaisesRegex(local.ReleaseError, 'JSON object'):
            local.load_profile(self.profile)

    def test_profile_path_escape_and_shell_commands_are_rejected(self):
        with self.assertRaises(local.ReleaseError):
            local.inside(self.root, '../other')
        profile = json.loads(self.profile.read_text())
        profile['check_commands'] = ['npm test; publish']
        self.profile.write_text(json.dumps(profile))
        with self.assertRaisesRegex(local.ReleaseError, 'argument arrays'):
            local.load_profile(self.profile)


class VaultSpendLocalRestrictionTests(unittest.TestCase):
    """Owner decision (6 November 2026): for OneTrueAsian/vault-spend, Local: full release and Local: Windows existing tag are refused. The first publishes at once and the hosted
    signing job can never follow; the second uploads unsigned installers to a published release. Other apps and the labelled unsigned local draft are unchanged."""
    def setUp(self):
        LocalPipelineTests.setUp(self)

    execute = LocalPipelineTests.execute
    commands = LocalPipelineTests.commands

    def vault(self, mode, repository='OneTrueAsian/vault-spend', tag='v1.3.4'):
        self.request.update(repository=repository, tag=tag, build_mode=mode, token='offline-secret')
        self.world = World(self.root, full=mode != 'local_windows')
        self.world.origin = repository

    def test_vault_spend_publishing_and_published_release_upload_modes_are_refused_before_any_work(self):
        for mode in ('local_release', 'local_windows'):
            for repository in ('OneTrueAsian/vault-spend', 'onetrueasian/Vault-Spend'):
                with self.subTest(mode=mode, repository=repository):
                    self.vault(mode, repository)
                    with self.assertRaisesRegex(local.ReleaseError, 'Windows \+ macOS: sharded draft'):
                        self.execute()
                    self.assertEqual(self.world.calls, [])
                    self.assertFalse(any(args[:3] == ['gh', 'release', 'create'] or args[:3] == ['gh', 'release', 'upload'] for args in self.commands()))

    def test_the_message_names_the_reason_and_the_supported_path(self):
        self.vault('local_release')
        with self.assertRaises(local.ReleaseError) as error:
            local.validate_local_request(dict(self.request))
        text = str(error.exception)
        self.assertIn('signed', text)
        self.assertIn('Windows + macOS: sharded draft', text)

    def test_the_unsigned_local_draft_stays_available_for_vault_spend(self):
        self.vault('local_draft')
        local.validate_local_request(dict(self.request))

    def test_other_applications_keep_both_modes(self):
        for mode in ('local_release', 'local_windows', 'local_draft'):
            for repository in ('owner/app', 'someone/vault-spend', 'OneTrueAsian/vault-spend-tools'):
                with self.subTest(mode=mode, repository=repository):
                    self.request.update(repository=repository, build_mode=mode, token='offline-secret', tag='v1.3.4')
                    local.validate_local_request(dict(self.request))

    def test_the_reserved_version_message_still_comes_first(self):
        self.vault('local_release', tag='v1.3.95')
        with self.assertRaisesRegex(local.ReleaseError, 'reserved test version'):
            local.validate_local_request(dict(self.request))


class LocalDraftPipelineTests(unittest.TestCase):
    """Local: draft release (VS-21): the same preparation, gates and local build as the full release, but the result is an unpublished draft that is verified as a draft.
    The publishing mode (Local: full release) is untouched and covered above."""
    def setUp(self):
        LocalPipelineTests.setUp(self)
        self.request['build_mode'] = 'local_draft'

    execute = LocalPipelineTests.execute
    commands = LocalPipelineTests.commands

    def test_draft_is_created_unpublished_at_the_merge_sha_and_verified_as_a_draft(self):
        self.assertTrue(self.execute())
        create = next(args for args in self.commands() if args[:3] == ['gh', 'release', 'create'])
        self.assertIn('--draft', create)
        self.assertNotIn('--latest', create)
        self.assertEqual(create[create.index('--target') + 1], 'b' * 40)
        self.assertTrue(self.world.release['draft'])
        self.assertIsNone(self.world.tag, 'no tag exists until the owner publishes')
        self.assertEqual(len(self.world.release['assets']), 2)
        text = str(list(self.events.queue))
        self.assertIn('Windows-only unsigned draft verified', text)
        self.assertIn('not published', text)
        self.assertIn('no signature or digest check', text)
        self.assertIn('not eligible for macOS', text)
        self.assertIn('not a complete release', text)
        self.assertNotIn('Complete draft', text)
        self.assertNotIn('installers published', text)
        self.assertNotIn('offline-secret', text)

    def test_no_macos_or_post_release_workflow_is_dispatched_or_monitored_and_the_limit_is_said(self):
        self.assertTrue(self.execute())
        self.assertFalse(any(args[-1].endswith('/dispatches') or args[0] == 'MONITOR' for args in self.commands()))
        self.assertIn('macOS', str([e.get('message') for e in self.events.queue if e['kind'] == 'log']))
        self.assertEqual(self.request['token'], '')

    def test_macos_workflow_fields_are_not_needed_for_a_draft(self):
        self.request.update(mac_workflow='', tag_input='')
        local.validate_local_request(dict(self.request, token='offline-secret'))

    def test_an_existing_release_of_any_kind_for_the_tag_stops_before_building(self):
        for release in (dict(tag_name='v1.0.1', draft=False, prerelease=False, assets=[]), dict(tag_name='v1.0.1', draft=True, prerelease=False, assets=[], target_commitish='c' * 40)):
            self.setUp()
            self.world.release = release
            with self.subTest(draft=release['draft']), self.assertRaisesRegex(local.ReleaseError, 'already exists'):
                self.execute()
            self.assertNotIn(['npx', 'tauri', 'build'], self.commands())

    def test_a_draft_that_was_published_meanwhile_is_not_reported_as_a_ready_draft(self):
        self.world.publish_after_create = True
        with self.assertRaisesRegex(local.ReleaseError, 'unpublished draft'):
            self.execute()
        self.assertNotIn('draft verified', str(list(self.events.queue)).lower())

    def test_a_draft_missing_a_file_or_with_a_tag_created_elsewhere_fails_verification(self):
        self.world.drop_draft_asset = True
        with self.assertRaisesRegex(local.ReleaseError, 'exactly the configured'):
            self.execute()
        self.setUp()
        self.world.tag_after_release = 'f' * 40
        with self.assertRaisesRegex(local.ReleaseError, 'tag'):
            self.execute()

    def test_a_reserved_vault_spend_version_is_refused_before_anything_runs(self):
        self.request.update(repository='OneTrueAsian/vault-spend', tag='v1.3.95')
        with self.assertRaisesRegex(local.ReleaseError, 'reserved test version'):
            self.execute()
        self.assertEqual(self.commands(), [])

    def test_a_release_that_is_not_newer_than_latest_still_stops_the_draft(self):
        self.world.latest_tags = ['v1.0.1']
        with self.assertRaisesRegex(local.ReleaseError, 'newer'):
            self.execute()
        self.assertNotIn(['npx', 'tauri', 'build'], self.commands())

    def test_verify_draft_release_requires_one_unpublished_production_draft_at_the_sha_with_exactly_the_files(self):
        def good(**over):
            return dict(dict(tag_name='v1.0.1', draft=True, prerelease=False, target_commitish='a' * 40, html_url='https://github.com/owner/app/releases/tag/untagged-1',
                             assets=[dict(name='A.exe', size=3), dict(name='B.msi', size=3)]), **over)
        def check(releases, **kw):
            runner = type('R', (), {'api': lambda self, repository, path, body=None, missing=False: (releases if path.startswith('releases?') and path.endswith('page=1') else [] if path.startswith('releases?') else None if path.startswith('git/ref') else None if missing else {})})()
            return local.verify_draft_release(runner, 'owner/app', 'v1.0.1', kw.get('sha', 'a' * 40), ['A.exe', 'B.msi'])
        self.assertEqual(check([good()])['tag_name'], 'v1.0.1')
        for label, releases, kw in [('none', [], {}), ('two', [good(), good()], {}), ('published', [good(draft=False)], {}), ('prerelease', [good(prerelease=True)], {}),
                                    ('other commit', [good()], {'sha': 'b' * 40}), ('empty file', [good(assets=[dict(name='A.exe', size=0), dict(name='B.msi', size=3)])], {}),
                                    ('extra file', [good(assets=good()['assets'] + [dict(name='C.zip', size=3)])], {}), ('missing file', [good(assets=good()['assets'][:1])], {})]:
            with self.subTest(label), self.assertRaises(local.ReleaseError):
                check(releases, **kw)


class RunnerTests(unittest.TestCase):
    def test_failure_retries_once_and_token_is_only_in_gh_environment(self):
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO('offline-secret failure\n')
            def wait(self): return 1
            def poll(self): return 1
        events = queue.Queue()
        runner = local.Runner(Path.cwd(), 'offline-secret', events)
        with patch.object(local.subprocess, 'Popen', side_effect=lambda *a, **kw: Process()) as popen:
            with self.assertRaises(local.CommandError):
                runner.run(['gh','auth','status'], retry=True)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args.kwargs['env']['GH_TOKEN'], 'offline-secret')
        self.assertNotIn('offline-secret', str(popen.call_args.args))
        self.assertNotIn('offline-secret', str(list(events.queue)))

    def test_build_environment_does_not_receive_github_credentials(self):
        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO('ok\n')
            def wait(self): return 0
            def poll(self): return 0
        runner = local.Runner(Path.cwd(), 'offline-secret', queue.Queue())
        with patch.dict(local.os.environ, {'GH_TOKEN':'old-secret', 'GITHUB_TOKEN':'another-secret', 'GIT_TRACE_CURL':'1'}), patch.object(local.subprocess, 'Popen', return_value=Process()) as popen:
            runner.run(['node', '--version'])
        env = popen.call_args.kwargs['env']
        self.assertNotIn('GH_TOKEN', env)
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotIn('GIT_TRACE_CURL', env)

    def test_powershell_bridge_treats_metacharacters_as_data(self):
        import os
        import subprocess
        script = local.HERE / 'Run-LocalCommand.ps1'
        exe = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        argument = 'literal;$(do-not-run)&value'
        request = dict(directory=str(Path.cwd()), command=['node', '-e', 'console.log(JSON.stringify(process.argv.slice(1)))', '--', argument])
        result = subprocess.run([str(exe), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)], input=json.dumps(request)+'\n', capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), [argument])

    def test_root_api_has_no_trailing_slash(self):
        runner = local.Runner(Path.cwd(), 'offline-secret', queue.Queue())
        with patch.object(runner, 'run', return_value=(0, '{}')) as run:
            runner.api('owner/app', '')
        self.assertEqual(run.call_args.args[0][2], 'repos/owner/app')


if __name__ == '__main__':
    unittest.main()
