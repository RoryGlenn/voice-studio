"""Local audiobook workspace: cached library, history, and guarded actions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from voice_studio.workspace_insights import insights, wording_diff

REPO = Path(__file__).resolve().parents[2]


def atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


class Workspace:
    def __init__(self, job, output, progress_reader, monitor, service=None):
        self.job, self.output = job, output
        self.progress_reader, self.monitor, self.service = (
            progress_reader,
            monitor,
            service,
        )
        self.plan = json.loads((job / "plan.json").read_text())
        self.segments = {
            f"{t['track']}/{s['number']}": (t, s)
            for t in self.plan["tracks"]
            for s in t["segments"]
        }
        self.cache, self.snapshot_value = {}, {}
        self.lock, self.action_lock = threading.Lock(), threading.Lock()
        self.action = {"state": "idle", "message": ""}
        self.controls_enabled = True
        self.history_path = job / "dashboard-history.json"
        try:
            self.history = json.loads(self.history_path.read_text())[-720:]
        except (OSError, ValueError):
            self.history = []
        self.last_history = 0
        self.download_stamp = None
        self.download_valid = False

    def record_path(self, key):
        track, segment = self.segments[key]
        return (
            self.job
            / "segments"
            / f"{track['track']:04d}"
            / f"{segment['number']:04d}.json"
        )

    def record(self, key):
        path = self.record_path(key)
        try:
            stamp = path.stat().st_mtime_ns
            if key not in self.cache or self.cache[key][0] != stamp:
                self.cache[key] = stamp, json.loads(path.read_text())
            return self.cache[key][1]
        except FileNotFoundError:
            return None

    def safe_audio(self, key):
        if key not in self.segments:
            raise KeyError("Unknown passage")
        row = self.record(key)
        if not row:
            raise FileNotFoundError("Audio has not been generated")
        path = Path(
            row.get("paced_file") if row["status"] == "verified" else row["raw_file"]
        ).resolve()
        if (
            not path.is_relative_to((self.job / "segments").resolve())
            or not path.is_file()
        ):
            raise ValueError("Audio is outside this job or unavailable")
        return path

    def final_ready(self, state):
        if state != "complete":
            return False
        try:
            path, report_path = (
                self.output / "audiobook.m4b",
                self.output / "draft_report.json",
            )
            stale = self.job / "dashboard-package-stale"
            if stale.exists():
                if report_path.stat().st_mtime_ns <= stale.stat().st_mtime_ns:
                    return False
                stale.unlink()
            stamp = (
                path.stat().st_mtime_ns,
                path.stat().st_size,
                report_path.stat().st_mtime_ns,
            )
            if stamp != self.download_stamp:
                report = json.loads(report_path.read_text())
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                self.download_valid = (
                    report.get("identity") == self.plan["identity_sha256"]
                    and report.get("full_audio_decode") == "pass"
                    and report.get("m4b_sha256") == digest
                )
                self.download_stamp = stamp
            return self.download_valid
        except (OSError, ValueError, KeyError):
            return False

    def refresh(self):
        data = self.progress_reader(self.job, self.output)
        data["activity"] = self.monitor.snapshot()
        if (self.job / "paused").exists() and data["state"] == "stopped":
            data["state"], data["stage"] = "paused", "Paused"
        chapters, review, recent = [], [], []
        audio_seconds = 0
        for track in self.plan["tracks"]:
            chapter = {
                "number": track["track"],
                "title": track["title"],
                "total": len(track["segments"]),
                "generated": 0,
                "checked": 0,
                "flagged": 0,
                "seconds": 0,
                "play": None,
            }
            for segment in track["segments"]:
                key = f"{track['track']}/{segment['number']}"
                row = self.record(key)
                if not row:
                    continue
                chapter["generated"] += 1
                chapter["checked"] += row["status"] in ("verified", "needs_review")
                chapter["flagged"] += row["status"] == "needs_review"
                chapter["seconds"] += row.get("duration_seconds", 0)
                chapter["play"] = chapter["play"] or key
                recent.append(
                    {
                        "id": key,
                        "chapter": track["title"],
                        "status": row["status"],
                        "at": self.cache[key][0] / 1e9,
                    }
                )
                if row["status"] == "needs_review":
                    wording = row.get("wording_check") or {}
                    reasons = []
                    if wording.get("flagged"):
                        reasons.append("Wording differs")
                    if row.get("timing_error"):
                        reasons.append(row["timing_error"])
                    if row.get("signal_metrics", {}).get("flagged"):
                        reasons.append("Audio signal needs review")
                    review.append(
                        {
                            "id": key,
                            "chapter": track["title"],
                            "expected": segment["speech_text"],
                            "recognized": wording.get("recognized", ""),
                            "diff": wording_diff(
                                segment["speech_text"], wording.get("recognized", "")
                            ),
                            "reasons": reasons,
                            "fingerprint": row["raw_sha256"],
                            "attempt": row["attempt"] + 1,
                        }
                    )
            chapters.append(chapter)
            audio_seconds += chapter["seconds"]
        now = time.time()
        sample = {
            "at": now,
            "generated": data["generated"],
            "checked": data["checked"],
            "state": data["state"],
            "cpu": data["activity"].get("cpu_percent"),
            "gpu": (data["activity"].get("gpu") or {}).get("utilization"),
        }
        if now - self.last_history >= 5:
            self.history = [h for h in self.history if now - h["at"] < 3600][-719:] + [
                sample
            ]
            atomic(self.history_path, self.history)
            self.last_history = now
        phase_key = "generated" if data["state"] == "rendering" else "checked"
        stable = []
        for h in reversed(self.history):
            if (
                h["state"] != data["state"]
                or now - h["at"] > 300
                or (stable and stable[-1]["at"] - h["at"] > 20)
            ):
                break
            stable.append(h)
        estimate = None
        if data["state"] in ("rendering", "checking") and stable:
            oldest = stable[-1]
            elapsed = now - oldest["at"]
            gained = data[phase_key] - oldest[phase_key]
            if elapsed >= 60 and gained >= 3:
                estimate = (data["total"] - data[phase_key]) * elapsed / gained
        ready = self.final_ready(data["state"])
        data.update(insights(self.job, self.output, data, ready))
        data.update(
            job_id=self.job.name,
            controls_enabled=self.controls_enabled,
            settings={
                key: self.plan.get(key) for key in ("engine", "narrator", "year")
            },
            book={
                "title": self.plan["title"],
                "author": self.plan["author"],
                "narrator": self.plan.get("narrator", ""),
                "chapters": len(chapters),
            },
            chapters=chapters,
            review=review,
            recent=sorted(recent, key=lambda r: r["at"], reverse=True)[:8],
            audio_seconds=audio_seconds,
            history=self.history[-60:],
            estimate_seconds=estimate,
            pause_requested=(self.job / "paused").exists(),
            download_ready=ready,
            action=dict(self.action),
            resume_supported=bool(self.service),
        )
        with self.lock:
            self.snapshot_value = data
        return data

    def snapshot(self):
        with self.lock:
            return self.snapshot_value

    def run(self):
        while True:
            try:
                self.refresh()
            except Exception as exc:
                with self.lock:
                    self.snapshot_value = {
                        **self.snapshot_value,
                        "workspace_error": str(exc),
                    }
            time.sleep(2)

    def idle(self):
        with (self.job / "scheduled.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

    def request(self, payload):
        if not self.controls_enabled:
            raise ValueError("This job is view-only")
        action = payload.get("action")
        if action not in ("pause", "resume", "approve", "regenerate"):
            raise ValueError("Unknown action")
        if not self.action_lock.acquire(blocking=False):
            raise ValueError("Another action is still running")
        try:
            if action == "pause":
                (self.job / "paused").write_text("Paused from audiobook workspace\n")
                self.action = {
                    "state": "complete",
                    "message": "Pause requested. The current passage will finish first; packaging may finish before pausing.",
                }
                return self.action
            if not self.idle():
                raise ValueError(
                    "Wait for the worker to finish pausing before continuing"
                )
            if action == "resume":
                if not self.service:
                    raise ValueError(
                        "No background service configured for this workspace"
                    )
                (self.job / "paused").unlink(missing_ok=True)
                # Invalidate completed-package state only when a review changed the audio.
                health_path = self.job / "pipeline-status.json"
                health = (
                    json.loads(health_path.read_text()) if health_path.exists() else {}
                )
                if (self.job / "dashboard-package-stale").exists():
                    health["state"] = "stopped"
                    atomic(health_path, health)
                try:
                    subprocess.run(
                        ["systemctl", "--user", "start", self.service],
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    (self.job / "paused").touch()
                    raise
                self.action = {
                    "state": "complete",
                    "message": "Resume requested. Waiting for the worker heartbeat.",
                }
                return self.action
            key = payload.get("id")
            if key not in self.segments:
                raise ValueError("Unknown passage")
            if not (self.job / "paused").exists():
                raise ValueError("Pause the job before reviewing or regenerating audio")
            row = self.record(key)
            if (
                not row
                or row["status"] != "needs_review"
                or row["raw_sha256"] != payload.get("fingerprint")
            ):
                raise ValueError(
                    "This passage changed. Refresh the review queue before acting."
                )
            if action == "approve" and (
                not payload.get("transcript", "").strip()
                or not payload.get("note", "").strip()
            ):
                raise ValueError("Enter the words you heard and a review note")
            self.action = {
                "state": "running",
                "message": f"{'Reviewing' if action == 'approve' else 'Regenerating'} passage {key}…",
            }
            request_path = self.job / "dashboard-actions" / f"{time.time_ns()}.json"
            request_path.parent.mkdir(exist_ok=True)
            atomic(request_path, payload)
            threading.Thread(
                target=self.execute, args=(request_path,), daemon=True
            ).start()
            return self.action
        finally:
            if self.action.get("state") != "running":
                self.action_lock.release()

    def execute(self, request_path):
        try:
            env = dict(os.environ, VOICE_STUDIO_CUDA_CONV_CACHE_SIZE="2048")
            result = subprocess.run(
                [
                    str(REPO / ".venv/bin/python"),
                    str(REPO / "run.py"),
                    "workspace-action",
                    "--job",
                    str(self.job),
                    "--request",
                    str(request_path),
                ],
                cwd=REPO,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if result.returncode:
                raise ValueError((result.stderr or result.stdout)[-1500:])
            self.action = {
                "state": "complete",
                "message": result.stdout.strip().splitlines()[-1],
            }
        except Exception as exc:
            self.action = {"state": "failed", "message": str(exc)}
        finally:
            self.action_lock.release()

    def passages(self, track_number):
        track = next(t for t in self.plan["tracks"] if t["track"] == track_number)
        return [
            {
                "id": f"{track_number}/{s['number']}",
                "number": s["number"],
                "text": s["speech_text"],
                "status": (self.record(f"{track_number}/{s['number']}") or {}).get(
                    "status", "pending"
                ),
            }
            for s in track["segments"]
        ]
