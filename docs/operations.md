# Run and recover background audiobook jobs

Use a foreground supervisor for short runs or a persistent user service for unattended work. This page describes general operation; exact commands for the existing book are in the [DDIA runbook](workstation-ddia.md).

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
if not (repo / 'audiobook_pipeline.py').is_file():
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
