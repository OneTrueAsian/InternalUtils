"""Offline contracts for the two-phase remote flow (VS-21): Windows gate, packaging and signing first; only then macOS joins that same draft."""
import io
import json
from pathlib import Path
import queue
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_monitor
import release_ui
from test_draft_flow import SHA, TAG, REPO

WINDOWS_RUN = {"kind": "run", "workflow": "release-windows.yml", "run_id": 11, "source_sha": SHA}
MAC_RUN = {"kind": "run", "workflow": "build-macos.yml", "run_id": 22, "source_sha": SHA}


def request(mode="draft_sharded_macos", **changes):
    return dict(dict(repository=REPO, ref="release-1.3.4", tag=TAG, token="offline-secret", windows_workflow="release-windows.yml",
                     mac_workflow="build-macos.yml", tag_input="tag", build_mode=mode), **changes)


def ds(stage, release_id=77, **extra):
    return dict({"stage": stage, "release_id": release_id, "url": f"https://github.com/{REPO}/releases/tag/{TAG}", "problems": [], "next_step": f"next step for {stage}"}, **extra)


class FakeShell:
    """Stands in for PowerShell: each launch plays back one scripted run and records the request it was sent."""
    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.sent = []
        self.launched = 0

    def __call__(self, *args, **kwargs):
        self.launched += 1
        lines, code = self.scripts.pop(0)
        shell = self
        class Input(io.StringIO):
            def close(self):
                shell.sent.append(json.loads(self.getvalue()))
                super().close()
        class Process:
            stdin = Input()
            stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
            def wait(self): return code
            def poll(self): return code
        return Process()


class Harness:
    def __init__(self, shell, monitor=(True, True), states=(), existing=None, complete=None):
        self.shell = shell
        self.monitor_results = list(monitor)
        self.states = list(states)
        self.existing = existing
        self.complete = complete
        self.monitored = []
        self.verified = []

    def monitor_runs(self, repository, token, runs, events, stop=None, **kw):
        self.monitored.append([dict(run) for run in runs])
        windows = runs[0].get("role") == "windows"
        events.put({"kind": "stage", "stage": "building" if windows else "macos_running", "platform": "Windows" if windows else "macOS", "message": "running"})
        return self.monitor_results.pop(0)

    def draft_state(self, *args, **kw):
        return self.states.pop(0)

    def verify_complete(self, repository, tag, source_sha, release_id, token, **kw):
        self.verified.append((repository, tag, source_sha, release_id))
        if isinstance(self.complete, Exception):
            raise self.complete
        return self.complete or f"https://github.com/{repository}/releases/tag/{tag}"

    def run(self, settings=None):
        events = queue.Queue()
        with patch.object(release_ui, "powershell_command", return_value=["powershell"]), \
             patch.object(release_ui, "monitor_runs", self.monitor_runs), \
             patch.object(release_ui, "draft_state", self.draft_state), \
             patch.object(release_ui, "existing_draft_state", lambda *a, **k: self.existing), \
             patch.object(release_ui, "verify_complete_draft", self.verify_complete):
            release_ui.run_release(settings or request(), events, self.shell)
        return list(events.queue)


def stages(events):
    return [e["stage"] for e in events if e.get("kind") == "stage"]


def last_finished(events):
    return [e for e in events if e["kind"] == "finished"][-1]["success"]


