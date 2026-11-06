"""Offline contracts for the draft-first, sharded Windows release path."""
import io
import json
from pathlib import Path
import queue
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_monitor
import release_ui


def request(mode="windows_sharded", **changes):
    return dict(repository="owner/repo", ref="release-1.3.3", tag="v1.3.4",
                token="offline-secret", windows_workflow="release-windows.yml",
                mac_workflow="", tag_input="tag", build_mode=mode, **changes)


class DraftReleaseTests(unittest.TestCase):
    def test_ui_draft_mode_disables_macos_and_reports_draft_completion(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = release_ui.ReleaseApp(root)
            app.inputs["build_mode"].set(release_ui.BUILD_MODES[4])
            self.assertTrue(app.entry_by_key["mac_workflow"].instate(["disabled"]))
            self.assertTrue(app.entry_by_key["local_repo"].instate(["disabled"]))
            app.inputs["repository"].set("owner/repo")
            app.inputs["tag"].set("v1.3.4")
            app.inputs["token"].set("offline-secret")
            with patch.object(release_ui.threading, "Thread"):
                app.start()
            self.assertIn("Not requested", app.platform_status["macOS"].get())
            app.events.put({"kind": "finished", "success": True})
            app.poll()
            self.assertIn("not published", app.status.get())
        finally:
            root.destroy()

    def test_sharded_and_serial_modes_validate_without_macos(self):
        for mode in ("windows_sharded", "windows_serial_draft"):
            release_ui.validate_request(request(mode))
        for tag in ("v1.3.90", "v1.3.99", "not-a-version"):
            settings = request()
            settings["tag"] = tag
            with self.assertRaises(ValueError):
                release_ui.validate_request(settings)

    def test_every_mode_refuses_vault_spend_reserved_versions_but_other_apps_keep_them(self):
        for mode in ("both", "windows_recovery", "windows_sharded", "windows_serial_draft"):
            for tag in ("v1.3.90", "v1.3.95", "v1.3.99", "v1.3.095", "v01.3.95"):
                settings = dict(request(mode), repository="OneTrueAsian/vault-spend", tag=tag, mac_workflow="build-macos.yml")
                with self.subTest(mode=mode, tag=tag), self.assertRaisesRegex(ValueError, "reserved test"):
                    release_ui.validate_request(settings)
        for tag in ("v1.3.89", "v1.3.100", "v1.3.3"):
            release_ui.validate_request(dict(request("both"), repository="OneTrueAsian/vault-spend", tag=tag, mac_workflow="build-macos.yml"))
        release_ui.validate_request(dict(request("both"), tag="v1.3.95", mac_workflow="build-macos.yml"))

    def test_worker_pins_draft_mode_and_checks_assets_after_monitoring(self):
        class Input(io.StringIO):
            def close(self):
                self.sent = self.getvalue()
                super().close()
        class Process:
            stdin = Input()
            stdout = io.StringIO('{"kind":"run","workflow":"release-windows.yml","run_id":11,"source_sha":"' + "a" * 40 + '"}\n')
            def wait(self): return 0
            def poll(self): return 0
        events = queue.Queue()
        process = Process()
        with patch.object(release_ui, "powershell_command", return_value=["powershell"]), \
             patch.object(release_ui, "monitor_runs", return_value=True) as monitor, \
             patch.object(release_ui, "verify_windows_draft", return_value="https://github.com/owner/repo/releases/1") as verify:
            release_ui.run_release(request(), events, lambda *a, **kw: process)
        self.assertEqual(json.loads(process.stdin.sent)["build_mode"], "windows_sharded")
        self.assertTrue(monitor.call_args.args[2][0]["track_jobs"])
        verify.assert_called_once_with("owner/repo", "v1.3.4", "a" * 40, "offline-secret")
        self.assertTrue(list(events.queue)[-1]["success"])
        self.assertIn("draft", str(list(events.queue)).lower())
        self.assertNotIn("offline-secret", str(list(events.queue)))

    def test_every_remote_mode_asks_the_monitor_for_step_progress_including_macos(self):
        class Process:
            def __init__(self, workflows):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("".join('{"kind":"run","workflow":"%s","run_id":%d,"source_sha":"%s"}\n' % (name, 11 + n, "a" * 40) for n, name in enumerate(workflows)))
            def wait(self): return 0
            def poll(self): return 0
        for mode, workflows in (("both", ["release-windows.yml", "build-macos.yml"]), ("windows_recovery", ["release-windows.yml"]),
                                ("windows_sharded", ["release-windows.yml"]), ("windows_serial_draft", ["release-windows.yml"])):
            settings = dict(request(mode), mac_workflow="build-macos.yml" if mode == "both" else "")
            with self.subTest(mode=mode), patch.object(release_ui, "powershell_command", return_value=["powershell"]), \
                 patch.object(release_ui, "monitor_runs", return_value=True) as monitor, \
                 patch.object(release_ui, "verify_windows_draft", return_value="https://github.com/owner/repo/releases/1"):
                release_ui.run_release(settings, queue.Queue(), lambda *a, **kw: Process(workflows))
                monitored = monitor.call_args.args[2]
                self.assertEqual([run["workflow"] for run in monitored], workflows)
                self.assertTrue(all(run["show_progress"] for run in monitored))
        self.assertTrue(monitored[0]["track_jobs"], "gate runs still reconcile job results")
        self.assertNotIn("required_artifacts", monitored[0], "the serial fallback has no gate summary")

    def test_sharded_draft_requires_the_gate_summary_evidence_for_the_run_attempt(self):
        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO('{"kind":"run","workflow":"release-windows.yml","run_id":11,"source_sha":"' + "a" * 40 + '"}\n')
            def wait(self): return 0
            def poll(self): return 0
        with patch.object(release_ui, "powershell_command", return_value=["powershell"]), \
             patch.object(release_ui, "monitor_runs", return_value=True) as monitor, \
             patch.object(release_ui, "verify_windows_draft", return_value="https://github.com/owner/repo/releases/1"):
            release_ui.run_release(request("windows_sharded"), queue.Queue(), lambda *a, **kw: Process())
        self.assertEqual(monitor.call_args.args[2][0]["required_artifacts"], ["sharded-gate-summary-{attempt}"])

    def test_failed_gate_never_checks_or_claims_a_ready_draft(self):
        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO('{"kind":"run","workflow":"release-windows.yml","run_id":11}\n')
            def wait(self): return 0
            def poll(self): return 0
        with patch.object(release_ui, "powershell_command", return_value=["powershell"]), \
             patch.object(release_ui, "monitor_runs", return_value=False), \
             patch.object(release_ui, "verify_windows_draft") as verify:
            events = queue.Queue()
            release_ui.run_release(request(), events, lambda *a, **kw: Process())
        verify.assert_not_called()
        self.assertFalse(list(events.queue)[-1]["success"])

    def test_draft_requires_exact_tag_nonempty_installers_and_companions(self):
        assets = [{"name": "App_1.3.4" + suffix, "size": 10}
                  for suffix in (".exe", ".exe.manifest.json", ".exe.signature.json",
                                 ".msi", ".msi.manifest.json", ".msi.signature.json")]
        release = dict(tag_name="v1.3.4", draft=True, prerelease=False, assets=assets,
                       html_url="https://github.com/owner/repo/releases/1")
        def get(path):
            if path.startswith("git/ref/"):
                return {"object": {"type": "commit", "sha": "a" * 40}}
            return [release]
        self.assertIn("/releases/1", release_monitor.verify_windows_draft("owner/repo", "v1.3.4", "a" * 40, "token", get=get))
        for field, value in (("draft", False), ("prerelease", True), ("assets", assets[:-1])):
            original = release[field]
            release[field] = value
            with self.assertRaises(RuntimeError):
                release_monitor.verify_windows_draft("owner/repo", "v1.3.4", "a" * 40, "token", get=get)
            release[field] = original
        assets[0]["size"] = 0
        with self.assertRaises(RuntimeError):
            release_monitor.verify_windows_draft("owner/repo", "v1.3.4", "a" * 40, "token", get=get)

    def test_wrong_source_is_refused_before_reading_releases(self):
        def get(path):
            self.assertTrue(path.startswith("git/ref/"))
            return {"object": {"type": "commit", "sha": "b" * 40}}
        with self.assertRaisesRegex(RuntimeError, "source commit"):
            release_monitor.verify_windows_draft("owner/repo", "v1.3.4", "a" * 40, "token", get=get)

    def test_job_pages_are_all_checked_and_progress_is_not_repeated(self):
        jobs = [dict(id=i, name=f"shard {i}", status="completed", conclusion="success", steps=[])
                for i in range(101)]
        pages = []
        def get(path):
            pages.append(path)
            return {"jobs": jobs[:100] if path.endswith("&page=1") else jobs[100:]}
        self.assertEqual(len(release_monitor.workflow_jobs(get, 11, 2)), 101)
        self.assertEqual(len(pages), 2)
        self.assertIn("/attempts/2/", pages[0])
        events = queue.Queue()
        target = dict(workflow="release-windows.yml")
        release_monitor.log_job_progress(target, jobs, events, "offline-secret")
        release_monitor.log_job_progress(target, jobs, events, "offline-secret")
        self.assertEqual(events.qsize(), 101)

    def test_current_approval_and_step_progress_are_visible_and_redacted(self):
        events = queue.Queue()
        job = dict(id=10, name="signing offline-secret", status="waiting", conclusion=None,
                   steps=[dict(number=1, name="sign", status="completed", conclusion="success",
                               started_at="2026-11-05T10:00:00Z", completed_at="2026-11-05T10:01:02Z")])
        release_monitor.log_job_progress(dict(workflow="release-windows.yml"), [job], events, "offline-secret")
        messages = str(list(events.queue))
        self.assertIn("waiting", messages)
        self.assertIn("1m 2s", messages)
        self.assertNotIn("offline-secret", messages)

    def test_monitor_waits_for_active_jobs_even_if_run_reports_success(self):
        calls = 0
        def get(path):
            nonlocal calls
            if "/jobs?" in path:
                calls += 1
                return {"jobs": [dict(id=1, name="build", status="in_progress" if calls == 1 else "completed", conclusion=None if calls == 1 else "success", steps=[])]}
            return dict(head_sha="abc", head_branch="v1.3.4", status="completed", conclusion="success")
        events = queue.Queue()
        target = dict(workflow="release-windows.yml", run_id=11, head_sha="abc", tag="v1.3.4", track_jobs=True)
        self.assertTrue(release_monitor.monitor_runs("owner/repo", "token", [target], events, get=get, interval=0))
        self.assertEqual(calls, 2)

    def test_completed_sharded_run_missing_required_job_is_not_success(self):
        def get(path):
            if "/jobs?" in path:
                return {"jobs": [dict(id=1, name="sharded-gate / build", status="completed", conclusion="success", steps=[])]}
            return dict(head_sha="abc", head_branch="v1.3.4", status="completed", conclusion="success")
        events = queue.Queue()
        target = dict(workflow="release-windows.yml", run_id=11, head_sha="abc", tag="v1.3.4", track_jobs=True, required_jobs=["sharded-gate / aggregate"])
        self.assertFalse(release_monitor.monitor_runs("owner/repo", "token", [target], events, get=get, interval=0))
        self.assertIn("aggregate", str(list(events.queue)))


if __name__ == "__main__":
    unittest.main()
