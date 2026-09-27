# Develop and validate Voice Studio

Changes should preserve local inference, immutable source/voice identities, original attempt evidence, and exclusive GPU ownership.

## Architecture and invariants

The browser submits text to `studio.py`, which validates it and serializes voice
conditioning/generation through a worker. `runtime.py` selects the GPU, enforces
local model use, and owns the shared cross-process GPU lease. Browser sessions
and host/origin checks protect the local API. The separate audiobook workspace
is loopback-bound; its controls require a page token and job locks.

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

- `src/voice_studio/`: application modules, GPU runtime, audiobook pipeline, and workspace HTTP API.
- `web/studio/`: text-to-speech studio frontend.
- `web/audiobook/`: audiobook overview, listening/review workspace, and system monitor.
- `tests/`: Python fixtures/integration tests and JavaScript UI tests.
- `tools/legacy/`: saved-edition workflows and historical book experiments. These remain available through `run.py book`, `book-new-b`, and `progress`.
- `config/systemd/`: background-service templates; `docs/`: operating instructions.
- `run.py`: stable launcher that selects the interpreter and sets checkout import paths.
- Root `audiobook.py`, `audiobook_pipeline.py`, `draft_audiobook.py`, and `live_book_progress.py`: small compatibility entrypoints for existing services/job scripts. New commands should use `run.py`.

Python modules import through `voice_studio`; the launcher adds `src/` to the child process import path. Private job paths are unchanged by source moves.

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
python3 run.py test
node --test tests/test_web.cjs
node tests/test_live_book_progress.cjs
uvx ruff check .
uvx ruff format --check .
uvx mypy --python-executable .venv/bin/python src/voice_studio/studio.py src/voice_studio/voice_profiles.py src/voice_studio/project_paths.py run.py src/voice_studio/runtime.py src/voice_studio/doctor.py src/voice_studio/audiobook.py src/voice_studio/book_prepare.py src/voice_studio/book_pacing.py src/voice_studio/book_package.py
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