class CompleteDraftFlowTests(unittest.TestCase):
    def full_success(self, mode="draft_sharded_macos"):
        shell = FakeShell(([WINDOWS_RUN], 0), ([MAC_RUN], 0))
        harness = Harness(shell, states=[ds("windows_ready")])
        return harness, harness.run(request(mode))

    def test_windows_finishes_and_is_verified_before_macos_is_dispatched_for_that_exact_release(self):
        harness, events = self.full_success()
        self.assertTrue(last_finished(events))
        windows_request, mac_request = harness.shell.sent
        self.assertEqual(windows_request["build_mode"], "windows_sharded")
        self.assertTrue(windows_request["include_macos"])
        self.assertEqual(mac_request["build_mode"], "macos_draft")
        self.assertEqual((mac_request["tag"], mac_request["source_sha"], mac_request["release_id"], mac_request["mac_workflow"]), (TAG, SHA, 77, "build-macos.yml"))
        self.assertEqual(harness.verified, [(REPO, TAG, SHA, 77)])
        windows_run, mac_run = harness.monitored[0][0], harness.monitored[1][0]
        self.assertEqual((windows_run["role"], windows_run["track_jobs"]), ("windows", True))
        self.assertEqual((mac_run["role"], mac_run["track_jobs"], mac_run["required_jobs"]), ("macos", True, ["build-macos", "sign-updates"]))
        self.assertEqual(mac_run["source_sha"], SHA)

    def test_stages_are_reported_in_order_and_never_claim_publication(self):
        harness, events = self.full_success()
        self.assertEqual(stages(events), ["building", "windows_draft_ready", "macos_running", "complete_draft_ready"])
        text = " ".join(str(e.get("message", "")) for e in events).lower()
        self.assertIn("not published", text)
        self.assertNotIn("publication complete", text)
        for event in events:
            if event["kind"] == "stage" and event["stage"] in ("windows_draft_ready", "complete_draft_ready"):
                self.assertIn("not published", event["message"])

    def test_serial_fallback_keeps_its_own_required_jobs_and_no_gate_summary(self):
        harness, events = self.full_success("draft_serial_macos")
        self.assertEqual(harness.shell.sent[0]["build_mode"], "windows_serial_draft")
        self.assertEqual(harness.monitored[0][0]["required_jobs"], ["release", "sign-updates"])
        self.assertNotIn("required_artifacts", harness.monitored[0][0])

    def test_sharded_windows_still_requires_the_gate_summary_evidence(self):
        harness, events = self.full_success()
        self.assertEqual(harness.monitored[0][0]["required_artifacts"], ["sharded-gate-summary-{attempt}"])

    def test_windows_failure_blocks_macos_completely(self):
        # Everything macOS would need is available, so only the Windows failure itself can be what stops it.
        shell = FakeShell(([WINDOWS_RUN], 0), ([MAC_RUN], 0))
        harness = Harness(shell, monitor=[False, True], states=[ds("windows_ready")])
        events = harness.run()
        self.assertFalse(last_finished(events))
        self.assertEqual(shell.launched, 1, "macOS was not even requested: PowerShell was launched once, for Windows")
        self.assertEqual(len(harness.monitored), 1)
        self.assertNotIn("windows_draft_ready", stages(events))
        self.assertNotIn("complete_draft_ready", stages(events))

    def test_a_partial_windows_draft_after_a_green_run_stops_before_macos(self):
        for stage in ("windows_incomplete", "invalid", "published", "none", "complete", "macos_partial"):
            shell = FakeShell(([WINDOWS_RUN], 0))
            harness = Harness(shell, states=[ds(stage)])
            events = harness.run()
            with self.subTest(stage=stage):
                self.assertFalse(last_finished(events))
                self.assertEqual(shell.launched, 1)
                self.assertIn(f"next step for {stage}", str(events))
                self.assertNotIn("windows_draft_ready", stages(events))

    def test_macos_dispatch_refusal_is_a_failure_and_nothing_is_monitored(self):
        shell = FakeShell(([WINDOWS_RUN], 0), ([{"kind": "error", "message": "build-macos.yml: draft already holds macOS files"}], 1))
        harness = Harness(shell, states=[ds("windows_ready")])
        events = harness.run()
        self.assertFalse(last_finished(events))
        self.assertEqual(len(harness.monitored), 1, "only the Windows run was ever monitored")
        self.assertEqual(harness.verified, [])
        self.assertIn("windows_draft_ready", stages(events))
        self.assertNotIn("complete_draft_ready", stages(events))

    def test_a_failed_or_unconfirmed_macos_run_never_reports_a_complete_draft(self):
        shell = FakeShell(([WINDOWS_RUN], 0), ([MAC_RUN], 0))
        harness = Harness(shell, monitor=[True, False], states=[ds("windows_ready")])
        events = harness.run()
        self.assertFalse(last_finished(events))
        self.assertEqual(harness.verified, [])
        self.assertNotIn("complete_draft_ready", stages(events))
        self.assertIn("Windows' draft is still intact", str(events))

    def test_a_green_macos_run_whose_draft_does_not_verify_is_not_success(self):
        for failure in (RuntimeError("The draft is not complete: macOS's signed files are not all there."),
                        RuntimeError("The release for v1.3.4 is a different release (id 99) from the one recorded earlier (id 77); not substituting it.")):
            shell = FakeShell(([WINDOWS_RUN], 0), ([MAC_RUN], 0))
            harness = Harness(shell, states=[ds("windows_ready")], complete=failure)
            events = harness.run()
            with self.subTest(str(failure)[:30]):
                self.assertFalse(last_finished(events))
                self.assertIn(str(failure), str(events))
                self.assertNotIn("complete_draft_ready", stages(events))

    def test_the_token_is_never_logged_and_the_tool_has_no_approval_or_publish_calls(self):
        harness, events = self.full_success()
        self.assertNotIn("offline-secret", str(events))
        source = Path(release_ui.__file__).read_text(encoding="utf-8") + Path(release_monitor.__file__).read_text(encoding="utf-8")
        for forbidden in ("pending_deployments", "gh release edit", "--draft=false", '"draft": False', "'draft': False"):
            self.assertNotIn(forbidden, source)


