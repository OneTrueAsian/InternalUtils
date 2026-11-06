import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import queue
import threading
import unittest
from unittest.mock import patch

import release_monitor


def target(workflow='release-windows.yml', run_id=11):
    return dict(workflow=workflow, run_id=run_id, head_sha='abc', tag='v1.0.0',
                requested_at='2026-10-07T10:00:00Z', url='')


def run(state='completed', conclusion='success', **changes):
    return dict(status=state, conclusion=conclusion, head_sha='abc', head_branch='v1.0.0', **changes)


class MonitorTests(unittest.TestCase):
    def test_recovery_identifies_branch_run_for_the_requested_tag(self):
        events = queue.Queue()
        t = dict(target(run_id=None), run_ref='main', run_title='Recover Windows v1.0.0')
        calls = []
        def get(path):
            calls.append(path)
            if '/workflows/' in path:
                return {'workflow_runs': [dict(id=i, head_sha='abc', head_branch='main', event='workflow_dispatch', display_title=title, created_at='2026-10-07T10:00:01Z') for i, title in [(12, 'Recover Windows v2.0.0'), (13, t['run_title'])]]}
            return dict(run(), head_branch='main', display_title=t['run_title'])
        self.assertTrue(release_monitor.monitor_runs('owner/repo', 'offline-token', [t], events, get=get, interval=0))
        self.assertIn('actions/runs/13', calls)
        self.assertNotIn('actions/runs/12', calls)

    def test_tracks_running_then_both_successes(self):
        events = queue.Queue()
        reads = {11: 0, 22: 0}
        def get(path):
            run_id = int(path.split('/')[-1])
            reads[run_id] += 1
            return run('in_progress', None) if reads[run_id] == 1 else run()
        self.assertTrue(release_monitor.monitor_runs('owner/repo', 'offline-token',
                       [target(), target('build-macos.yml', 22)], events, get=get, interval=0))
        states = [e['state'] for e in events.queue if e['kind'] == 'status']
        self.assertEqual(states.count('in_progress'), 2)
        self.assertEqual(states.count('completed'), 2)
        self.assertEqual(list(events.queue)[-1]['kind'], 'done')

    def test_failure_cancellation_and_timeout_are_not_success(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'skipped', 'neutral', 'action_required'):
            with self.subTest(conclusion=conclusion):
                events = queue.Queue()
                def get(path):
                    return {'jobs': [{'name': 'Build', 'conclusion': 'failure', 'steps': [{'name': 'Compile', 'conclusion': 'failure'}]}]} if '/jobs?' in path else run(conclusion=conclusion)
                self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], events, get=get, interval=0))
                self.assertFalse(any(e['kind'] == 'done' for e in events.queue))
                self.assertTrue(any('Compile' in e.get('message', '') for e in events.queue))

    def test_one_failure_does_not_stop_tracking_other_platform(self):
        events = queue.Queue()
        reads = 0
        def get(path):
            nonlocal reads
            if '/jobs?' in path:
                return {'jobs': []}
            if path.endswith('/11'):
                return run(conclusion='failure')
            reads += 1
            return run('queued', None) if reads == 1 else run()
        self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target(), target('build-macos.yml', 22)], events, get=get, interval=0))
        self.assertEqual(reads, 2)

    def test_missing_dispatch_id_discovers_only_new_matching_run(self):
        events = queue.Queue()
        calls = []
        def get(path):
            calls.append(path)
            if '/workflows/' in path:
                return {'workflow_runs': [dict(id=9, head_sha='abc', head_branch='v1.0.0', event='workflow_dispatch', created_at='2026-10-06T10:00:00Z'),
                                          dict(id=12, head_sha='abc', head_branch='v1.0.0', event='workflow_dispatch', created_at='2026-10-07T10:00:01Z')]}
            return run()
        self.assertTrue(release_monitor.monitor_runs('owner/repo', 'offline-token', [target(run_id=None)], events, get=get, interval=0))
        self.assertIn('actions/runs/12', calls)
        self.assertNotIn('actions/runs/9', calls)

    def test_clock_skew_does_not_reuse_known_runs(self):
        events = queue.Queue()
        t = dict(target(run_id=None), previous_run_ids=[12])
        calls = []
        def get(path):
            calls.append(path)
            if '/workflows/' in path:
                return {'workflow_runs': [dict(id=i, head_sha='abc', head_branch='v1.0.0', event='workflow_dispatch', created_at='2026-10-07T09:59:40Z') for i in (12, 13)]}
            return run()
        self.assertTrue(release_monitor.monitor_runs('owner/repo', 'offline-token', [t], events, get=get, interval=0))
        self.assertIn('actions/runs/13', calls)

    def test_failed_job_excerpt_is_redacted(self):
        events = queue.Queue()
        def get(path):
            return {'jobs': [dict(id=55, name='E2E', conclusion='failure', steps=[])]} if '/jobs?' in path else run(conclusion='failure')
        self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], events, get=get, interval=0, log_get=lambda _: 'Error: ENOENT offline-token'))
        text = str(list(events.queue))
        self.assertIn('ENOENT [redacted]', text)
        self.assertNotIn('offline-token', text)

    def test_transient_status_errors_recover_but_persistent_errors_are_unknown(self):
        events = queue.Queue()
        calls = 0
        def get(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('offline failure')
            return run()
        self.assertTrue(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], events, get=get, interval=0))
        events = queue.Queue()
        with patch.object(release_monitor, 'github_get', side_effect=RuntimeError('offline failure')) as api:
            self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], events, interval=0))
        self.assertEqual(api.call_count, 3)
        self.assertIn('unknown', list(events.queue)[-1]['message'])

    def test_stop_and_monitoring_timeout_do_not_cancel_builds(self):
        stop = threading.Event()
        stop.set()
        with patch.object(release_monitor, 'github_get') as api:
            self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], queue.Queue(), stop=stop))
            api.assert_not_called()
        events = queue.Queue()
        self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], events, timeout=0))
        self.assertIn('timed out', list(events.queue)[-1]['message'])

    def test_wrong_commit_is_not_reported_successful(self):
        events = queue.Queue()
        self.assertFalse(release_monitor.monitor_runs('owner/repo', 'offline-token', [target()], events,
                         get=lambda _: dict(run(), head_sha='wrong'), interval=0))
        self.assertTrue(any('does not match' in e.get('message', '') for e in events.queue))


