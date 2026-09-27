# Create an audiobook from an EPUB

Generate a chaptered draft from an unencrypted EPUB, then listen and resolve flagged passages before a verified export. Start with [installation](installation.md); the reusable pipeline requires the Natural engine and an installed Whisper model.

## Try the original sample

Before a full book, create a short, original two-chapter EPUB with cover art. The
example generator needs Pillow from the installed project environment and refuses
to overwrite an existing file:

```sh
.venv/bin/python docs/examples/make_sample_epub.py
```

Expected output: `data/books/incoming/library-garden.epub`. Use this path instead
of `/path/to/book.epub` in the preparation command below, and use a new job/output
name such as `library-garden`. The plan should contain two tracks, The Garden and
The Notebook. The full speech check still requires the local voice/model assets.

## Prepare and run a book

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
   unit names and this job's paths; follow [background job setup](operations.md#install-a-background-job-on-ubuntu).

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

## Choose what gets narrated

Preparation preserves source locations, paragraph and heading roles, an omission ledger, the original EPUB, voice reference and its optional provenance record, full voice settings, model-file hashes, and runtime identity. It follows the linear EPUB spine; navigation and non-linear resources are recorded as omissions. XHTML must be well-formed and resources unencrypted. A cover is read from EPUB 2/3 metadata or supplied with `--cover`; the original RGB artwork is preserved with an appended AUDIOBOOK band. The default recognizer is `<workspace>/work/whisper-small.en-mlx`; set `--asr-model` for a separately installed local model. `--workspace`, `--data-dir`, and `--voice` select private assets. This pipeline currently uses the Natural engine.

`--code include` retains standalone `<pre>` blocks. Choosing `--code omit` omits those blocks for this book and records their source text and hashes; surrounding explanations, inline code, and captions remain. Publisher-specific class names, complex tables, and unusual note conventions need source-plan review. Long paragraphs split into bounded passages while keeping each sentence dash with both anchor words and retaining paragraph identity. The command fails on unsupported spine resources or unbreakable tokens that exceed the passage limit. Review `plan.json` and its omission ledger before a full render.

Continue with [quality review and export](quality-review.md).