class ResumeAndRecoveryTests(unittest.TestCase):
    """Recovery reconciles what already exists; it never dispatches Windows twice, never starts completed macOS packaging again and never overwrites."""

    def existing(self, stage, **extra):
        return dict(ds(stage, **extra), source_sha=SHA, branch_matches=True)

    def test_a_complete_windows_draft_resumes_at_macos_without_another_windows_run(self):
        shell = FakeShell(([MAC_RUN], 0))
        harness = Harness(shell, monitor=[True], existing=self.existing("windows_ready"))
        events = harness.run()
        self.assertTrue(last_finished(events))
        self.assertEqual([sent["build_mode"] for sent in shell.sent], ["macos_draft"])
        self.assertEqual(shell.sent[0]["release_id"], 77)
        self.assertIn("already complete", str(events))

    def test_a_complete_draft_is_only_verified_nothing_is_dispatched(self):
        shell = FakeShell()
        harness = Harness(shell, existing=self.existing("complete"))
        events = harness.run()
        self.assertTrue(last_finished(events))
        self.assertEqual((shell.sent, shell.launched), ([], 0))
        self.assertEqual(harness.monitored, [])
        self.assertEqual(harness.verified, [(REPO, TAG, SHA, 77)])
        self.assertEqual(stages(events), ["complete_draft_ready"])

    def test_partial_published_invalid_or_mismatched_state_is_reported_with_the_safe_next_step_and_changes_nothing(self):
        cases = [self.existing("macos_partial"), self.existing("windows_incomplete"), self.existing("published"), self.existing("invalid"),
                 dict(self.existing("windows_ready"), branch_matches=False)]
        for existing in cases:
            shell = FakeShell()
            harness = Harness(shell, existing=existing)
            events = harness.run()
            with self.subTest(stage=existing["stage"], branch=existing["branch_matches"]):
                self.assertFalse(last_finished(events))
                self.assertEqual((shell.sent, shell.launched), ([], 0), "no PowerShell launch, so no dispatch of any kind")
                self.assertEqual(harness.monitored, [])
                self.assertNotIn("complete_draft_ready", stages(events))
                self.assertTrue(any(e["kind"] == "error" for e in events))
        events = Harness(FakeShell(), existing=self.existing("macos_partial")).run()
        self.assertIn("next step for macos_partial", str(events))

    def test_macos_assets_that_appeared_while_waiting_end_in_verification_not_a_second_dispatch(self):
        shell = FakeShell(([WINDOWS_RUN], 0), ([], 0))
        harness = Harness(shell, states=[ds("windows_ready"), ds("complete")])
        events = harness.run()
        self.assertTrue(last_finished(events))
        self.assertEqual(harness.verified, [(REPO, TAG, SHA, 77)])
        self.assertEqual(len(harness.monitored), 1)

    def test_macos_step_that_finds_nothing_to_track_and_no_complete_draft_fails(self):
        shell = FakeShell(([WINDOWS_RUN], 0), ([], 0))
        harness = Harness(shell, states=[ds("windows_ready"), ds("windows_ready")])
        events = harness.run()
        self.assertFalse(last_finished(events))
        self.assertEqual(harness.verified, [])


