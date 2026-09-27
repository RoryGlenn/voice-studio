# Run and recover background audiobook jobs

Use a foreground supervisor for short runs or a persistent user service for unattended work. This page describes general operation; exact commands for the existing book are in the [DDIA runbook](workstation-ddia.md).

## Use the audiobook workspace

Open the configured localhost dashboard (DDIA: `http://127.0.0.1:8765/`).

Use **Theme** in the header to choose Forest or Midnight (dark), or Paper or Slate (light). The choice applies to every workspace view, including charts and review highlights, and is saved in this browser across page reloads and book changes. If browser storage is unavailable, the choice lasts until the page reloads.

The **Book library** lists sibling job directories containing `plan.json` and `status.json`, including their current state and saved counts. Selecting a book opens its own `/jobs/JOB/` URL; it never switches another tab's job or starts narration. Jobs without a saved dashboard configuration are view-only. Launching `run.py workspace --job PATH --output PATH --service UNIT` registers that job's output and service in its private `dashboard-config.json`. Restart the library dashboard after changing another job's configuration if that job is already open there. Jobs in other parent directories need their own workspace instance.

- **Overview** separates narration, quality checks, packaging, and ready status. Chapter rows show saved passages and checked/flagged counts. **Listen** opens a passage selector and player; it advances to the next available passage in that chapter. Unchecked and flagged audio is explicitly labeled.
- **Review** shows expected wording, the recognizer's transcript, and flag reasons. Amber highlighting on the left marks missing or changed expected text; rose highlighting on the right marks differing recognized text. Comparison ignores letter case but includes punctuation. These highlights locate transcript differences, not proven narration mistakes; timing and signal flags still require listening. Click **Pause after passage**, wait for the paused state, then listen before recording a review. Enter the words actually heard and a note. Strict wording and timing validation still applies; an approval can leave a timing flag unresolved. **Regenerate passage** preserves previous attempts, renders only that passage, and checks it. The job remains paused until you choose **Resume conversion**.
- **System** shows whole-machine CPU/GPU utilization and separate RAM/VRAM capacity charts. Choose **2 min**, **15 min**, or **1 hour** for all charts. Hover for synchronized time/value inspection, or focus a chart and use the arrow keys. Each chart shows current usage, sample average, and peak; utilization uses a fixed 0–100% scale and memory uses GiB against capacity. Missing readings and collection gaps stay blank; loss of one metric does not erase the others.
- The **Worker timeline** and dashed chart markers show observed state changes such as narration, checking, GPU waits, and retries. These are sampled observations, not precise profiler events. Older history may lack RAM/VRAM; those series fill as new samples arrive. The core heatmap shows each logical CPU, and process table headers sort the sampled top processes by name, PID, CPU, or RAM. Process CPU percentages and bars use total machine capacity, matching the CPU chart; the adjacent core count shows equivalent logical CPUs in use. For example, using 1.5 cores on a machine with 20 logical CPUs is 7.5% of total CPU. If the logical CPU count is unavailable, the percentage shows a dash. Memory is resident memory. These readings describe the whole computer, not only the audiobook worker.

**History** shows measured worker durations, success/failure, and render/check batch limits, with the job's engine settings. New supervisors append results to `worker-history.jsonl`, including before/after counts and cache settings. Recording begins when the updated supervisor starts; an already-running supervisor continues uninterrupted and older timings are not fabricated. The page displays the latest 30 records. Duration includes model startup and checkpoint work; compare similar audio workloads before drawing speed conclusions.

The completion message distinguishes unfinished narration, outstanding checks, packaging/validation, and a validated download. Alerts identify stopped workers, repeated failures, more than 15 minutes without a saved passage during rendering/checking, and less than 10 GiB free on job/output disks. Alerts offer guidance; they do not delete files or restart workers.

Updates use a local event stream every two seconds, with polling fallback. The job's `dashboard-history.json` retains up to one hour of sampled counts and CPU/GPU/RAM/VRAM activity; recent history survives refreshes and service restarts. Live samples arrive about every two seconds; persisted samples are written at roughly five-to-six-second intervals. Averages are over available samples, so a mixed live/persisted window is not a time-weighted average. Estimates apply only to the active narration/checking stage and require steady observed progress. They exclude packaging.

