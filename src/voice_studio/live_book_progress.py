"""Serve the local audiobook workspace, playback, and system activity monitor."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


def progress(job: Path, output: Path) -> dict[str, Any]:
    """Read only atomic status and pipeline markers; never load book text."""
    status_path = job / "status.json"
    status = json.loads(status_path.read_text())
    now = time.time()
    counts = status["counts"]
    total = status["total_segments"]
    generated = total - counts["pending"]
    checked = counts["verified"] + counts["needs_review"]
    health_path = job / "pipeline-status.json"
    health = json.loads(health_path.read_text()) if health_path.exists() else {}
    state = health.get("state", "stopped")
    if state in {
        "rendering",
        "checking",
        "packaging",
        "recovering",
        "retrying",
        "waiting",
    }:
        try:
            os.kill(health["pid"], 0)
            alive = time.time() - health["heartbeat"] < 30
        except (ProcessLookupError, PermissionError, KeyError, TypeError):
            alive = False
        if not alive:
            state = "stopped"
    stages = {
        "rendering": "Generating chapter audio",
        "recovering": "Validating saved checkpoints",
        "retrying": "Restarting after a GPU error",
        "stopped": "Worker stopped; saved progress retained",
        "waiting": "Waiting for available GPU capacity",
        "paused": "Paused",
        "checking": "Checking generated speech",
        "packaging": "Building chapter files and M4B",
        "complete": "Draft audiobook is ready",
        "failed": "Conversion stopped; see the job log",
    }
    message = (
        f"Finished: {output / 'audiobook.m4b'}"
        if state == "complete" and (output / "audiobook.m4b").is_file()
        else "Paused by user."
        if state == "paused"
        else "The saved passages are intact. The worker needs attention before it can continue."
        if state in {"failed", "stopped"}
        else "The draft will use original audio for flagged passages."
    )
    return {
        "state": state,
        "stage": stages.get(state, state.capitalize()),
        "total": total,
        "generated": generated,
        "checked": checked,
        "held": counts["needs_review"],
        "remaining": counts["pending"],
        "verified": counts["verified"],
        "awaiting_check": counts["rendered"],
        "unchecked": total - checked,
        "saved_age_seconds": max(0, now - status_path.stat().st_mtime),
        "heartbeat_age_seconds": max(0, now - health["heartbeat"])
        if health.get("heartbeat") is not None
        else None,
        "worker_pid": health.get("pid"),
        "failures": health.get("failures", 0),
        "observed_at": now,
        "output_ready": state == "complete" and (output / "audiobook.m4b").is_file(),
        "render_percent": 100 * generated / total if total else 0,
        "check_percent": 100 * checked / total if total else 0,
        "message": health.get("message") or message,
    }


class ActivityMonitor:
    """Sample locally in one background thread so page requests never wait on the GPU."""

    def __init__(self):
        self.previous_cpu = {}
        self.previous_processes = {}
        self.process_time = None
        self.lock = threading.Lock()
        self.latest = {"sampled_at": None, "cpu_percent": None, "gpu": None}

    def sample(self):
        cpu, cores = None, []
        try:
            current = {}
            for line in Path("/proc/stat").read_text().splitlines():
                fields = line.split()
                if not fields or not fields[0].startswith("cpu"):
                    continue
                name = fields[0]
                values = list(map(int, fields[1:9]))
                total, idle = sum(values), values[3] + values[4]
                current[name] = total, idle
                previous = self.previous_cpu.get(name)
                percent = None
                if previous and total > previous[0]:
                    percent = round(
                        max(
                            0,
                            min(
                                100,
                                100
                                * (1 - (idle - previous[1]) / (total - previous[0])),
                            ),
                        ),
                        1,
                    )
                if name == "cpu":
                    cpu = percent
                else:
                    cores.append({"name": name, "percent": percent})
            self.previous_cpu = current
        except (OSError, ValueError, IndexError):
            self.previous_cpu = {}
        memory = self.read_memory()
        processes = self.read_processes()
        gpu = None
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--id=0",
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1,
                check=True,
            )
            row = next(csv.reader(result.stdout.splitlines(), skipinitialspace=True))

            def number(value):
                try:
                    parsed = float(value)
                    return parsed if math.isfinite(parsed) else None
                except ValueError:
                    return None

            gpu = (
                dict(
                    zip(
                        (
                            "name",
                            "utilization",
                            "memory_used",
                            "memory_total",
                            "temperature",
                        ),
                        [row[0], *map(number, row[1:5])],
                    )
                )
                if len(row) == 5
                else None
            )
        except (OSError, subprocess.SubprocessError, StopIteration, csv.Error):
            pass
        with self.lock:
            self.latest = {
                "sampled_at": time.time(),
                "cpu_percent": cpu,
                "gpu": gpu,
                "cores": cores,
                "memory": memory,
                "processes": processes,
            }

    @staticmethod
    def read_memory():
        try:
            with open("/proc/meminfo") as handle:
                fields = {
                    line.split(":")[0]: int(line.split()[1]) * 1024 for line in handle
                }
            total, available = fields["MemTotal"], fields["MemAvailable"]
            return {
                "total": total,
                "available": available,
                "used": total - available,
                "percent": round(100 * (total - available) / total, 1),
                "swap_total": fields["SwapTotal"],
                "swap_used": fields["SwapTotal"] - fields["SwapFree"],
            }
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            return None

    def read_processes(self):
        now = time.monotonic()
        elapsed = now - self.process_time if self.process_time is not None else 0
        rows, current = [], {}
        ticks, page_size = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        for pid in entries:
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat") as handle:
                    stat = handle.read()
                # comm can contain spaces and parentheses; numeric fields follow the last ')'.
                end = stat.rfind(")")
                name = stat[stat.index("(") + 1 : end]
                fields = stat[end + 2 :].split()
                consumed = int(fields[11]) + int(fields[12])
                key = (
                    int(pid),
                    int(fields[19]),
                )  # Start time protects against PID reuse.
                current[key] = consumed
                previous = self.previous_processes.get(key)
                percent = (
                    round(max(0, (consumed - previous) / ticks / elapsed * 100), 1)
                    if previous is not None and elapsed > 0
                    else None
                )
                rows.append(
                    {
                        "pid": int(pid),
                        "name": "".join(c for c in name if c.isprintable())[:32],
                        "cpu_percent": percent,
                        "rss": max(0, int(fields[21])) * page_size,
                    }
                )
            except (OSError, ValueError, IndexError):
                continue  # Processes can exit between directory listing and read.
        self.previous_processes, self.process_time = current, now
        return sorted(
            rows, key=lambda row: (row["cpu_percent"] or 0, row["rss"]), reverse=True
        )[:8]

    def snapshot(self):
        with self.lock:
            return dict(self.latest)

    def run(self):
        while True:
            started = time.monotonic()
            self.sample()
            time.sleep(max(0.1, 2 - (time.monotonic() - started)))


def serve(job: Path, output: Path, port: int, service: str | None = None) -> None:
    from voice_studio.audiobook_workspace import Workspace, atomic
    from voice_studio.workspace_http import make_handler
    from voice_studio.workspace_library import Library

    monitor = ActivityMonitor()
    monitor.sample()
    workspace = Workspace(job, output, progress, monitor, service)
    workspace.refresh()
    atomic(job / "dashboard-config.json", {"output": str(output), "service": service})
    library = Library(workspace)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(workspace, port, library)
    )
    threading.Thread(target=monitor.run, daemon=True).start()
    threading.Thread(target=workspace.run, daemon=True).start()
    print(f"Audiobook workspace: http://127.0.0.1:{server.server_port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--service", help="User systemd unit that owns this job, for Resume"
    )
    args = parser.parse_args()
    serve(
        args.job.expanduser().resolve(),
        args.output.expanduser().resolve(),
        args.port,
        args.service,
    )
