"""Read-only GitHub workflow monitoring. No dispatch, rerun or cancellation calls."""
from __future__ import annotations
from datetime import datetime, timedelta
import re
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler


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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_windows_draft(repository, tag, source_sha, token, get=None):
    """Check tag identity and draft asset presence; signing remains the workflow's gate."""
    get = get or (lambda path: github_get(repository, path, token))
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha or ""):
        raise RuntimeError("No exact source commit was recorded for the draft.")
    obj = get("git/ref/tags/" + quote(tag, safe=""))["object"]
    for _ in range(8):
        if obj.get("type") == "commit":
            break
        if obj.get("type") != "tag":
            raise RuntimeError("The release tag does not resolve to a commit.")
        obj = get("git/tags/" + quote(obj["sha"], safe=""))["object"]
    if obj.get("type") != "commit" or obj.get("sha") != source_sha:
        raise RuntimeError("The draft tag no longer matches the tested source commit.")
    # List with authentication: drafts may not be returned by the public tag lookup.
    matches = []
    for page in range(1, 101):
        releases = get(f"releases?per_page=100&page={page}")
        matches.extend(item for item in releases if item.get("tag_name") == tag)
        if len(releases) < 100:
            break
    else:
        raise RuntimeError("Release listing exceeded the verification limit; inspect the draft manually.")
    if len(matches) != 1 or matches[0].get("draft") is not True or matches[0].get("prerelease") is not False:
        raise RuntimeError("Expected one unpublished production draft, not a published release or test prerelease.")
    release = matches[0]
    assets = release.get("assets", [])
    names = [item.get("name") for item in assets]
    if len(names) != len(set(names)):
        raise RuntimeError("Draft has duplicate asset names.")
    installers = [item for item in assets if str(item.get("name", "")).lower().endswith((".exe", ".msi"))]
    if not all(any(item["name"].lower().endswith(ext) for item in installers) for ext in (".exe", ".msi")):
        raise RuntimeError("Draft is missing its Windows EXE or MSI installer.")
    by_name = {item["name"]: item for item in assets}
    if repository.lower() == "onetrueasian/vault-spend":
        version = tag.removeprefix("v")
        expected = {f"Vault.Spend_{version}_x64-setup.exe", f"Vault.Spend_{version}_x64_en-US.msi"}
        if {item["name"] for item in installers} != expected:
            raise RuntimeError("Draft Windows installer filenames do not match the requested Vault Spend version.")
    for installer in installers:
        for suffix in ("", ".manifest.json", ".signature.json"):
            asset = by_name.get(installer["name"] + suffix, {})
            if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
                raise RuntimeError("Draft is missing a nonempty installer, manifest or signature: " + installer["name"] + suffix)
    url = release.get("html_url", "")
    if not url.startswith(f"https://github.com/{repository}/releases/"):
        raise RuntimeError("The draft has no valid repository release URL.")
    return url


def workflow_jobs(get, run_id, attempt):
    jobs = []
    for page in range(1, 101):
        result = get(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        batch = result.get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
    raise RuntimeError("Job listing exceeded the monitoring limit.")


# Anything that can move the cursor, retitle or recolor a terminal, or hide text: OSC and CSI sequences, other escapes, and control characters except tab and newline.
TERMINAL_CONTROL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\x5c)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe_text(text, token):
    """Display-safe text: control sequences removed first (so one cannot split the token to dodge redaction), then the token redacted."""
    text = TERMINAL_CONTROL.sub("", str(text))
    return text.replace(token, "[redacted]") if token else text


def workflow_artifacts(get, run_id):
    artifacts = []
    for page in range(1, 101):
        batch = get(f"actions/runs/{run_id}/artifacts?per_page=100&page={page}").get("artifacts", [])
        artifacts.extend(batch)
        if len(batch) < 100:
            return artifacts
    raise RuntimeError("Artifact listing exceeded the monitoring limit.")


