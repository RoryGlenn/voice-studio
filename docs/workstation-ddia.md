# DDIA workstation runbook

This runbook is specific to the existing Ubuntu workstation. New installations should use [background job setup](operations.md#install-a-background-job-on-ubuntu).

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

For errors and retry limits, see [troubleshooting](operations.md#understand-progress-and-recover-from-a-stop).

## Worker throughput

The DDIA service uses 50-passage rendering and checking workers. Each passage
is checkpointed immediately. On this workstation, a 50-passage rendering trial
took 262 seconds, including 236 seconds of generation; all prior checkpoints
were unchanged. Recent 20-passage workers used about 20 seconds of overhead per
batch, compared with 27 seconds for the 50-passage trial. Passage lengths vary,
so this is an overhead comparison rather than a promised whole-book duration.

A warmed six-passage Whisper comparison (82 seconds of audio) took 4.94 seconds
with per-passage model loading and 2.58 seconds with reused weights. Transcripts
and word timings matched exactly in that sample. This measures recognition,
not total checking time, which also includes startup and checkpoint validation.
A full native 50-passage checking batch also completed successfully in 35.4 seconds,
including startup and checkpoint writes.
See [worker tuning](operations.md#tune-worker-throughput) for controls and tradeoffs.
