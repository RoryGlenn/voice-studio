"""Serve a read-only, auto-refreshing progress page for one local audiobook job."""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audiobook progress</title>
<style>
  :root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; background: #10191b; color: #eaf1ed; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
  main { width: min(720px, calc(100% - 36px)); padding: 36px 0; }
  .eyebrow { color: #8db9aa; text-transform: uppercase; letter-spacing: .16em; font-size: .75rem; font-weight: 700; }
  h1 { font-size: clamp(1.8rem, 5vw, 3rem); line-height: 1.1; margin: .5rem 0 1rem; }
  #stage { color: #bacac4; margin-bottom: 2rem; }
  .card { background: #1b292a; border: 1px solid #334947; border-radius: 16px; padding: 24px; margin: 14px 0; }
  .row { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
  .row strong { font-size: 1.08rem; }
  .row span { font-variant-numeric: tabular-nums; color: #a8c0b7; }
  progress { width: 100%; height: 18px; margin: 18px 0 5px; accent-color: #79dfb1; }
  .small { color: #9db0aa; font-size: .85rem; }
  #message.failed { color: #ffad9c; }
  #message.complete { color: #8be4b3; }
  footer { margin-top: 26px; color: #91a8a0; font-size: .8rem; }
</style>
<main>
  <div class="eyebrow">Local audiobook conversion</div>
  <h1>Designing Data-Intensive Applications</h1>
  <p id="stage" aria-live="polite">Connecting to the local job…</p>
  <div class="card">
    <div class="row"><strong>Narration</strong><span id="render-count">—</span></div>
    <progress id="render-bar" max="100" value="0" aria-label="Narration progress"></progress>
    <div class="row small"><span>Passages with saved audio</span><span id="render-percent">0%</span></div>
  </div>
  <div class="card">
    <div class="row"><strong>Quality check</strong><span id="check-count">—</span></div>
    <progress id="check-bar" max="100" value="0" aria-label="Quality check progress"></progress>
    <div class="row small"><span id="held-count">0 flagged for review</span><span id="check-percent">0%</span></div>
  </div>
  <p id="message" aria-live="polite"></p>
  <footer>Updates every 2 seconds from saved local checkpoints. This page does not upload book content.</footer>
</main>
<script>
  const $ = id => document.getElementById(id);
  async function update() {
    try {
      const response = await fetch('/api/progress', {cache: 'no-store'});
      if (!response.ok) throw new Error(`Progress request failed (${response.status})`);
      const data = await response.json();
      $('stage').textContent = data.stage;
      $('render-count').textContent = `${data.generated.toLocaleString()} / ${data.total.toLocaleString()}`;
      $('check-count').textContent = `${data.checked.toLocaleString()} / ${data.total.toLocaleString()}`;
      $('render-bar').value = data.render_percent;
      $('check-bar').value = data.check_percent;
      $('render-percent').textContent = `${data.render_percent.toFixed(1)}%`;
      $('check-percent').textContent = `${data.check_percent.toFixed(1)}%`;
      $('held-count').textContent = `${data.held.toLocaleString()} flagged for review`;
      $('message').textContent = data.message;
      $('message').className = data.state;
    } catch (error) {
      $('stage').textContent = 'Progress connection lost';
      $('message').textContent = String(error);
      $('message').className = 'failed';
    }
  }
  update();
  setInterval(update, 2000);
</script>
</html>
"""


def progress(job: Path, output: Path) -> dict[str, Any]:
    """Read only atomic status and pipeline markers; never load book text."""
    status = json.loads((job / "status.json").read_text())
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
        except (ProcessLookupError, KeyError):
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
        "render_percent": 100 * generated / total,
        "check_percent": 100 * checked / total,
        "message": health.get("message") or message,
    }


def serve(job: Path, output: Path, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                data, kind = PAGE.encode(), "text/html; charset=utf-8"
            elif self.path == "/api/progress":
                data = json.dumps(progress(job, output)).encode()
                kind = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(
        f"Live audiobook progress: http://127.0.0.1:{server.server_port}/", flush=True
    )
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(
        args.job.expanduser().resolve(), args.output.expanduser().resolve(), args.port
    )