def log_job_progress(target, jobs, events, token):
    seen = target.setdefault("job_progress", {})
    for job in jobs:
        entries = [(str(job.get("id", job["name"])), job["name"], job)]
        entries += [(f"{job.get('id', job['name'])}/{step['number']}", job["name"] + " > " + step["name"], step)
                    for step in job.get("steps", []) if step.get("status") not in ("queued", "pending")]
        for key, name, item in entries:
            marker = (item.get("status"), item.get("conclusion"))
            if marker == seen.get(key):
                continue
            seen[key] = marker
            label = item.get("conclusion") or str(item.get("status", "unknown")).replace("_", " ")
            if item.get("started_at") and item.get("completed_at"):
                try:
                    seconds = int((datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00")) - datetime.fromisoformat(item["started_at"].replace("Z", "+00:00"))).total_seconds())
                    label += f" ({seconds // 60}m {seconds % 60}s)"
                except (ValueError, TypeError):
                    pass
            events.put({"kind": "log", "message": safe_text(f"{target['workflow']}: {name}: {label}", token)})


def job_log_excerpt(repository, job_id, token):
    # Authenticate only the GitHub API request, never its signed storage redirect.
    request = Request(f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/logs",
                      headers={"Authorization": f"Bearer {token}", "User-Agent": "InternalUtils-Release-Tool"})
    try:
        response = build_opener(NoRedirect).open(request, timeout=15)
    except HTTPError as exc:
        if exc.code != 302:
            raise RuntimeError("Job logs are unavailable.") from None
        location = exc.headers.get("Location", "")
        if not location.startswith("https://"):
            raise RuntimeError("Invalid job log redirect.")
        response = urlopen(Request(location), timeout=15)
    with response:
        text = response.read(2_000_000).decode("utf-8", errors="replace")
    return excerpt_from_log(text, token)


def excerpt_from_log(text, token, limit=10):
    """The first `limit` error-looking lines of a failed job's log, cleaned and redacted. Only used after a failure; running logs are never streamed."""
    lines = [safe_text(line, token) for line in text.splitlines()]
    return "\n".join([line[:500] for line in lines if re.search(r"ENOENT|##\[error\]|Error:|FAILED|panic", line, re.I)][:limit])


def monitor_runs(repository, token, runs, events, stop=None, get=None, interval=15, timeout=21600, clock=time.monotonic, log_get=None):
    stop = stop or threading.Event()
    get = get or (lambda path: github_get(repository, path, token))
    log_get = log_get or (lambda job_id: job_log_excerpt(repository, job_id, token))
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
                    emit(target, "unknown", f"{target['workflow']}: monitoring timed out; the result is not confirmed. Check the run on GitHub; it keeps running.",
                         url=target.get("url") or (f"https://github.com/{repository}/actions/runs/{target['run_id']}" if target.get("run_id") else None))
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
                                  run["head_sha"] == target["head_sha"] and run["head_branch"] == target.get("run_ref", target["tag"]) and
                                  run["event"] == "workflow_dispatch" and
                                  (not target.get("run_title") or run.get("display_title") == target["run_title"]) and
                                  datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) >=
                                  (datetime.fromisoformat(target["requested_at"].replace("Z", "+00:00")) - timedelta(seconds=60)) and
                                  run["id"] not in target.get("previous_run_ids", [])]
                    if not candidates:
                        if clock() - target["discovery_started"] >= 120:
                            raise RuntimeError("The dispatched run could not be identified. Check GitHub Actions; no final result is confirmed.")
                        if target["last"] != "discovering":
                            emit(target, "queued", f"{target['workflow']}: waiting for its run to appear...")
                            target["last"] = "discovering"
                        continue
                    target["run_id"] = max(candidates, key=lambda run: run["id"])["id"]
                result = get(f"actions/runs/{target['run_id']}")
                if result["head_sha"] != target["head_sha"] or result["head_branch"] != target.get("run_ref", target["tag"]) or (target.get("run_title") and result.get("display_title") != target["run_title"]):
                    raise RuntimeError("Run does not match the requested tag and commit. Final result is not confirmed.")
                state, conclusion = result["status"], result.get("conclusion")
                url = f"https://github.com/{repository}/actions/runs/{target['run_id']}"
                target["url"] = url
                marker = (state, conclusion)
                recovered = target["errors"] > 0
                if target.get("track_jobs") or target.get("show_progress"):
                    # Gate runs (track_jobs) need the job list to decide the result. Other runs only display it, so an unreadable list must not make their result unknown.
                    try:
                        jobs = workflow_jobs(get, target["run_id"], result.get("run_attempt", 1))
                    except Exception:
                        if target.get("track_jobs"):
                            raise
                        jobs = None
                        if not target.get("progress_unavailable"):
                            target["progress_unavailable"] = True
                            events.put({"kind": "log", "message": f"{target['workflow']}: step progress is unavailable; the run status is still being monitored."})
                    if jobs is not None:
                        log_job_progress(target, jobs, events, token)
                        # A stale completed run response must not turn a job that is still active into success. Wait, and say which jobs, once.
                        unsettled = [job["name"] for job in jobs if job.get("status") != "completed"]
                        if state == "completed" and conclusion == "success" and (unsettled or (target.get("track_jobs") and not jobs)):
                            settling = ("settling", tuple(unsettled))
                            if target["last"] != settling:
                                emit(target, "waiting", f"{target['workflow']}: awaiting consistent completed job results" + (f" (still reported running: {safe_text(', '.join(unsettled[:5]), token)})." if unsettled else "."))
                                target["last"] = settling
                            continue
                if target.get("track_jobs") and state == "completed" and conclusion == "success":
                    for name in target.get("required_jobs", []):
                        matched = [job for job in jobs if job.get("name") == name]
                        if len(matched) != 1 or matched[0].get("conclusion") != "success":
                            raise RuntimeError(f"Required job {name} did not complete successfully.")
                    if any(job.get("conclusion") not in ("success", "skipped") for job in jobs):
                        raise RuntimeError("A job failed despite the workflow reporting success.")
                    # The gate's own summary must exist for exactly this run, attempt and source: it is the aggregate evidence the jobs vouch for.
                    for pattern in target.get("required_artifacts", []):
                        name = pattern.format(attempt=result.get("run_attempt", 1))
                        matched = [item for item in workflow_artifacts(get, target["run_id"]) if item.get("name") == name]
                        if (len(matched) != 1 or matched[0].get("expired") or not matched[0].get("size_in_bytes")
                                or (matched[0].get("workflow_run") or {}).get("id") != target["run_id"]
                                or (matched[0].get("workflow_run") or {}).get("head_sha") != target["head_sha"]):
                            raise RuntimeError(f"Gate evidence {name} is missing or does not belong to this run.")
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
                            for job in workflow_jobs(get, target["run_id"], result.get("run_attempt", 1)):
                                if job.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required"):
                                    steps = [step["name"] for step in job.get("steps", []) if step.get("conclusion") == "failure"]
                                    suffix = "; failed step: " + ", ".join(steps) if steps else ""
                                    events.put({"kind": "log", "message": safe_text(f"Failed job: {job['name']}{suffix}", token)})
                                    if job.get("id"):
                                        try:
                                            excerpt = log_get(job["id"])
                                            if excerpt:
                                                events.put({"kind": "log", "message": "Failure log excerpt: " + safe_text(excerpt, token)})
                                        except Exception:
                                            events.put({"kind": "log", "message": "Failure log excerpt unavailable; open the run for full logs."})
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