TOKEN = 'offline-token'


def step(number, name, status, conclusion=None, started=None, completed=None):
    return dict(number=number, name=name, status=status, conclusion=conclusion, started_at=started, completed_at=completed)


def logs(events):
    return [e['message'] for e in events.queue if e['kind'] == 'log']


class StepProgressTests(unittest.TestCase):
    """VS-2: step progress only (current and finished steps), for Windows and macOS runs, one message per change, never raw logs."""

    def watch(self, targets, polls, **kwargs):
        """polls[n] maps a run id to (run state, conclusion, jobs) as GitHub reports it on poll n; the last entry repeats."""
        events = queue.Queue()
        seen = {'poll': 0, 'paths': []}
        def get(path):
            seen['paths'].append(path)
            run_id = int(path.split('/')[2])
            current = polls[min(seen['poll'], len(polls) - 1)][run_id]
            if '/jobs?' in path:
                if run_id == targets[-1]['run_id']:
                    seen['poll'] += 1
                if isinstance(current[2], Exception):
                    raise current[2]
                return {'jobs': current[2]}
            return run(current[0], current[1], run_attempt=1)
        def no_raw_logs(_job_id):
            raise AssertionError('raw job logs must not be read while a build is progressing')
        kwargs.setdefault('timeout', 3)  # a monitor that never sees the builds finish must fail the test, not hang it
        result = release_monitor.monitor_runs('owner/repo', TOKEN, targets, events, get=get, interval=0, log_get=no_raw_logs, **kwargs)
        return result, events, seen

    def windows_and_macos(self):
        return [dict(target('release-windows.yml', 11), show_progress=True), dict(target('build-macos.yml', 22), show_progress=True)]

    def test_state_changes_and_completed_durations_are_reported_once_for_windows_and_macos(self):
        def windows(stage):
            if stage == 0:
                return [dict(id=1, name='Build', status='in_progress', conclusion=None, steps=[
                    step(1, 'Checkout', 'completed', 'success', '2026-11-05T10:00:00Z', '2026-11-05T10:00:04Z'),
                    step(2, 'Compile', 'in_progress', None, '2026-11-05T10:00:04Z'),
                    step(3, 'Package', 'pending'), step(4, 'Upload', 'queued')])]
            return [dict(id=1, name='Build', status='completed', conclusion='success', started_at='2026-11-05T10:00:00Z', completed_at='2026-11-05T10:05:00Z', steps=[
                step(1, 'Checkout', 'completed', 'success', '2026-11-05T10:00:00Z', '2026-11-05T10:00:04Z'),
                step(2, 'Compile', 'completed', 'success', '2026-11-05T10:00:04Z', '2026-11-05T10:04:16Z'),
                step(3, 'Package', 'completed', 'success', '2026-11-05T10:04:16Z', '2026-11-05T10:04:46Z'),
                step(4, 'Upload', 'completed', 'success', '2026-11-05T10:04:46Z', '2026-11-05T10:05:00Z')])]
        def macos(stage):
            return [dict(id=2, name='Notarize', status='in_progress' if stage == 0 else 'completed', conclusion=None if stage == 0 else 'success', steps=[
                step(1, 'Sign app', 'in_progress' if stage == 0 else 'completed', None if stage == 0 else 'success', '2026-11-05T10:00:00Z', None if stage == 0 else '2026-11-05T10:02:00Z')])]
        polls = [{11: ('in_progress', None, windows(0)), 22: ('in_progress', None, macos(0))},
                 {11: ('in_progress', None, windows(0)), 22: ('in_progress', None, macos(0))},
                 {11: ('completed', 'success', windows(1)), 22: ('completed', 'success', macos(1))}]
        result, events, _ = self.watch(self.windows_and_macos(), polls)
        self.assertTrue(result)
        messages = logs(events)
        self.assertEqual([m for m in messages if m.startswith('release-windows.yml')], [
            'release-windows.yml: Build: in progress',
            'release-windows.yml: Build > Checkout: success (0m 4s)',
            'release-windows.yml: Build > Compile: in progress',
            'release-windows.yml: Build: success (5m 0s)',
            'release-windows.yml: Build > Compile: success (4m 12s)',
            'release-windows.yml: Build > Package: success (0m 30s)',
            'release-windows.yml: Build > Upload: success (0m 14s)'])
        self.assertEqual([m for m in messages if m.startswith('build-macos.yml')], [
            'build-macos.yml: Notarize: in progress',
            'build-macos.yml: Notarize > Sign app: in progress',
            'build-macos.yml: Notarize: success',
            'build-macos.yml: Notarize > Sign app: success (2m 0s)'])
        self.assertEqual(len(messages), len(set(messages)), 'a repeated poll must not repeat a message')

    def test_steps_that_have_not_started_are_not_listed(self):
        jobs = [dict(id=1, name='Build', status='in_progress', conclusion=None, steps=[step(1, 'Later', 'pending'), step(2, 'Queued', 'queued')])]
        result, events, _ = self.watch([dict(target(), show_progress=True)], [{11: ('completed', 'success', jobs)}])
        self.assertFalse(any('Later' in m or 'Queued' in m for m in logs(events)))

    def test_progress_is_only_read_for_runs_that_ask_for_it(self):
        result, events, seen = self.watch([target()], [{11: ('completed', 'success', [])}])
        self.assertTrue(result)
        self.assertFalse(any('/jobs' in path for path in seen['paths']))

    def test_an_unreadable_job_listing_does_not_make_a_progress_only_run_unknown(self):
        t = dict(target(), show_progress=True)
        polls = [{11: ('in_progress', None, RuntimeError('jobs gone'))}, {11: ('in_progress', None, RuntimeError('jobs gone'))},
                 {11: ('completed', 'success', RuntimeError('jobs gone'))}]
        result, events, _ = self.watch([t], polls)
        self.assertTrue(result)
        self.assertEqual(sum('step progress is unavailable' in m for m in logs(events)), 1)
        self.assertFalse(any(e.get('state') == 'unknown' for e in events.queue))

    def test_names_cannot_inject_terminal_control_or_leak_the_token(self):
        hostile = 'Build \x1b[31mred\x1b[0m \x1b]0;owned\x07 \x1b[2J\x1b[Hmoved\rhidden\x08\x08 offl\x1b[1mine-token \x9b'
        jobs = [dict(id=1, name=hostile, status='in_progress', conclusion=None, steps=[step(1, hostile, 'in_progress', None, '2026-11-05T10:00:00Z')])]
        result, events, _ = self.watch([dict(target(), show_progress=True)], [{11: ('completed', 'success', jobs)}])
        text = ''.join(logs(events))
        self.assertNotRegex(text, '[\x00-\x08\x0b-\x1f\x7f-\x9f]')
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('owned', text)
        self.assertIn('[redacted]', text)
        self.assertIn('red', text)

    def test_a_failed_job_message_is_cleaned_too(self):
        jobs = {'jobs': [dict(id=7, name='Bu\x1b[2Jild \x1b]0;x\x07offl\x1b[1mine-token', conclusion='failure', steps=[dict(number=1, name='Co\x1b[1mmpile', conclusion='failure')])]}
        events = queue.Queue()
        get = lambda path: jobs if '/jobs?' in path else run(conclusion='failure')
        self.assertFalse(release_monitor.monitor_runs('owner/repo', TOKEN, [target()], events, get=get, interval=0, log_get=lambda _: ''))
        text = ''.join(logs(events))
        self.assertIn('Failed job: Build [redacted]; failed step: Compile', text)
        self.assertNotRegex(text, '[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def job(name, status='completed', conclusion='success', job_id=None):
    return dict(id=job_id or abs(hash(name)) % 10**6, name=name, status=status, conclusion=conclusion, steps=[])


def sequence(items):
    """A reader that returns each item once, then the last one forever."""
    state = {'n': 0}
    def next_item():
        item = items[min(state['n'], len(items) - 1)]
        state['n'] += 1
        return item
    return next_item


class StaleStatusAndGateEvidenceTests(unittest.TestCase):
    """VS-15: a stale or early status never becomes success; job results and gate evidence must agree first."""

    def monitor(self, t, run_states, jobs_for, artifacts_for=None, **kwargs):
        events, paths = queue.Queue(), []
        next_run, next_jobs = sequence(run_states), sequence(jobs_for)
        def get(path):
            paths.append(path)
            if '/artifacts' in path:
                return artifacts_for(path)
            if '/jobs?' in path:
                return {'jobs': next_jobs()}
            return next_run()
        kwargs.setdefault('timeout', 5)
        result = release_monitor.monitor_runs('owner/repo', TOKEN, [t], events, get=get, interval=0, log_get=lambda _: '', **kwargs)
        return result, events, paths

    def test_a_run_still_reported_queued_or_in_progress_is_not_success_even_when_every_job_finished(self):
        t = dict(target(), track_jobs=True)
        result, events, _ = self.monitor(t, [run('queued', None), run('in_progress', None), run()], [[job('build')]])
        self.assertTrue(result)
        states = [e['state'] for e in events.queue if e['kind'] == 'status']
        self.assertEqual(states, ['queued', 'in_progress', 'completed'])

    def test_a_completed_run_without_a_conclusion_yet_is_still_pending(self):
        result, events, _ = self.monitor(target(), [run('completed', None), run('completed', None), run()], [[]])
        self.assertTrue(result)
        messages = [e['message'] for e in events.queue if e['kind'] == 'status']
        self.assertEqual(messages, ['release-windows.yml: result pending', 'release-windows.yml: success'])

    def test_a_success_while_jobs_still_look_active_waits_names_them_once_and_applies_to_every_mode(self):
        for flags in (dict(show_progress=True), dict(track_jobs=True), dict(track_jobs=True, show_progress=True)):
            with self.subTest(flags=flags):
                t = dict(target(), **flags)
                stale = [job('shard 5 of 6'), job('shard 6 of 6', 'in_progress', None)]
                settled = [job('shard 5 of 6'), job('shard 6 of 6')]
                result, events, _ = self.monitor(t, [run()], [stale, stale, stale, settled])
                self.assertTrue(result)
                waiting = [e['message'] for e in events.queue if e.get('state') == 'waiting']
                self.assertEqual(len(waiting), 1, 'the same wait is not repeated every poll')
                self.assertIn('shard 6 of 6', waiting[0])
                self.assertNotIn('shard 5 of 6', waiting[0])

    def test_a_job_that_never_settles_ends_as_not_confirmed_not_success(self):
        t = dict(target(), track_jobs=True)
        result, events, _ = self.monitor(t, [run()], [[job('shard 6 of 6', 'in_progress', None)]], timeout=0.2)
        self.assertFalse(result)
        self.assertFalse(any(e['kind'] == 'done' for e in events.queue))
        self.assertTrue(any('timed out' in e.get('message', '') for e in events.queue))

    def test_a_failed_run_reports_failing_jobs_from_every_page_of_the_runs_own_attempt(self):
        jobs = [job(f'shard {n}', job_id=n) for n in range(1, 101)] + [job('shard 101', conclusion='failure', job_id=101)]
        paths = []
        def get(path):
            paths.append(path)
            if '/jobs?' in path:
                return {'jobs': jobs[100:] if path.endswith('page=2') else jobs[:100]}
            return run(conclusion='failure', run_attempt=2)
        events = queue.Queue()
        self.assertFalse(release_monitor.monitor_runs('owner/repo', TOKEN, [target()], events, get=get, interval=0, log_get=lambda _: ''))
        self.assertIn('Failed job: shard 101', ''.join(logs(events)))
        self.assertTrue(any('/attempts/2/jobs' in p for p in paths))

    def test_required_jobs_must_have_succeeded_not_been_skipped_cancelled_or_failed(self):
        for conclusion in ('skipped', 'cancelled', 'failure', 'timed_out', 'neutral', None):
            with self.subTest(conclusion=conclusion):
                t = dict(target(), track_jobs=True, required_jobs=['sharded-package'])
                jobs = [job('build'), job('sharded-package', conclusion=conclusion)]
                result, events, _ = self.monitor(t, [run()], [jobs])
                self.assertFalse(result)
                self.assertFalse(any(e['kind'] == 'done' for e in events.queue))
                self.assertIn('sharded-package', ''.join(e.get('message', '') for e in events.queue))

    def test_a_failed_job_outside_the_required_list_still_blocks_a_gate_run(self):
        t = dict(target(), track_jobs=True, required_jobs=['sharded-package'])
        result, events, _ = self.monitor(t, [run()], [[job('sharded-package'), job('lint', conclusion='failure')]])
        self.assertFalse(result)
        self.assertIn('failed despite', ''.join(e.get('message', '') for e in events.queue))

    def test_waiting_for_approval_is_shown_once_and_the_run_still_completes(self):
        t = dict(target(), show_progress=True)
        waiting = [job('sign', 'waiting', None)]
        result, events, _ = self.monitor(t, [run('waiting', None), run('waiting', None), run('waiting', None), run()], [waiting, waiting, waiting, [job('sign')]])
        self.assertTrue(result)
        self.assertEqual([e['state'] for e in events.queue if e['kind'] == 'status'], ['waiting', 'completed'])
        self.assertEqual(sum('sign: waiting' in m for m in logs(events)), 1)

    def gate(self):
        return dict(target(), head_sha='abc', track_jobs=True, required_jobs=['aggregate'], required_artifacts=['sharded-gate-summary-{attempt}'])

    def artifact(self, **changes):
        return dict(dict(name='sharded-gate-summary-1', expired=False, size_in_bytes=900, workflow_run=dict(id=11, head_sha='abc')), **changes)

    def test_gate_evidence_must_exist_for_this_run_attempt_and_source(self):
        good = self.artifact()
        cases = {'present': ([good], True), 'missing': ([], False), 'other attempt': ([self.artifact(name='sharded-gate-summary-2')], False),
                 'expired': ([self.artifact(expired=True)], False), 'empty': ([self.artifact(size_in_bytes=0)], False),
                 'other run': ([self.artifact(workflow_run=dict(id=99, head_sha='abc'))], False),
                 'other source': ([self.artifact(workflow_run=dict(id=11, head_sha='zzz'))], False),
                 'duplicate': ([good, dict(good)], False)}
        for label, (artifacts, expected) in cases.items():
            with self.subTest(label):
                result, events, paths = self.monitor(self.gate(), [run()], [[job('aggregate')]], lambda _p, a=artifacts: {'artifacts': a})
                self.assertEqual(result, expected, label)
                if not expected:
                    self.assertIn('Gate evidence sharded-gate-summary-1', ''.join(e.get('message', '') for e in events.queue))
                self.assertFalse(any(p.endswith('/rerun') or 'dispatches' in p for p in paths))

    def test_gate_evidence_is_found_on_a_later_page(self):
        filler = [dict(self.artifact(), name=f'other-{n}') for n in range(100)]
        def listing(path):
            return {'artifacts': filler if path.endswith('page=1') else [self.artifact()]}
        result, _, paths = self.monitor(self.gate(), [run()], [[job('aggregate')]], listing)
        self.assertTrue(result)
        self.assertTrue(any(p.endswith('artifacts?per_page=100&page=2') for p in paths))

    def test_gate_evidence_is_not_requested_until_the_jobs_agree(self):
        stale = [job('aggregate', 'in_progress', None)]
        asked = []
        def listing(path):
            asked.append(len(asked))
            return {'artifacts': [self.artifact()]}
        result, _, _ = self.monitor(self.gate(), [run()], [stale, stale, [job('aggregate')]], listing)
        self.assertTrue(result)
        self.assertEqual(len(asked), 1)

    def test_a_timeout_hands_control_back_with_the_run_link(self):
        events = queue.Queue()
        self.assertFalse(release_monitor.monitor_runs('owner/repo', TOKEN, [target()], events, get=lambda _: run(), timeout=0))
        last = list(events.queue)[-1]
        self.assertEqual(last['url'], 'https://github.com/owner/repo/actions/runs/11')
        self.assertIn('not confirmed', last['message'])


class FailureExcerptTests(unittest.TestCase):
    def test_excerpt_shows_more_than_three_matching_lines_cleaned_and_redacted(self):
        text = '\n'.join(['ok line', *[f'##[error]step {n} failed \x1b[31mbadly\x1b[0m' for n in range(1, 9)], 'Error: token offl\x1b[1mine-token leaked', 'x' * 900 + ' Error:'])
        excerpt = release_monitor.excerpt_from_log(text, TOKEN).splitlines()
        self.assertEqual(len(excerpt), 10)
        self.assertEqual(excerpt[0], '##[error]step 1 failed badly')
        self.assertEqual(excerpt[8], 'Error: token [redacted] leaked')
        self.assertTrue(all(len(line) <= 500 for line in excerpt))
        self.assertNotIn('ok line', excerpt)
        self.assertNotIn('\x1b', '\n'.join(excerpt))

    def test_excerpt_is_empty_when_nothing_matches(self):
        self.assertEqual(release_monitor.excerpt_from_log('all good\nstill good', TOKEN), '')


if __name__ == '__main__':
    unittest.main()
