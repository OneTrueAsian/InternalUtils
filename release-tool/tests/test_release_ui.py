import io
import json
from pathlib import Path
import queue
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ui


def request(**changes):
    return dict(repository="owner/repo", ref="main", tag="v1.2.3", token="offline-secret",
                windows_workflow="release-windows.yml", mac_workflow="build-macos.yml", tag_input="tag", **changes)


class FakeProcess:
    def __init__(self, output, code=0):
        class Input(io.StringIO):
            def close(self):
                self.sent = self.getvalue()
                super().close()
        self.stdin = Input()
        self.stdout = io.StringIO(output)
        self.code = code

    def wait(self):
        return self.code

    def poll(self):
        return self.code


class ReleaseTests(unittest.TestCase):
    def test_credentials_only_travel_over_stdin_and_logs_are_redacted(self):
        process = FakeProcess('{"kind":"log","message":"offline-secret"}\n{"kind":"done","message":"requested"}\n')
        settings = request()
        events = queue.Queue()
        with patch.object(release_ui, "powershell_command", return_value=["powershell", "-InputJson"]):
            captured = []
            def launch(command, **kwargs):
                captured.append((command, kwargs))
                return process
            release_ui.run_release(settings, events, launch)
        self.assertNotIn("offline-secret", str(captured))
        self.assertEqual(json.loads(process.stdin.sent)["token"], "offline-secret")
        self.assertEqual(settings["token"], "")
        messages = list(events.queue)
        self.assertNotIn("offline-secret", str(messages))
        self.assertEqual(messages[-1], {"kind": "finished", "success": True})

    def test_partial_workflow_failure_is_not_reported_as_success(self):
        process = FakeProcess('{"kind":"error","message":"macOS dispatch failed; tag retained"}\n', code=1)
        events = queue.Queue()
        with patch.object(release_ui, "powershell_command", return_value=["powershell"]):
            release_ui.run_release(request(), events, lambda *args, **kwargs: process)
        self.assertFalse(list(events.queue)[-1]["success"])

    def test_ref_and_tag_are_data_not_shell_arguments(self):
        settings = request()
        settings["ref"] = 'branch;$(anything)'
        release_ui.validate_request(settings)
        for tag in ["", "bad tag", "bad..tag", "-bad", "bad.lock", "bad/.hidden", "bad\\tag", "bad@{tag"]:
            with self.subTest(tag=tag):
                settings["tag"] = tag
                with self.assertRaises(ValueError):
                    release_ui.validate_request(settings)

    def test_workflow_traversal_and_duplicate_files_are_rejected(self):
        for filename in ["../build.yml", "a.yml;cmd", "", "https://example.com/a.yml"]:
            settings = request()
            settings["windows_workflow"] = filename
            with self.assertRaises(ValueError):
                release_ui.validate_request(settings)
        settings = request()
        settings["windows_workflow"] = settings["mac_workflow"]
        with self.assertRaises(ValueError):
            release_ui.validate_request(settings)

    def test_ui_clears_token_and_reenables_after_request(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = release_ui.ReleaseApp(root)
            app.inputs["repository"].set("owner/repo")
            app.inputs["tag"].set("v1.2.3")
            app.inputs["token"].set("offline-secret")
            with patch.object(release_ui, "powershell_command", return_value=["powershell"]), patch.object(release_ui.threading, "Thread"):
                app.start()
            self.assertEqual(app.inputs["token"].get(), "")
            self.assertTrue(app.running)
            self.assertTrue(app.start_button.instate(["disabled"]))
            app.events.put({"kind": "finished", "success": True})
            app.poll()
            self.assertFalse(app.running)
            self.assertFalse(app.start_button.instate(["disabled"]))
            self.assertIn("requested", app.status.get())
        finally:
            root.destroy()

    def test_form_controls_fit_at_minimum_window_size(self):
        root = tk.Tk()
        root.attributes("-alpha", 0)
        try:
            app = release_ui.ReleaseApp(root)
            root.geometry("860x760")
            root.update()
            for control in [*app.entries, app.start_button, app.actions_button, app.release_button]:
                bottom = control.winfo_rooty() - root.winfo_rooty() + control.winfo_height()
                self.assertLessEqual(bottom, root.winfo_height(), str(control))
        finally:
            root.destroy()

    def test_release_summary_and_settings_tab(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = release_ui.ReleaseApp(root)
            self.assertEqual(app.inputs["repository"].get(), "")
            app.inputs["repository"].set("owner/another-app")
            app.inputs["tag"].set("v2.0.0")
            self.assertEqual(app.summary["repository"].get(), "owner/another-app")
            self.assertEqual(app.summary["ref"].get(), "Repository default branch")
            self.assertEqual(app.summary["tag"].get(), "v2.0.0")
            self.assertEqual(app.tabs.index(app.tabs.select()), 0)
            app.tabs.select(1)
            self.assertEqual(app.tabs.index(app.tabs.select()), 1)
            self.assertEqual(set(app.help_buttons), set(app.inputs))
        finally:
            root.destroy()

    def test_partial_failure_keeps_platform_results_and_redacted_log(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = release_ui.ReleaseApp(root)
            app.inputs["repository"].set("owner/repo")
            app.inputs["tag"].set("v1.2.3")
            app.inputs["token"].set("offline-secret")
            app.show_token.set(True)
            app.toggle_token()
            with patch.object(release_ui, "powershell_command", return_value=["powershell"]), patch.object(release_ui.threading, "Thread"):
                app.start()
            self.assertFalse(app.show_token.get())
            self.assertEqual(app.entry_by_key["token"].cget("show"), "•")
            app.events.put({"kind": "link", "message": "release-windows.yml: build requested", "url": "https://github.com/owner/repo/actions/runs/1"})
            app.events.put({"kind": "error", "message": "build-macos.yml: access denied. Tag retained."})
            app.events.put({"kind": "finished", "success": False})
            app.poll()
            self.assertEqual(app.platform_status["Windows"].get(), "Windows  ·  Build requested")
            self.assertEqual(app.platform_status["macOS"].get(), "macOS  ·  Request failed")
            self.assertIn("failed", app.status.get())
            app.copy_log()
            self.assertIn("Tag retained", root.clipboard_get())
            self.assertNotIn("offline-secret", root.clipboard_get())
            app.inputs["repository"].set("owner/new-app")
            self.assertIn("Not requested", app.platform_status["Windows"].get())
            self.assertTrue(app.actions_button.instate(["disabled"]))
        finally:
            root.destroy()

    def test_invalid_form_reports_inline_without_starting_worker(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = release_ui.ReleaseApp(root)
            with patch.object(release_ui.threading, "Thread") as worker:
                app.start()
            worker.assert_not_called()
            self.assertFalse(app.running)
            self.assertIn("Check release details", app.status.get())
            self.assertIn("Enter your GitHub API token", app.log.get("1.0", "end"))
        finally:
            root.destroy()



if __name__ == "__main__":
    unittest.main()
