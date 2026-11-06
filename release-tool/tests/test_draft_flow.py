"""Offline contracts for the complete draft flow (VS-21): Windows draft first, then macOS joins that same draft; publication is never part of it."""
import io
import json
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_monitor
import release_ui

SHA = "a" * 40
TAG = "v1.3.4"
REPO = "owner/repo"
URL = f"https://github.com/{REPO}/releases/tag/untagged-1"


def asset(name, size=10):
    return {"name": name, "size": size}


def windows_assets(version="1.3.4"):
    names = [f"App_{version}_x64-setup.exe", f"App_{version}_x64_en-US.msi"]
    return [asset(n + s) for n in names for s in ("", ".manifest.json", ".signature.json")]


def mac_assets(version="1.3.4", archive=True):
    names = [f"App_{version}_universal.dmg", f"App_{version}_universal.dmg.manifest.json", f"App_{version}_universal.dmg.signature.json"]
    return [asset(n) for n in names] + ([asset("App_universal.app.tar.gz")] if archive else [])


def draft(assets, **over):
    return dict({"id": 77, "tag_name": TAG, "draft": True, "prerelease": False, "html_url": URL.replace("untagged-1", TAG), "assets": assets}, **over)


def getter(releases, tag_sha=SHA):
    def get(path):
        if path.startswith("git/ref/tags/"):
            return {"object": {"type": "commit", "sha": tag_sha}}
        assert path.startswith("releases?per_page=100&page="), path
        page = int(path.rsplit("=", 1)[1])
        return releases[(page - 1) * 100:page * 100]
    return get


def state(releases, **kw):
    return release_monitor.draft_state(REPO, TAG, kw.get("sha", SHA), "token", get=getter(releases, kw.get("tag_sha", SHA)))


class DraftStateTests(unittest.TestCase):
    def test_every_stage_of_one_shared_draft_is_told_apart(self):
        cases = [
            ([], "none"),
            ([draft(windows_assets()[:4])], "windows_incomplete"),
            ([draft(windows_assets())], "windows_ready"),
            ([draft(windows_assets() + mac_assets()[:1])], "macos_partial"),
            ([draft(windows_assets() + mac_assets()[:2])], "macos_partial"),
            ([draft(windows_assets() + mac_assets())], "complete"),
            ([draft(windows_assets() + mac_assets(archive=False))], "complete"),
            ([draft(windows_assets(), draft=False)], "published"),
        ]
        for releases, expected in cases:
            with self.subTest(expected=expected, assets=[a["name"] for a in releases[0]["assets"]] if releases else []):
                self.assertEqual(state(releases)["stage"], expected)

    def test_the_release_id_and_url_are_reported_so_later_steps_bind_to_that_release(self):
        result = state([draft(windows_assets())])
        self.assertEqual(result["release_id"], 77)
        self.assertTrue(result["url"].startswith(f"https://github.com/{REPO}/releases/"))
        self.assertTrue(result["next_step"])

    def test_unsafe_or_ambiguous_state_is_invalid_and_never_substituted(self):
        base = windows_assets()
        for label, releases in [
            ("two releases for the tag", [draft(base), draft(base, id=78)]),
            ("a test prerelease", [draft(base, prerelease=True)]),
            ("duplicate asset names", [draft(base + base[:1])]),
            ("macOS files without Windows' signed set", [draft(base[:2] + mac_assets())]),
        ]:
            with self.subTest(label):
                self.assertEqual(state(releases)["stage"], "invalid", label)
        # An empty file may be an upload still in progress: the Windows set is incomplete, not valid and not a different release.
        self.assertEqual(state([draft(base[:-1] + [asset(base[-1]["name"], 0)])])["stage"], "windows_incomplete")
        self.assertEqual(state([draft(base)], tag_sha="b" * 40)["stage"], "invalid", "the tag moved away from the tested commit")
        self.assertEqual(state([draft(base)], sha="not-a-sha")["stage"], "invalid")

    def test_a_dmg_without_its_signature_or_with_empty_bytes_is_partial_never_complete(self):
        for assets in (windows_assets() + mac_assets()[:2], windows_assets() + [asset(mac_assets()[0]["name"], 0)] + mac_assets()[1:],
                       windows_assets() + mac_assets()[1:3], windows_assets() + [asset("App_universal.app.tar.gz")]):
            self.assertEqual(state([draft(assets)])["stage"], "macos_partial")

    def test_vault_spend_file_names_must_match_the_version(self):
        def vault(assets):
            return release_monitor.draft_state("OneTrueAsian/vault-spend", TAG, SHA, "token", get=getter([draft(assets)]))
        names = [f"Vault.Spend_1.3.4_x64-setup.exe", f"Vault.Spend_1.3.4_x64_en-US.msi"]
        good = [asset(n + s) for n in names for s in ("", ".manifest.json", ".signature.json")]
        self.assertEqual(vault(good)["stage"], "windows_ready")
        self.assertEqual(vault(good + [asset(n) for n in ("Vault.Spend_1.3.4_universal.dmg", "Vault.Spend_1.3.4_universal.dmg.manifest.json", "Vault.Spend_1.3.4_universal.dmg.signature.json")])["stage"], "complete")
        self.assertEqual(vault([asset(a["name"].replace("1.3.4", "1.3.3")) for a in good])["stage"], "invalid")
        self.assertEqual(vault(good + [asset("Vault.Spend_1.3.3_universal.dmg")])["stage"], "invalid")

    def test_listing_is_paginated_so_a_draft_on_a_later_page_is_found(self):
        filler = [dict(draft([]), id=1000 + i, tag_name=f"v0.0.{i}", draft=False) for i in range(100)]
        self.assertEqual(state(filler + [draft(windows_assets())])["stage"], "windows_ready")

    def test_complete_draft_verification_binds_to_the_recorded_release_id(self):
        releases = [draft(windows_assets() + mac_assets())]
        verify = lambda release_id, listing=releases: release_monitor.verify_complete_draft(REPO, TAG, SHA, release_id, "token", get=getter(listing))
        self.assertIn("/releases/", verify(77))
        with self.assertRaisesRegex(RuntimeError, "different release"):
            verify(78)
        with self.assertRaisesRegex(RuntimeError, "macOS|complete"):
            verify(77, [draft(windows_assets())])
        with self.assertRaisesRegex(RuntimeError, "published|not a draft"):
            verify(77, [draft(windows_assets() + mac_assets(), draft=False)])


