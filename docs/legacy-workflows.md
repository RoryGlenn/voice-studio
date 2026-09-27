# Historical audiobook workflows

These instructions preserve existing saved editions. Use [EPUB audiobooks](audiobooks.md) for new books.

## Audiobook workflow

`python3 run.py book` dispatches the existing higher-precision edition wrapper. Its actions are `render`, `adjudicate`, `repair`, `audit`, `finalize`, and `verify`. Use `render --plan-only` to inspect the prepared plan without generating speech. `book-new-b` addresses the older 4-bit edition.

The historical edition has its own progress and verification commands:

```sh
python3 run.py progress
python3 run.py book verify
```

These commands do not report on the generic EPUB jobs or the DDIA service.

This workflow is specifically the existing 30-track, nine-folder *Thinking in Systems* recipe. It requires the externally prepared `work/narration_text/spoken_manifest.json`, `work/book_subsections.json`, source text paths, frozen voice profile, and model assets. It does not import arbitrary EPUBs or recreate the original chapter inventories. These inputs and historical jobs are data, not repository fixtures.

Run render, recognition, repair, and finalization stages sequentially. Studio and book inference share a user-level GPU lock; a competing process exits with a busy error. Generation models are released before recognition. Repair and adjudication helpers must not run concurrently with the renderer. Repairs retain source wording and their evidence; finalization accepts only verified checkpoints with matching identities and intact audio.

`precision_comparison.py` and `publish_precision_comparison.py` retain the historical A/B experiment. Its frozen source hash still refers to the original external Studio code. Historical checks deliberately reject changed inputs; do not rewrite fingerprints to force reuse. A new comparison needs separately prepared inputs and its own job identity.

## Older launchers and extracted installations

Old and extracted launchers share the instance lock when configured with the same
Studio data directory. Launching reopens the existing instance. To switch code
installations, finish any active render, quit Studio, and launch the intended
checkout. Keep existing media and completed books; do not overwrite their
configuration with fresh-install examples.
