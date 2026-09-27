# Run narration yourself

Use Voice Studio to turn text into speech or an EPUB into a chaptered audiobook.
You do not need Codex or an AI chat session to keep narration running. This guide
covers an existing installation, with an Ubuntu NVIDIA example for whole books.
For a new machine, complete the [installation instructions](../README.md#ubuntu-installation)
first, including the local model files and voice reference.

Choose your task:

- [Narrate pasted text](#narrate-pasted-text) in the browser.
- [Control the configured DDIA audiobook](#control-the-configured-ddia-audiobook) on the existing Ubuntu workstation.
- [Start another EPUB](#start-another-epub) from the terminal.
- [Understand progress and recover from a stop](#understand-progress-and-recover-from-a-stop).

## What runs on your computer

The Natural engine uses the Chatterbox Turbo speech model to generate narration
from text and a saved voice reference. For audiobook jobs, Whisper transcribes
that audio so the software can compare the spoken words with the source. Python
coordinates these stages, saves completed passages, and packages the audio.
Speech generation and recognition use locally installed models; book text and
voice recordings are not uploaded by this workflow.

A completed **draft** can contain passages flagged for review. It is ready to
listen to, but it is not a claim that every word or pronunciation is correct.
The separate verified export requires all passages to pass its checks; even that
needs listening review. See [review and verified export](../README.md#reusable-epub-audiobook-jobs).

## Narrate pasted text

Start with the configured Python environment, models, and saved voice profile.
On the existing Ubuntu workstation, open a terminal and run:

```sh
cd ~/repos/voice-studio
./launch-voice-studio.sh
```

On macOS, use `Launch Voice Studio.command`. If the browser does not open, use
the local URL printed by the launcher, including its session token. Keep that
URL private. Do not substitute the audiobook dashboard URL: they are different
interfaces.

1. Paste your text into **The script**, or choose **Import .txt**.
2. Select a **Narrator** and an available voice engine.
3. Choose **Preview first 120 words** and listen before starting a long render.
4. Adjust the available voice, speed, and pause controls as needed, then choose
   **Generate audio**.
5. Play the completed recording and download WAV or MP3. WAV preserves
   uncompressed audio; MP3 uses less storage.

Use **Cancel render** to stop a browser render, or **Quit studio** when finished.
Studio and audiobook workers share a GPU lock. If another narration worker owns
it, wait for that worker or stop its service before generating in Studio.

## Control the configured DDIA audiobook

This section applies only to the already prepared *Designing Data-Intensive
Applications* job and installed `voice-studio-ddia` services. It does not prepare
a new book. The templates assume the repository lives at `~/repos/voice-studio`.

Open the [local progress dashboard](http://127.0.0.1:8765/). To start or resume the
background job, run this from any directory:

```sh
systemctl --user start voice-studio-ddia
```

The service validates saved checkpoints, generates missing passages, checks the
speech, and packages the draft. You can close the terminal. With this service
enabled and user lingering configured, it starts again after a reboot. Keep the
computer awake while you want work to advance; suspended or powered-off machines
cannot render.

Check the service independently of the dashboard:

```sh
systemctl --user status voice-studio-ddia --no-pager
```

`active (running)` means the supervisor is alive; it may be waiting for the GPU.
The dashboard tells you which stage it is in.

### Stop or pause

To stop promptly while retaining completed passages:

```sh
systemctl --user stop voice-studio-ddia
```

The interrupted passage may need regeneration. Start the service again to
continue. A manual service stop does not disable startup after a future reboot.

To pause between passages and keep the job paused across service starts or
reboots, use a pause marker instead:

```sh
cd ~/repos/voice-studio
python3 run.py audiobook pause \
  --job data/books/jobs/designing-data-intensive-applications-audio
```

Wait for the dashboard to report paused. To explicitly resume a marker-paused job,
stop the supervisor first, remove only that marker, then start it:

```sh
systemctl --user stop voice-studio-ddia
rm -f ~/repos/voice-studio/data/books/jobs/designing-data-intensive-applications-audio/paused
systemctl --user start voice-studio-ddia
```

Do not run the standalone `audiobook resume` command alongside the service;
it starts a separate renderer rather than controlling the supervisor.

### Find the result

When the dashboard says **Draft audiobook is ready**, open:

```text
~/repos/voice-studio/data/outputs/designing-data-intensive-applications-draft/audiobook.m4b
```

The same directory contains chapter audio, `Listen in order.m3u8`, and
`draft_report.json`. The report identifies held passages that need listening
review. Narration reaching 100% alone does not mean packaging has finished.

## Start another EPUB

These commands target the configured Ubuntu NVIDIA installation. Run them from
`~/repos/voice-studio`. You need an unencrypted, supported EPUB, the installed
speech and Whisper models, a saved voice, and space for passage audio and exports.
The supervisor uses the interpreter you launch it with; the examples use the
repository's `.venv`. If your `local.toml` selects a different environment, use
that environment's Python for the supervisor too.

1. Choose new job and output directories. Replace the EPUB path below. In the
   same terminal, set the runtime options before preparing the book:

   ```sh
   cd ~/repos/voice-studio
   export VOICE_STUDIO_CUDA_CONV_CACHE_SIZE=2048
   export VOICE_STUDIO_MIN_FREE_GPU_MIB=6000
   python3 run.py audiobook prepare \
     --epub "/path/to/book.epub" \
     --job "data/books/jobs/my-book" \
     --code include
   ```

   `--code include` retains standalone code blocks. Use `--code omit` if you want
   those blocks excluded and recorded in the omission ledger. This is not a
   general option for removing a book's index. Choose a different job directory
   for each book or preparation policy.

2. Inspect `data/books/jobs/my-book/plan.json`. Check the source order, included
   text, and recorded omissions before committing to the full render. Preparation
   freezes the source, voice, model, and runtime identity. Use the same runtime
   settings when resuming; do not edit hashes or the plan to bypass a mismatch.

3. Start the supervisor in that terminal:

   ```sh
   .venv/bin/python audiobook_pipeline.py \
     --job "data/books/jobs/my-book" \
     --output "data/outputs/my-book-draft"
   ```

   Leave the terminal open. This command validates checkpoints, renders in
   20-passage batches, checks generated speech, and builds the draft. It does not
   install a background service for the new book. To run unattended across
   logouts/reboots, adapt the [service templates](../config/systemd/) with distinct
   unit names and this job's paths; see the [service reference](../README.md#supervised-audiobook-recovery).

4. In a second terminal, inspect saved counts without interrupting the worker:

   ```sh
   cd ~/repos/voice-studio
   python3 run.py audiobook status --job data/books/jobs/my-book
   ```

   For an updating dashboard, run the following in that second terminal and open
   `http://127.0.0.1:8766/`. Port 8766 avoids the installed DDIA dashboard on 8765.

   ```sh
   python3 live_book_progress.py \
     --job data/books/jobs/my-book \
     --output data/outputs/my-book-draft \
     --port 8766
   ```

5. Wait for **Draft audiobook is ready**, then play
   `data/outputs/my-book-draft/audiobook.m4b` and inspect `draft_report.json`.
   Use a new output directory initially; the exporter rejects unrelated contents.

To stop the foreground supervisor, press Ctrl+C in its terminal. Run the same
supervisor command again to continue from saved checkpoints. If you open a new
terminal, restore the two environment variables from step 1 first. Do not run
`prepare` again on the existing job.

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

Service exit code `2` requires attention rather than an automatic service
restart. If systemd reports its restart limit was reached, diagnose the failure,
then use `systemctl --user reset-failed voice-studio-ddia` before starting again.
