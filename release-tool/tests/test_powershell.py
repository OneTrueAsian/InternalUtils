import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ui


class PowerShellIntegrationTests(unittest.TestCase):
    def scenario(self, name):
        command = release_ui.powershell_command()
        command[-2:] = [str(Path(__file__).with_name('mock_release.ps1')), '-Scenario', name]
        request = dict(repository='owner/repo', ref='main', tag='v1.2.3', token='offline-secret',
                       windows_workflow='release-windows.yml', mac_workflow='build-macos.yml', tag_input='tag')
        result = subprocess.run(command, input=json.dumps(request) + '\n', text=True,
                                encoding='utf-8', capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertNotIn('offline-secret', result.stdout + result.stderr)
        self.assertFalse(result.stderr, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        posts = [c for c in calls if c['method'] == 'POST']
        return result, events, posts

    def test_new_tag_pins_both_workflows_to_same_commit(self):
        result, events, posts = self.scenario('new')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 3)
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        self.assertEqual(calls[0]['uri'], 'https://api.github.com/repos/owner/repo')
        tag_body = json.loads(posts[0]['body'])
        self.assertEqual(tag_body, {'ref': 'refs/tags/v1.2.3', 'sha': 'abc123'})
        for dispatch in posts[1:]:
            self.assertEqual(json.loads(dispatch['body']), {'ref': 'v1.2.3', 'inputs': {'tag': 'v1.2.3'}})

    def test_same_commit_tag_and_annotated_tag_can_resume(self):
        for name in ('same', 'annotated'):
            result, events, posts = self.scenario(name)
            self.assertEqual(result.returncode, 0, events)
            self.assertEqual(len(posts), 2)
            self.assertTrue(all(c['uri'].endswith('/dispatches') for c in posts))

    def test_conflicting_tag_never_changes_remote_state(self):
        result, events, posts = self.scenario('different')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(posts, [])
        self.assertTrue(any('different commit' in e.get('message', '') for e in events))

    def test_both_workflows_preflight_before_tag_creation(self):
        result, events, posts = self.scenario('missing')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(posts, [])

    def test_partial_failure_retains_tag_and_reports_failure(self):
        result, events, posts = self.scenario('partial')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(posts), 2)
        self.assertTrue(any('Tag retained' in e.get('message', '') for e in events))
        self.assertFalse(any(e['kind'] == 'done' for e in events))

    def test_auto_triggered_macos_run_is_not_dispatched_again(self):
        result, events, posts = self.scenario('automatic')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        self.assertIn('/11/dispatches', posts[0]['uri'])
        self.assertTrue(any('already started' in e.get('message', '') for e in events))

    def test_failed_tag_warns_and_preserves_run_baseline(self):
        result, events, posts = self.scenario('failed')
        self.assertEqual(result.returncode, 0, events)
        self.assertTrue(any('SAME commit' in e.get('message', '') for e in events))
        self.assertTrue(all(10 in e['previous_run_ids'] for e in events if e['kind'] == 'run'))

    def test_total_dispatch_failure_has_no_release_link(self):
        result, events, posts = self.scenario('bothfail')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any('/releases/tag/' in e.get('url', '') for e in events))

    def test_api_error_details_are_redacted(self):
        result, events, posts = self.scenario('error422')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any('Unexpected input [redacted]' in e.get('message', '') for e in events))

    def test_push_trigger_is_rejected_before_mutation(self):
        result, events, posts = self.scenario('push')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(posts, [])
        self.assertTrue(any('push trigger' in e.get('message', '') for e in events))

    def test_real_script_launch_from_directory_with_spaces_is_offline(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='release tool launch ') as folder:
            script = Path(folder) / 'Start-GitHubRelease.ps1'
            script.write_bytes(release_ui.SCRIPT.read_bytes())
            with patch.object(release_ui, 'SCRIPT', script):
                command = release_ui.powershell_command()
            self.assertEqual(command[-2], str(script.resolve()))
            request = dict(repository='invalid', ref='', tag='', token='offline-secret',
                           windows_workflow='release-windows.yml', mac_workflow='build-macos.yml', tag_input='tag')
            result = subprocess.run(command, input=json.dumps(request) + '\n', text=True,
                                    encoding='utf-8', capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.stderr, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            self.assertEqual(events[0]['kind'], 'error')
            self.assertEqual(events[0]['message'], 'Repository must use owner/repo format.')
            self.assertNotIn('offline-secret', result.stdout)

    def test_missing_companion_script_is_detected_before_launch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'Start-GitHubRelease.ps1'
            with patch.object(release_ui, 'SCRIPT', missing):
                with self.assertRaisesRegex(RuntimeError, 'Release script is missing'):
                    release_ui.powershell_command()


    def test_repository_404_identifies_target_and_does_not_mutate_state(self):
        result, events, posts = self.scenario('repository404')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(posts, [])
        errors = [e['message'] for e in events if e['kind'] == 'error']
        self.assertEqual(len(errors), 1)
        self.assertIn('repository owner/repo (HTTP 404)', errors[0])
        self.assertIn('token resource owner', errors[0])
        self.assertIn('Private repositories', errors[0])
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        self.assertEqual(len(calls), 1)



if __name__ == '__main__':
    unittest.main()