**Pause after passage** lets the current passage finish. A packaging operation may finish before it can pause. Resume uses the user service passed with `--service`; it does not launch a duplicate worker. Workspaces without that option are read-only for resume. Review actions hold both pipeline and checkpoint locks, use the configured local Python interpreter, and retain their requests and listening evidence under `dashboard-actions/` and the segment's review/history directories. No review decisions are inferred automatically.

The download button appears only after a matching draft report records a successful full decode and the final M4B checksum matches. Reviewing/regenerating a completed job invalidates that download until the draft is rebuilt. A validated draft may still contain held passages.

Example for an existing job and its own service:

```sh
python3 run.py workspace --job data/books/jobs/my-book \
  --output data/outputs/my-book-draft --port 8766 \
  --service voice-studio-my-book.service
```

All routes bind to loopback. Mutating requests require the local page's session token and same-origin checks. Audio routes resolve only known passages inside the selected job.

## Understand progress and recover from a stop

The dashboard separates saved work from worker health:

| Display | Meaning and next action |
| --- | --- |
| Narration percentage | Passages with saved audio divided by total passages; it is not a time estimate. |
| Quality-check percentage | Passages checked, including those flagged for review; it is not the percentage that passed. |
| Waiting for available GPU capacity | GPU 0 needs at least 6000 MiB free and utilization at most 35% with the example settings. Close GPU-heavy apps when convenient; the supervisor checks again automatically. |
| Restarting after a GPU error | A recognized CUDA error triggered a fresh worker. Let the bounded retry run. |
| Worker stopped / conversion stopped | Inspect the saved failure and log before restarting. Checkpoints remain available. |
| Draft audiobook is ready | Packaging finished. Listen to the result and review flagged passages. |

The GPU check happens between workers. It does not reserve memory or stop a game
launched during a batch. The underlying CUDA fault is not claimed fixed.

For the configured DDIA service, inspect the service log and the saved worker
status:

```sh
journalctl --user -u voice-studio-ddia -n 50 --no-pager
cd ~/repos/voice-studio
python3 -m json.tool \
  data/books/jobs/designing-data-intensive-applications-audio/pipeline-status.json
```

The JSON's `message` describes a failure; `log` points to the individual worker's
log file. For another book, substitute its job directory. `status.json` contains
passage counts; `pipeline-status.json` contains supervisor health. A saved
`ready` state in the passage counts is not proof that a worker is running.

- **CUDA illegal memory access or cache thrashing:** the supervisor retries in
  fresh processes with backoff. Five failures without checkpoint progress stop
  the job. After investigating the cause, stop the service, back up
  `pipeline-status.json`, and reset its `failures` field to `0` before restarting.
  Do not delete the job or repeatedly reset a failure without diagnosing it.
- **Identity/checksum mismatch:** restore the expected source, voice, model, or
  runtime settings, or prepare a new job. Recovery does not bypass intact JSON
  records whose evidence no longer matches.
- **Unreadable checkpoint JSON:** startup preserves damaged records in the job's
  `recovery/` directory. It restores valid original evidence or regenerates
  affected passages without overwriting earlier audio. Inspect the log if this
  recovery itself fails.
- **Progress timeout:** workers stop after 15 minutes without a saved status
  update; packaging has a separate six-hour cap. Inspect the worker log before
  retrying; a timeout is not treated as a recognized CUDA retry.
- **Dashboard unavailable:** run
  `systemctl --user start voice-studio-ddia-progress` for the installed DDIA page.
  A new book's foreground dashboard needs its own running terminal command.

Service exit code `2` requires attention rather than an automatic restart. Diagnose the failure before resetting a restart limit with `systemctl --user reset-failed UNIT`. Replace `UNIT` with your service name.

## Tune worker throughput

Use `--render-batch-size` and `--check-batch-size` on `audiobook_pipeline.py`
to choose how many new passages each process handles. The defaults are 20 and
50 respectively. The supplied DDIA service template selects 50 for both.
Larger rendering batches spread model loading and checkpoint
validation over more audio, but wait longer before yielding the GPU to other
applications. Every completed passage is still saved immediately. If longer
workers encounter repeated CUDA errors, reduce the rendering batch to 20.

