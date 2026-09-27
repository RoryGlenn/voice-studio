# Develop and validate Voice Studio

Changes should preserve local inference, immutable source/voice identities, original attempt evidence, and exclusive GPU ownership.

## Architecture and invariants

The browser submits text to `studio.py`, which validates it and serializes voice
conditioning/generation through a worker. `runtime.py` selects the GPU, enforces
local model use, and owns the shared cross-process GPU lease. Browser sessions
and host/origin checks protect the local API; the separate progress server is
read-only and loopback-bound.

For books, `book_prepare.py` freezes an EPUB plan; `audiobook.py` generates,
checks, and repairs passages; `book_package.py` exports verified audio.
`draft_audiobook.py` explicitly retains held status while producing a listening
copy. `audiobook_pipeline.py` launches disposable workers and records their
health. Native CUDA aborts cannot be recovered by catching a Python exception in
the poisoned worker, so recovery starts a fresh process.

A passage checkpoint links source text, voice/runtime identity, seed, raw audio,
and recognition evidence by hashes. Preserve originals when repairing. Durable
writes flush files before replacing checkpoint JSON; mutating stages own the job
lock. Never edit fingerprints to make incompatible data appear resumable.

## Repository contents

- `studio.py`, `web/`: local API, serialized render worker, and browser UI.
- `render_book.py` and helper scripts: resume, recognition checks, repair, assembly, and output verification.
- `higher_precision_book.py`, `new_b_book.py`: explicit saved-edition configuration and provenance.
- `project_paths.py`, `run.py`: portable source/data separation and runtime selection.
- `test_*.py`, `test_web.cjs`: synthetic fixtures, real FFmpeg export checks, API boundaries, and browser session recovery.

Recordings, books, generated audio, model weights, caches, credentials, and `local.toml` are ignored by Git. Public examples contain no personal voice recordings or book text. Keep new experiment data under an ignored runtime directory.

## Set up a model-free development environment

Use Python 3.12, uv, FFmpeg/ffprobe, and Node. From a fresh checkout:

```sh
uv sync --locked
```

The test suite uses synthetic voices and fake generation, with real audio codecs.
It does not download voice models or prove native GPU quality. Use platform extras
from [installation](installation.md) when testing actual inference. Avoid rerunning
`uv sync` without an extra over an inference environment you intend to preserve.

## Development checks

```sh
uv run python -m unittest discover -p 'test_*.py'
node --test test_web.cjs
uvx ruff check .
uvx ruff format --check .
uvx mypy --python-executable .venv/bin/python studio.py voice_profiles.py project_paths.py run.py runtime.py doctor.py audiobook.py book_prepare.py book_pacing.py book_package.py
```

Tests use fake tone generation and real audio encoding. They establish plumbing and verification behavior; automated recognition does not guarantee flawless pronunciation or replace listening review. No production speech generation is triggered by the default tests.

## Documentation changes

Use the Doc Agent workflow when creating or revising documentation. Inspect the
implementation and existing tests before changing behavioral claims. Keep one
canonical page for each subject, preserve old links with routing sections, and
separate general procedures from workstation/legacy runbooks. Do not publish
private recordings, books, configuration, session tokens, or local model files.

Validate example syntax, CLI options, local links/anchors, and observable outcomes.
Use the original sample EPUB for safe checks. Record whether validation used real
speech, synthetic tones, or a clean installation in [validation](validation.md).
A passing syntax check is not proof that a user can complete a native render.
