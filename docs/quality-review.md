# Review narration quality and export

Use this workflow after checking an EPUB job. It separates listening review from
recognition errors and keeps original evidence. Start with a prepared job and the
same runtime/model settings used to render it. Stop its supervisor or wait until
it finishes; do not run repairs beside an active worker.

## Understand the passage states

| State | What it establishes | Next action |
| --- | --- | --- |
| `pending` | No saved current audio record | Render or resume the supervisor. |
| `rendered` | Audio exists, not yet checked | Run `check` or let the supervisor continue. |
| `needs_review` | Wording, signal, or timing remains uncertain | Listen and inspect evidence. |
| `verified` | Automated checks, or a bound listening review plus other checks, passed | Include in a verified export; still listen to the result. |

A draft uses raw audio for held passages and records their locations in
`draft_report.json`. It does not silently approve them. An M4B marked Draft is a
convenient listening copy, not a guarantee of accuracy.

## Inspect a flagged passage

For the example `my-book` job, run from the repository root. On Ubuntu, restore
`VOICE_STUDIO_CUDA_CONV_CACHE_SIZE=2048` if that is how you prepared this job.

```sh
python3 run.py audiobook review --job data/books/jobs/my-book
```

The report lists held passages and evidence. `1/3` means track 1, passage 3; its
current checkpoint is `segments/0001/0003.json` under the job. Inspect the
checkpoint's source text, `raw_file`, wording differences, signal metrics, and
timing error. Play the file identified by `raw_file` in your audio player.

Decide from the audio, not from what you expect it to say:

- Missing, repeated, or wrong speech: regenerate the passage.
- Accurate speech but a recognition mistake: supply a truthful listening transcript.
- Clipping, silence, or unresolved timing: investigate or regenerate; a transcript
  does not override these checks.

## Regenerate one held passage

Replace `1/3` with an actually held passage:

```sh
python3 run.py audiobook repair \
  --job data/books/jobs/my-book --segment 1/3 --seed-offset 1000
```

The positive seed offset requests a different generation. Repair preserves the
old attempt and checks the new one. Run `review` again and listen to the result.
Repeatedly changing seeds is not a substitute for diagnosing a consistently bad
source extraction or voice reference. A changed source/voice requires a new job.

## Record a confirmed recognition mistake

Write the words you actually heard to a UTF-8 file, for example
`data/books/jobs/my-book/heard-1-3.txt`. Do not copy the source as a presumed
transcript. Bind the review to that exact recording:

```sh
python3 run.py audiobook review \
  --job data/books/jobs/my-book --segment 1/3 \
  --transcript data/books/jobs/my-book/heard-1-3.txt \
  --note "Listening confirms the source wording; recognition misheard the name."
```

Replace the note with your real finding. The stored review hashes the evidence;
raw recognition output is retained. Signal and dash-timing checks still apply.

## Choose a draft or verified export

All passages must be checked before a draft can be packaged. Use a new output
directory, or one already owned by this exact job; unrelated contents are rejected.

```sh
.venv/bin/python draft_audiobook.py \
  --job data/books/jobs/my-book --output data/outputs/my-book-draft
```

For a verified export, resolve all held passages first:

```sh
python3 run.py audiobook finish \
  --job data/books/jobs/my-book --output data/outputs/my-book-verified
python3 run.py audiobook verify --job data/books/jobs/my-book
```

`finish` rejects unresolved passages. `verify` checks the saved final export's
fingerprints and decoding; it does not repair or approve passages. Use the same
Python environment as the job for the direct draft command.

Both exports provide ordered WAV/MP3 chapters, `audiobook.m4b`, cover art, chapter
metadata, and a playlist. The verified package records checks in
`verification.json`; the job stores `final_verification.json`. The draft records
its limitations in `draft_report.json`. Play the beginning, transitions, names,
and flagged areas, then listen to the book as needed.

Verified pacing inserts silence without removing original audio. Quiet targets
are 0.85 seconds after headings, 0.70 between paragraphs, 1.00 before headings,
and 0.35 at sentence dashes. Existing quiet counts toward the target; longer
pauses remain. Dash insertion requires matching words, timing confidence, and an
existing quiet gap. Chunks within a paragraph receive no extra paragraph pause.
