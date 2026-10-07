"""Read-only GitHub workflow monitoring. No dispatch, rerun or cancellation calls."""
from __future__ import annotations
from datetime import datetime
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def github_get(repository: str, path: str, token: str):
    request = Request(f"https://api.github.com/repos/{repository}/{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "InternalUtils-Release-Tool", "X-GitHub-Api-Version": "2026-03-10",
    })
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise RuntimeError(f"Cannot read workflow status (HTTP {exc.code}). Check token access, expiration and API rate limits.") from None
        raise RuntimeError(f"GitHub status request failed (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("Cannot read workflow status. Check network connectivity.") from None


def monitor_runs(repository, token, runs, events, stop=None, get=None, interval=15, timeout=7200, clock=time.monotonic):
    stop = stop or threading.Event()
    get = get or (lambda path: github_get(repository, path, token))
    targets = [dict(run, errors=0, finished=False, last=None, discovery_started=clock()) for run in runs]
    started = clock()
    overall = True
    events.put({"kind": "monitoring", "message": "Monitoring GitHub builds every 15 seconds. You can stop monitoring without cancelling builds."})

    def emit(target, state, message, conclusion=None, url=None):
        events.put({"kind": "status", "workflow": target["workflow"], "run_id": target.get("run_id"),
                    "state": state, "conclusion": conclusion, "message": message,
                    "url": url or target.get("url", "")})

    while any(not target["finished"] for target in targets):
        if stop.is_set():
            events.put({"kind": "log", "message": "Monitoring stopped. GitHub builds continue; final results are not confirmed."})
            return False
        if clock() - started >= timeout:
            for target in targets:
                if not target["finished"]:
                    emit(target, "unknown", f"{target['workflow']}: monitoring timed out; check the run on GitHub.")
            return False
        for target in targets:
            if target["finished"]:
                continue
            if stop.is_set():
                return False
            try:
                if not target.get("run_id"):
                    query = urlencode({"head_sha": target["head_sha"], "per_page": 100})
                    listing = get(f"actions/workflows/{quote(target['workflow'], safe='')}/runs?{query}")
                    candidates = [run for run in listing["workflow_runs"] if
                                  run["head_sha"] == target["head_sha"] and run["head_branch"] == target["tag"] and
                                  run["event"] == "workflow_dispatch" and
                                  datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) >=
                                  datetime.fromisoformat(target["requested_at"].replace("Z", "+00:00"))]
                    if not candidates:
                        if clock() - target["discovery_started"] >= 120:
                            raise RuntimeError("The dispatched run could not be identified. Check GitHub Actions; no final result is confirmed.")
                        if target["last"] != "discovering":
                            emit(target, "queued", f"{target['workflow']}: waiting for its run to appear...")
                            target["last"] = "discovering"
                        continue
                    target["run_id"] = max(candidates, key=lambda run: run["id"])["id"]
                result = get(f"actions/runs/{target['run_id']}")
                if result["head_sha"] != target["head_sha"] or result["head_branch"] != target["tag"]:
                    raise RuntimeError("Run does not match the requested tag and commit. Final result is not confirmed.")
                state, conclusion = result["status"], result.get("conclusion")
                url = f"https://github.com/{repository}/actions/runs/{target['run_id']}"
                target["url"] = url
                marker = (state, conclusion)
                recovered = target["errors"] > 0
                target["errors"] = 0
                if marker != target["last"] or recovered:
                    label = conclusion or "result pending" if state == "completed" else state.replace("_", " ")
                    emit(target, state, f"{target['workflow']}: {label}", conclusion, url)
                    target["last"] = marker
                if state == "completed" and conclusion:
                    target["finished"] = True
                    if conclusion != "success":
                        overall = False
                        events.put({"kind": "error", "message": f"{target['workflow']}: workflow finished with {conclusion}. See its run for details."})
                        try:
                            jobs = get(f"actions/runs/{target['run_id']}/jobs?per_page=100")
                            for job in jobs.get("jobs", []):
                                if job.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required"):
                                    steps = [step["name"] for step in job.get("steps", []) if step.get("conclusion") == "failure"]
                                    suffix = "; failed step: " + ", ".join(steps) if steps else ""
                                    events.put({"kind": "log", "message": f"Failed job: {job['name']}{suffix}"})
                        except Exception:
                            events.put({"kind": "log", "message": "Job details could not be loaded. Open the workflow run for its logs."})
            except Exception as exc:
                target["errors"] += 1
                emit(target, "unknown", f"{target['workflow']}: {exc} ({target['errors']}/3 status-read attempts)")
                if target["errors"] >= 3:
                    target["finished"] = True
                    overall = False
                    events.put({"kind": "error", "message": f"{target['workflow']}: monitoring failed; the GitHub build result is unknown."})
        if any(not target["finished"] for target in targets):
            stop.wait(interval)
    if overall:
        events.put({"kind": "done" if len(targets) == 2 else "log", "message": "Both GitHub workflows completed successfully." if len(targets) == 2 else "The tracked GitHub workflow completed successfully."})
    return overall