Checking workers reuse Whisper weights across passages, without using an earlier
passage's transcript as context. They release the model on completion or failure;
new workers start fresh. Standalone `python3 run.py audiobook check --job PATH --max-segments 50`
also limits new checks to 50. Omitting that limit checks all remaining rendered passages.
Model, voice, decoding settings, and checkpoint validation are unchanged by these
batch controls.

## Install a background job on Ubuntu

This example creates **new** units for the `my-book` job from the
[audiobook guide](audiobooks.md). It uses your repository's absolute path and
`.venv`, a distinct dashboard port (8766), and the same 2048 cache setting used
at preparation. Change all job/output paths consistently for a different book.
A foreground supervisor for this job must be stopped first. The DDIA units are
unaffected. These commands must run from the repository root as your normal user.

Create both units without overwriting existing files:

```sh
python3 - <<'PY'
from pathlib import Path
repo = Path.cwd().resolve()
if not (repo / 'src/voice_studio/audiobook_pipeline.py').is_file():
    raise SystemExit('Run from the Voice Studio repository root')
# Keep systemd specifier/quoting handling explicit rather than guessing escaping.
if any(ch in str(repo) for ch in ' %"\\\n'):
    raise SystemExit('This example requires a repository path without spaces or systemd metacharacters')
units = Path.home() / '.config/systemd/user'
units.mkdir(parents=True, exist_ok=True)
worker = units / 'voice-studio-my-book.service'
page = units / 'voice-studio-my-book-progress.service'
if worker.exists() or page.exists():
    raise SystemExit('Unit already exists; inspect it instead of overwriting')
for source, target in [
    ('voice-studio-ddia.service', worker),
    ('voice-studio-ddia-progress.service', page),
]:
    text = (repo / 'config/systemd' / source).read_text()
    text = text.replace('%h/repos/voice-studio', str(repo))
    text = text.replace('designing-data-intensive-applications-audio', 'my-book')
    text = text.replace('designing-data-intensive-applications-draft', 'my-book-draft')
    text = text.replace('DDIA', 'my-book').replace('--port 8765', '--port 8766')
    with target.open('x') as handle:
        handle.write(text)
print(worker, page, sep='\n')
PY
systemd-analyze --user verify \
  ~/.config/systemd/user/voice-studio-my-book.service \
  ~/.config/systemd/user/voice-studio-my-book-progress.service
systemctl --user daemon-reload
systemctl --user enable --now voice-studio-my-book voice-studio-my-book-progress
```

The service owns the job through `scheduled.lock`. It first validates checkpoints,
then either works or waits for GPU capacity. Check both the service and dashboard:

```sh
systemctl --user status voice-studio-my-book --no-pager
journalctl --user -u voice-studio-my-book -n 30 --no-pager
```

Open `http://127.0.0.1:8766/`. `active (running)` alone does not mean speech is
being generated. To start the user services before login and keep them available
after logout, enable lingering for your account:

```sh
loginctl enable-linger
loginctl show-user "$USER" -p Linger
```

Expect `Linger=yes`; local policy may require authentication. This changes your
user-manager lifecycle, not the computer's sleep policy. Keep the machine awake.
No reboot is necessary to start the job now.

### Stop, resume, and remove the service

`systemctl --user stop voice-studio-my-book` stops promptly and preserves saved
passages; start it with `systemctl --user start voice-studio-my-book`. To pause
between passages across restarts, run the following from the repository root:

```sh
python3 run.py audiobook pause --job data/books/jobs/my-book
```
 Stop the service
before removing that job's `paused` marker to explicitly resume. Do not launch a
second standalone renderer beside the service.

To remove only these example services, first disable and stop them:

```sh
systemctl --user disable --now voice-studio-my-book voice-studio-my-book-progress
rm ~/.config/systemd/user/voice-studio-my-book.service
rm ~/.config/systemd/user/voice-studio-my-book-progress.service
systemctl --user daemon-reload
```

This leaves the job, audio, and outputs intact. Do not disable user lingering if
other background services depend on it. The service retries unexpected supervisor
failures at 15-second intervals, limited to three starts in ten minutes. An
intentional stop or supervisor exit code 2 is not automatically restarted.
