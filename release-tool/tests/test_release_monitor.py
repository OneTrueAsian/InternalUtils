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


if __name__ == '__main__':
    unittest.main()