class ExistingStateTests(unittest.TestCase):
    """Resuming: what already exists for the tag is read first, bound to the tag's own commit and to the selected branch."""
    def existing(self, releases, branch_sha=SHA, tag_error=False, ref="release-1.3.4"):
        def get(path):
            if path.startswith("git/ref/tags/"):
                if tag_error:
                    raise RuntimeError("Cannot read workflow status (HTTP 404).")
                return {"object": {"type": "commit", "sha": SHA}}
            if path.startswith("branches/"):
                return {"commit": {"sha": branch_sha}}
            if path == "":
                return {"default_branch": "main"}
            return releases if path.endswith("page=1") else []
        return release_monitor.existing_draft_state(REPO, TAG, ref, "token", get=get)

    def test_nothing_exists_means_a_fresh_start(self):
        self.assertIsNone(self.existing([]))

    def test_an_existing_draft_is_classified_at_the_tag_commit_and_the_branch_is_compared(self):
        found = self.existing([draft(windows_assets())])
        self.assertEqual((found["stage"], found["release_id"], found["source_sha"], found["branch_matches"]), ("windows_ready", 77, SHA, True))
        self.assertFalse(self.existing([draft(windows_assets())], branch_sha="b" * 40)["branch_matches"])
        self.assertEqual(self.existing([draft(windows_assets() + mac_assets())])["stage"], "complete")

    def test_a_blank_branch_means_the_repository_default_branch(self):
        self.assertTrue(self.existing([draft(windows_assets())], ref="")["branch_matches"])

    def test_an_untagged_draft_is_invalid_not_a_crash_and_is_never_used(self):
        found = self.existing([draft(windows_assets())], tag_error=True)
        self.assertEqual(found["stage"], "invalid")
        self.assertFalse(found["branch_matches"])
        self.assertIn("tag", " ".join(found["problems"]).lower())


