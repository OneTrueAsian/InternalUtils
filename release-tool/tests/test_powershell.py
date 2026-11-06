import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ui


class PowerShellIntegrationTests(unittest.TestCase):
    def test_sharded_draft_dispatches_only_windows_with_exact_source_and_safe_flags(self):
        result, events, posts = self.scenario('draft', build_mode='windows_sharded', mac_workflow='')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]['body']), {'ref': 'v1.2.3', 'inputs': {
            'tag': 'v1.2.3', 'source_sha': 'a' * 40, 'sharded': True,
            'recovery': False, 'diagnostic_only': False, 'test_release': False}})
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        self.assertFalse(any('build-macos' in c['uri'] for c in calls))

    def test_serial_draft_has_explicit_false_sharded_input(self):
        result, events, posts = self.scenario('draft', build_mode='windows_serial_draft', mac_workflow='')
        self.assertEqual(result.returncode, 0, events)
        self.assertIs(json.loads(posts[0]['body'])['inputs']['sharded'], False)

    def test_draft_refuses_incompatible_workflows_and_test_tags_before_mutation(self):
        for scenario, changes in [('draftmissing', {}), ('draftpublishing', {}),
                                  ('draft', {'tag': 'v1.3.92'}), ('draft', {'tag_input': ''})]:
            result, events, posts = self.scenario(scenario, build_mode='windows_sharded', mac_workflow='', **changes)
            self.assertEqual(result.returncode, 1, events)
            self.assertEqual(posts, [])

    def test_draft_does_not_reuse_a_same_commit_run_of_unknown_gate_mode(self):
        result, events, posts = self.scenario('draftreuse', build_mode='windows_sharded', mac_workflow='')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        run = next(e for e in events if e['kind'] == 'run')
        self.assertIn(99, run['previous_run_ids'])
        self.assertNotEqual(run['run_id'], 99)

    def test_legacy_both_mode_cannot_dispatch_macos_alongside_windows_draft(self):
        result, events, posts = self.scenario('draftmac')
        self.assertEqual(result.returncode, 1, events)
        self.assertEqual(posts, [])
        self.assertTrue(any('wait until Windows has finished and signed' in e.get('message', '') for e in events))

    def test_active_draft_run_is_not_dispatched_twice(self):
        result, events, posts = self.scenario('draftactive', build_mode='windows_sharded', mac_workflow='')
        self.assertEqual(result.returncode, 1, events)
        self.assertEqual(posts, [])
        self.assertTrue(any('already active' in e.get('message', '') for e in events))

    # ---- Windows phase of the Windows + macOS draft modes: macOS is validated but never dispatched here ----
    def test_windows_phase_validates_the_macos_workflow_but_dispatches_only_windows(self):
        result, events, posts = self.scenario('draftmacA', build_mode='windows_sharded', include_macos=True)
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        self.assertIn('/11/dispatches', posts[0]['uri'])
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        self.assertTrue(any('build-macos.yml' in c['uri'] and 'contents' in c['uri'] for c in calls), 'the macOS workflow was inspected at the source commit')
        self.assertFalse(any('/22/dispatches' in c['uri'] for c in calls))
        self.assertTrue(any('macOS will be added' in e.get('message', '') for e in events), events)

    def test_a_macos_workflow_without_the_draft_contract_is_refused_before_any_change(self):
        result, events, posts = self.scenario('draftmacAlegacy', build_mode='windows_sharded', include_macos=True)
        self.assertEqual(result.returncode, 1, events)
        self.assertEqual(posts, [])
        self.assertTrue(any('draft policy' in e.get('message', '') for e in events), events)

    def test_the_legacy_both_mode_still_refuses_to_run_macos_alongside_a_windows_draft_and_points_to_the_new_mode(self):
        result, events, posts = self.scenario('draftmac')
        self.assertEqual(result.returncode, 1, events)
        self.assertEqual(posts, [])
        self.assertTrue(any('Windows + macOS' in e.get('message', '') and 'draft' in e.get('message', '') for e in events), events)

    # ---- macOS phase: joins the finished Windows draft by release id; every doubt stops it before a dispatch ----
    def macos(self, name, **changes):
        request = dict(build_mode='macos_draft', source_sha='a' * 40, release_id=77)
        request.update(changes)
        return self.scenario(name, **request)

    def test_macos_is_dispatched_once_for_the_exact_tag_after_every_check_passes(self):
        result, events, posts = self.macos('mbok')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        self.assertIn('/22/dispatches', posts[0]['uri'])
        self.assertEqual(json.loads(posts[0]['body']), {'ref': 'v1.2.3', 'inputs': {'tag': 'v1.2.3'}})
        run = next(e for e in events if e['kind'] == 'run')
        self.assertEqual((run['workflow'], run['head_sha'], run['source_sha'], run['run_id'], run['run_ref']), ('build-macos.yml', 'a' * 40, 'a' * 40, 200, 'v1.2.3'))
        self.assertFalse(any(c['method'] not in ('GET', 'POST') for c in next(e['calls'] for e in events if e['kind'] == 'test_calls')))

    def test_every_doubt_about_the_draft_stops_macos_before_a_dispatch(self):
        cases = {'mbpublished': 'published', 'mbprerelease': 'prerelease', 'mbwrongid': 'different release', 'mbdup': 'exactly one',
                 'mbunsigned': 'signed set|Windows', 'mbtagmoved': 'no longer points', 'mblegacy': 'draft policy'}
        for name, pattern in cases.items():
            with self.subTest(name):
                result, events, posts = self.macos(name)
                self.assertEqual(result.returncode, 1, events)
                self.assertEqual(posts, [], 'no dispatch of any kind')
                self.assertRegex(' '.join(e.get('message', '') for e in events), pattern)

    def test_inputs_that_would_bind_macos_to_nothing_are_refused(self):
        for changes in (dict(source_sha=''), dict(source_sha='main'), dict(release_id=0), dict(release_id=None), dict(tag='latest'), dict(mac_workflow='bad name.yml'), dict(tag_input='')):
            with self.subTest(**changes):
                result, events, posts = self.macos('mbok', **changes)
                self.assertEqual(result.returncode, 1, events)
                self.assertEqual(posts, [])

    def test_macos_files_already_in_the_draft_are_never_dispatched_again(self):
        result, events, posts = self.macos('mbassets')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(posts, [])
        self.assertFalse([e for e in events if e['kind'] == 'run'])
        self.assertTrue(any('already' in e.get('message', '') for e in events), events)

    def test_partial_macos_files_are_reported_with_a_safe_next_step_and_nothing_is_dispatched(self):
        result, events, posts = self.macos('mbpartial')
        self.assertEqual(result.returncode, 1, events)
        self.assertEqual(posts, [])
        message = ' '.join(e.get('message', '') for e in events)
        self.assertIn('partly', message)
        self.assertIn('never deletes', message)

    def test_a_macos_run_already_active_is_tracked_not_duplicated(self):
        result, events, posts = self.macos('mbactive')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(posts, [])
        run = next(e for e in events if e['kind'] == 'run')
        self.assertEqual(run['run_id'], 88)

    def test_a_completed_green_macos_run_without_macos_files_is_not_started_again(self):
        result, events, posts = self.macos('mbsuccessrun')
        self.assertEqual(result.returncode, 1, events)
        self.assertEqual(posts, [])
        self.assertTrue(any('already completed' in e.get('message', '') for e in events), events)

    def test_a_failed_earlier_macos_run_only_warns(self):
        result, events, posts = self.macos('mbfailedrun')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        self.assertTrue(any('previously failed' in e.get('message', '') for e in events), events)

    def test_the_release_list_is_paginated_so_a_draft_on_a_later_page_is_found(self):
        result, events, posts = self.macos('mbpages')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        self.assertTrue(any('releases?per_page=100&page=2' in c['uri'] for c in calls))

    def test_vault_spend_reserved_versions_are_refused_in_the_macos_phase_too(self):
        for tag in ('v1.3.90', 'v1.3.95', 'v1.3.095'):
            with self.subTest(tag):
                result, events, posts = self.macos('mbok', repository='OneTrueAsian/vault-spend', tag=tag)
                self.assertEqual(result.returncode, 1, events)
                self.assertEqual(posts, [])
                self.assertTrue(any('reserved test version' in e.get('message', '') for e in events), events)

    def scenario(self, name, **changes):
        command = release_ui.powershell_command()
        command[-2:] = [str(Path(__file__).with_name('mock_release.ps1')), '-Scenario', name]
        request = dict(repository='owner/repo', ref='main', tag='v1.2.3', token='offline-secret',
                       windows_workflow='release-windows.yml', mac_workflow='build-macos.yml', tag_input='tag')
        request.update(changes)
        result = subprocess.run(command, input=json.dumps(request) + '\n', text=True,
                                encoding='utf-8', capture_output=True, timeout=30, env=release_ui.powershell_environment(command[0]),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertNotIn('offline-secret', result.stdout + result.stderr)
        self.assertFalse(result.stderr, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        posts = [c for c in calls if c['method'] == 'POST']
        return result, events, posts

    def test_recovery_dispatches_only_windows_from_branch_without_changing_tag(self):
        result, events, posts = self.scenario('recovery', build_mode='windows_recovery', mac_workflow='')
        self.assertEqual(result.returncode, 0, events)
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]['body']), {'ref': 'main', 'inputs': {'tag': 'v1.2.3', 'recovery': True, 'source_sha': 'abc123'}})
        calls = next(e['calls'] for e in events if e['kind'] == 'test_calls')
        self.assertFalse(any('build-macos' in c['uri'] for c in calls))
        run = next(e for e in events if e['kind'] == 'run')
        self.assertEqual(run['head_sha'], 'def456')
        self.assertEqual(run['source_sha'], 'abc123')
        self.assertEqual(run['run_ref'], 'main')
        self.assertEqual(run['run_title'], 'Recover Windows v1.2.3')

    def test_recovery_requires_existing_tag_and_tag_input(self):
        for changes in ({}, {'tag_input': ''}):
            result, events, posts = self.scenario('new', build_mode='windows_recovery', **changes)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(posts, [])

    def test_vault_spend_reserved_test_versions_are_refused_before_any_remote_change(self):
        for build_mode, mac_workflow in (('both', 'build-macos.yml'), ('windows_recovery', '')):
            for tag in ('v1.3.90', 'v1.3.95', 'v1.3.99', 'v1.3.095', 'v01.3.95'):
                with self.subTest(build_mode=build_mode, tag=tag):
                    result, events, posts = self.scenario('new', repository='OneTrueAsian/vault-spend', tag=tag,
                                                          build_mode=build_mode, mac_workflow=mac_workflow)
                    self.assertEqual(result.returncode, 1, events)
                    self.assertEqual(posts, [])
                    self.assertTrue(any('reserved test version' in e.get('message', '') for e in events), events)

    def test_reserved_band_is_only_vault_spends_and_supported_versions_still_release(self):
        for repository, tag in (('OneTrueAsian/vault-spend', 'v1.3.89'), ('OneTrueAsian/vault-spend', 'v1.3.100'),
                                ('OneTrueAsian/vault-spend', 'v1.3.3'), ('OneTrueAsian/vault-spend', 'v1.4.95'),
                                ('owner/repo', 'v1.3.95')):
            with self.subTest(repository=repository, tag=tag):
                result, events, posts = self.scenario('new', repository=repository, tag=tag)
                self.assertEqual(result.returncode, 0, events)
                self.assertEqual(len(posts), 3)

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
                                    encoding='utf-8', capture_output=True, timeout=15, env=release_ui.powershell_environment(command[0]),
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