class RequestValidationAndUiTests(unittest.TestCase):
    def test_new_modes_validate_like_the_draft_modes_plus_a_distinct_macos_workflow(self):
        for mode in ("draft_sharded_macos", "draft_serial_macos"):
            release_ui.validate_request(request(mode))
            for changes in (dict(tag="v1.3.90"), dict(tag="not-a-version"), dict(tag_input="other"), dict(mac_workflow=""), dict(mac_workflow="release-windows.yml")):
                with self.subTest(mode=mode, **changes), self.assertRaises(ValueError):
                    release_ui.validate_request(request(mode, **changes))
            for tag in ("v1.3.90", "v1.3.95", "v1.3.99", "v1.3.095", "v01.3.95"):
                with self.subTest(mode=mode, tag=tag), self.assertRaisesRegex(ValueError, "reserved test|production"):
                    release_ui.validate_request(request(mode, repository="OneTrueAsian/vault-spend", tag=tag))

    def test_existing_modes_are_unchanged_by_the_new_ones(self):
        self.assertEqual(release_ui.BUILD_MODES[:6], ("Windows + macOS", "Windows only · existing tag", "Local: full release", "Local: Windows existing tag", "Windows: sharded draft", "Windows: serial draft"))
        self.assertTrue(release_ui.windows_only(request("windows_sharded")))
        self.assertFalse(release_ui.windows_only(request("draft_sharded_macos")))
        self.assertEqual(release_ui.draft_mode(request("draft_serial_macos")), "windows_serial_draft")
        self.assertFalse(release_ui.macos_follows(request("windows_sharded")))
        self.assertTrue(release_ui.macos_follows(request(release_ui.BUILD_MODES[6])))

    def start_app(self, mode_index):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = release_ui.ReleaseApp(root)
        app.inputs["build_mode"].set(release_ui.BUILD_MODES[mode_index])
        app.inputs["repository"].set(REPO)
        app.inputs["tag"].set(TAG)
        app.inputs["token"].set("offline-secret")
        with patch.object(release_ui.threading, "Thread"):
            app.start()
        return app

    def test_ui_shows_each_stage_and_only_a_verified_complete_draft_as_ready(self):
        app = self.start_app(6)
        self.assertNotIn("Not requested", app.platform_status["macOS"].get())
        for stage, platform, expected in [("building", "Windows", "Building"), ("awaiting_signing_approval", "Windows", "Waiting for signing approval"),
                                          ("windows_draft_ready", "Windows", "Draft ready"), ("macos_running", "macOS", "Running"),
                                          ("awaiting_signing_approval", "macOS", "Waiting for signing approval"), ("complete_draft_ready", "macOS", "Draft ready")]:
            app.events.put({"kind": "stage", "stage": stage, "platform": platform, "message": stage})
            app.poll()
            self.assertIn(expected, app.platform_status[platform].get(), (stage, platform))
        self.assertNotIn("ready for review", app.status.get(), "no ready claim before the finish event")
        app.events.put({"kind": "finished", "success": True})
        app.poll()
        self.assertEqual(app.status.get(), "Complete draft ready for review · not published")

    def test_local_draft_mode_is_a_local_mode_with_notes_and_no_macos_fields_and_never_says_published(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = release_ui.ReleaseApp(root)
        app.inputs["build_mode"].set(release_ui.BUILD_MODES[8])
        self.assertTrue(release_ui.local_mode(request(release_ui.BUILD_MODES[8])))
        self.assertEqual(release_ui.normalize_local(request("local_draft"))["build_mode"], "local_draft")
        self.assertEqual(release_ui.normalize_local(request(release_ui.BUILD_MODES[3]))["build_mode"], "local_windows")
        self.assertEqual(release_ui.normalize_local(request(release_ui.BUILD_MODES[2]))["build_mode"], "local_release")
        self.assertTrue(app.entry_by_key["mac_workflow"].instate(["disabled"]))
        self.assertTrue(app.entry_by_key["windows_workflow"].instate(["disabled"]))
        self.assertFalse(app.entry_by_key["notes_file"].instate(["disabled"]))
        self.assertIn("unpublished draft", app.footer.get())
        self.assertIn("nothing is published", app.footer.get())
        self.assertEqual(release_ui.BUILD_MODES[8], "Local: Windows-only unsigned draft")
        self.assertIn("Windows-only unsigned draft", app.footer.get())

    def test_local_draft_finish_status_says_windows_only_unsigned_not_complete(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = release_ui.ReleaseApp(root)
        app.local_kind = "local_draft"
        app.events.put({"kind": "finished", "success": True})
        app.poll()
        self.assertEqual(app.status.get(), "Windows-only unsigned draft ready · not published")

    def test_run_release_sends_the_local_draft_mode_to_the_local_pipeline_and_never_to_powershell(self):
        seen = {}
        def fake_local(settings, events, stop=None):
            seen["mode"] = settings["build_mode"]
        with patch.object(release_ui, "run_local_release", fake_local), patch.object(release_ui, "powershell_command", side_effect=AssertionError("no PowerShell for a local mode")):
            release_ui.run_release(request(release_ui.BUILD_MODES[8]), queue.Queue(), lambda *a, **k: None)
        self.assertEqual(seen["mode"], "local_draft")

    def test_ui_failure_and_unconfirmed_never_say_ready(self):
        for stage, text in (("failed", "Failed"), ("unconfirmed", "Not confirmed")):
            with self.subTest(stage):
                app = self.start_app(7)
                app.events.put({"kind": "stage", "stage": stage, "platform": "macOS", "message": stage})
                app.events.put({"kind": "finished", "success": False})
                app.poll()
                self.assertNotIn("ready for review", app.status.get())
                self.assertIn(text, app.platform_status["macOS"].get())
                self.assertNotIn("Draft ready", app.platform_status["macOS"].get())


if __name__ == "__main__":
    unittest.main()