class StageReportingTests(unittest.TestCase):
    """The monitor reports where a run is, and never reports a result it has not confirmed."""

    def run_monitor(self, jobs_by_poll, results, role="windows", timeout=21600, clock=None, errors=False):
        polls = {"jobs": 0, "run": 0}
        def get(path):
            if "/jobs?" in path:
                index = min(polls["jobs"], len(jobs_by_poll) - 1)
                polls["jobs"] += 1
                return {"jobs": jobs_by_poll[index]}
            if errors:
                raise RuntimeError("GitHub unreadable")
            index = min(polls["run"], len(results) - 1)
            polls["run"] += 1
            return dict(head_sha=SHA, head_branch=TAG, run_attempt=1, **results[index])
        events = queue.Queue()
        target = dict(workflow="x.yml", run_id=11, head_sha=SHA, tag=TAG, role=role, track_jobs=False, show_progress=True)
        kwargs = dict(get=get, interval=0, timeout=timeout, log_get=lambda job_id: "")
        if clock:
            kwargs["clock"] = clock
        ok = release_monitor.monitor_runs(REPO, "token", [target], events, **kwargs)
        return ok, [e for e in list(events.queue)]

    def stages(self, events):
        return [(e["platform"], e["stage"]) for e in events if e["kind"] == "stage"]

    def job(self, status, conclusion=None, name="sharded-sign-updates"):
        return dict(id=1, name=name, status=status, conclusion=conclusion, steps=[])

    def test_windows_building_then_waiting_for_signing_approval_then_building_again(self):
        ok, events = self.run_monitor(
            [[self.job("in_progress", name="build")], [self.job("waiting")], [self.job("waiting")], [self.job("in_progress")], [self.job("completed", "success")]],
            [dict(status="in_progress", conclusion=None)] * 4 + [dict(status="completed", conclusion="success")])
        self.assertTrue(ok)
        self.assertEqual(self.stages(events), [("Windows", "building"), ("Windows", "awaiting_signing_approval"), ("Windows", "building")])
        approval = next(e for e in events if e.get("stage") == "awaiting_signing_approval")
        self.assertIn("release-signing", approval["message"])
        self.assertIn("never approves", approval["message"])

    def test_macos_run_reports_macos_stages_and_its_own_approval_wait(self):
        ok, events = self.run_monitor(
            [[self.job("in_progress", name="build-macos")], [self.job("waiting", name="sign-updates")], [self.job("completed", "success")]],
            [dict(status="in_progress", conclusion=None)] * 2 + [dict(status="completed", conclusion="success")], role="macos")
        self.assertTrue(ok)
        self.assertEqual(self.stages(events), [("macOS", "macos_running"), ("macOS", "awaiting_signing_approval")])

    def test_a_failed_run_reports_failed_and_never_a_ready_stage(self):
        ok, events = self.run_monitor([[self.job("completed", "failure")]], [dict(status="completed", conclusion="failure")])
        self.assertFalse(ok)
        self.assertEqual(self.stages(events)[-1], ("Windows", "failed"))
        self.assertNotIn("ready", str(self.stages(events)))

    def test_a_timeout_is_unconfirmed_not_a_result(self):
        times = iter([0, 0, 0, 10, 10, 10, 10, 10, 10])
        ok, events = self.run_monitor([[self.job("in_progress")]], [dict(status="in_progress", conclusion=None)], timeout=5, clock=lambda: next(times, 10))
        self.assertFalse(ok)
        last = [e for e in events if e["kind"] == "stage"][-1]
        self.assertEqual(last["stage"], "unconfirmed")
        self.assertIn("not confirmed", last["message"])

    def test_unreadable_status_ends_as_unconfirmed(self):
        ok, events = self.run_monitor([[self.job("in_progress")]], [dict(status="in_progress", conclusion=None)], errors=True)
        self.assertFalse(ok)
        self.assertEqual([e for e in events if e["kind"] == "stage"][-1]["stage"], "unconfirmed")

    def test_runs_without_a_role_report_no_stage_events_so_other_modes_are_unchanged(self):
        events = queue.Queue()
        polls = {"n": 0}
        def get(path):
            if "/jobs?" in path:
                return {"jobs": [self.job("waiting" if polls["n"] == 1 else "completed", None if polls["n"] == 1 else "success")]}
            polls["n"] += 1
            done = polls["n"] > 1
            return dict(head_sha=SHA, head_branch=TAG, status="completed" if done else "in_progress", conclusion="success" if done else None)
        target = dict(workflow="x.yml", run_id=11, head_sha=SHA, tag=TAG, show_progress=True)
        release_monitor.monitor_runs(REPO, "token", [target], events, get=get, interval=0)
        self.assertFalse([e for e in list(events.queue) if e["kind"] == "stage"])


if __name__ == "__main__":
    unittest.main()
