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
                    return 0, 'https://github.com/other/app.git' if world.bad_origin else 'https://github.com/owner/app.git'
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
                    world.tag = args[args.index('--target') + 1]
                    world.release = dict(tag_name=args[3], draft=False, prerelease=False, assets=[dict(name=Path(name).name.replace(' ', '.'), size=13) for name in args[4:args.index('--repo')]])
                if args[:3] == ['gh', 'release', 'upload']:
                    paths = args[4:args.index('--repo')]
                    world.release['assets'].extend(dict(name=Path(name).name.replace(' ', '.'), size=13) for name in paths)
                return 0, ''

            def api(self, repository, path, body=None, missing=False):
                world.calls.append((['API', path], dict(body=body)))
                if path.startswith('git/ref/tags/'):
                    return dict(object=dict(type='commit', sha=world.tag)) if world.tag else None
                if path.startswith('releases/tags/'):
                    return world.release
                if path == 'releases/latest':
                    if not world.release:
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
        profile = json.loads((local.HERE / 'profiles/tauri-vault-spend.json').read_text())
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

    def test_wrong_latest_is_not_reported_as_success(self):
        self.world.wrong_latest = True
        with self.assertRaisesRegex(local.ReleaseError, 'releases/latest'):
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
