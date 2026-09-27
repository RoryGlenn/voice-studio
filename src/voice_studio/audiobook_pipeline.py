"""Supervise resumable local workers; never reuse a poisoned CUDA process."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from voice_studio.render_book import save_json

REPOSITORY = Path(__file__).resolve().parents[2]
RETRYABLE = ("illegal memory access", "Cache thrashing")


class Pipeline:
    def __init__(
        self,
        job: Path,
        output: Path,
        *,
        batch: int = 20,
        check_batch: int = 50,
        retry_limit: int = 5,
        backoff: float = 3,
        timeout: float = 900,
    ):
        self.job, self.output = job, output
        if batch <= 0 or check_batch <= 0:
            raise ValueError("Worker batch sizes must be positive")
        self.batch, self.retry_limit = batch, retry_limit
        self.check_batch = check_batch
        self.backoff, self.timeout = backoff, timeout
        self.child = None
        self.stopping = False
        self.state = {}
        previous = job / "pipeline-status.json"
        if previous.exists():
            self.state = json.loads(previous.read_text())
        self.failures = self.state.get("failures", 0)
        self.last_progress = self.state.get("progress")

    def publish(self, stage: str, message: str = "") -> None:
        self.state.update(
            state=stage,
            message=message,
            heartbeat=time.time(),
            pid=os.getpid(),
            child_pid=self.child.pid if self.child else None,
            failures=self.failures,
            progress=self.last_progress,
        )
        save_json(self.job / "pipeline-status.json", self.state)
        # Compatibility for older readers; authoritative health is the JSON heartbeat.
        (self.job / "pipeline-state.txt").write_text(stage + "\n")

    def stop(self, *_: object) -> None:
        self.stopping = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def run_worker(self, stage: str, command: list[str]) -> tuple[int, str]:
        logs = self.job / "worker-logs"
        logs.mkdir(exist_ok=True)
        log = logs / f"{time.time_ns()}-{stage}.log"
        self.state["log"] = str(log)
        started = changed = time.monotonic()
        stamp = None
        timed_out = False
        with log.open("wb") as handle:
            self.child = subprocess.Popen(
                command, cwd=REPOSITORY, stdout=handle, stderr=subprocess.STDOUT
            )
            try:
                while self.child.poll() is None:
                    now = time.monotonic()
                    status = self.job / "status.json"
                    new_stamp = status.stat().st_mtime_ns if status.exists() else None
                    if new_stamp != stamp:
                        stamp, changed = new_stamp, now
                    # Packaging has no per-passage status; give it a separate generous cap.
                    limit = 6 * 3600 if stage == "packaging" else self.timeout
                    if now - (started if stage == "packaging" else changed) > limit:
                        timed_out = True
                        self.child.kill()
                    if self.stopping and now - started > 5:
                        self.child.kill()
                    self.publish(stage)
                    time.sleep(1)
                code = self.child.wait()
            finally:
                if self.child.poll() is None:
                    self.child.kill()
                    self.child.wait()
                self.child = None
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 65536))
            detail = handle.read().decode(errors="replace")
        if timed_out:
            detail += "\nWorker exceeded progress timeout"
        return code, detail

    def wait_for_gpu(self) -> bool:
        """Yield to interactive GPU users between workers; do not kill their apps."""
        minimum = int(os.environ.get("VOICE_STUDIO_MIN_FREE_GPU_MIB", "0"))
        if not minimum:
            return True
        while not self.stopping and not (self.job / "paused").exists():
            query = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free,utilization.gpu",
                    "--format=csv,noheader,nounits",
                    "--id=0",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            free, utilization = map(int, query.stdout.strip().split(","))
            if free >= minimum and utilization <= 35:
                return True
            self.publish(
                "waiting",
                f"Waiting for GPU: {free} MiB free, {utilization}% busy; need {minimum} MiB free and at most 35% busy",
            )
            for _ in range(10):
                if self.stopping or (self.job / "paused").exists():
                    break
                time.sleep(1)
        return False

    def counts(self) -> dict:
        return json.loads((self.job / "status.json").read_text())["counts"]

    def execute(self) -> int:
        command = [sys.executable, str(REPOSITORY / "run.py"), "audiobook"]
        stage = "recovering"
        if (
            self.state.get("state") == "complete"
            and (self.output / "audiobook.m4b").is_file()
        ):
            return 0
        try:
            if self.failures >= self.retry_limit:
                self.publish(
                    "failed",
                    "Retry limit reached; inspect logs before resetting failures",
                )
                return 2
            code, detail = self.run_worker(
                stage, command + ["recover", "--job", str(self.job)]
            )
            if code:
                raise RuntimeError("Checkpoint recovery failed: " + detail[-1500:])
            while not self.stopping:
                if (self.job / "paused").exists():
                    self.publish("paused", "Paused by user")
                    return 0
                counts = self.counts()
                stage = (
                    "rendering"
                    if counts["pending"]
                    else "checking"
                    if counts["rendered"]
                    else "packaging"
                )
                progress = [counts["pending"], counts["rendered"]]
                if self.last_progress != progress:
                    self.failures = 0
                self.last_progress = progress
                if stage == "packaging":
                    args = [
                        sys.executable,
                        str(REPOSITORY / "run.py"),
                        "draft",
                        "--job",
                        str(self.job),
                        "--output",
                        str(self.output),
                    ]
                else:
                    args = command + [
                        "render" if stage == "rendering" else "check",
                        "--job",
                        str(self.job),
                    ]
                    batch = self.batch if stage == "rendering" else self.check_batch
                    args += ["--max-segments", str(batch)]
                if stage != "packaging" and not self.wait_for_gpu():
                    continue
                code, detail = self.run_worker(stage, args)
                if self.stopping:
                    break
                after = self.counts()
                new_progress = [after["pending"], after["rendered"]]
                if code == 0:
                    if stage == "packaging":
                        if not (self.output / "audiobook.m4b").is_file():
                            raise RuntimeError("Packager exited without audiobook.m4b")
                        self.publish("complete", str(self.output / "audiobook.m4b"))
                        return 0
                    if new_progress == progress and not (self.job / "paused").exists():
                        raise RuntimeError(
                            "Worker exited successfully without progress"
                        )
                    self.failures = 0
                    continue
                if not any(marker in detail for marker in RETRYABLE):
                    raise RuntimeError(
                        f"{stage} worker exited {code}: " + detail[-1500:]
                    )
                self.failures = 1 if new_progress != progress else self.failures + 1
                self.last_progress = new_progress
                if self.failures >= self.retry_limit:
                    raise RuntimeError(
                        "CUDA retry limit reached without checkpoint progress"
                    )
                delay = min(60, self.backoff * 2 ** (self.failures - 1))
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self.stopping:
                    self.publish(
                        "retrying",
                        f"{stage}: CUDA abort; fresh worker retry {self.failures}/{self.retry_limit}",
                    )
                    time.sleep(min(1, delay))
            self.publish("stopped", "Supervisor stopped; saved checkpoints retained")
            return 0
        except Exception as exc:
            self.publish("failed", str(exc))
            print(str(exc), file=sys.stderr, flush=True)
            return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--render-batch-size", type=int, default=20)
    parser.add_argument("--check-batch-size", type=int, default=50)
    args = parser.parse_args()
    if args.render_batch_size <= 0 or args.check_batch_size <= 0:
        parser.error("Worker batch sizes must be positive")
    job = args.job.resolve()
    with (job / "scheduled.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        pipeline = Pipeline(
            job,
            args.output.resolve(),
            batch=args.render_batch_size,
            check_batch=args.check_batch_size,
        )
        signal.signal(signal.SIGTERM, pipeline.stop)
        signal.signal(signal.SIGINT, pipeline.stop)
        return pipeline.execute()


if __name__ == "__main__":
    raise SystemExit(main())
