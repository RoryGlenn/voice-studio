# Voice Studio

Local saved-voice narration with a browser interface, MP3/WAV export, and the existing verified *Thinking in Systems* audiobook workflow.

The website accepts text, previews or renders it, and exposes speed, paragraph pauses, and engine-specific expression controls. One worker owns model loading and generation. Natural uses the approved higher-precision checkpoint; its repository is named `chatterbox-turbo-fp16`, although the inspected weights are F32.

New WAV downloads preserve the model's native 24 kHz audio as uncompressed 24-bit PCM. MP3 remains available for smaller files. Extra export bit depth preserves precision; it does not create higher-frequency detail or guarantee more natural synthesis.

## Existing installation

The ignored `local.toml` selects your existing Python environment, voice profile, model cache, and audiobook workspace. Media stays in its current location. From this repository:

```sh
python3 run.py studio
python3 run.py studio --port 53648 --no-browser
python3 run.py progress
python3 run.py book verify
```

On macOS, double-click `Launch Voice Studio.command` to open the website. Its launch URL contains the current local session token; use that URL when opening another browser. The token is stored only in the private runtime state. The server binds to `127.0.0.1` and is started on demand.

The old and extracted launchers use the same instance lock when configured with the same Studio data directory. If the existing server is already running, launching here reopens that instance. To switch to the extracted server, finish any active render and quit the existing server first. The existing installation and completed audiobooks are preserved.

## Narrator selection

The Narrator menu selects a saved voice independently of its voice engine. The original `voice_profile.json` remains the default, with the stable ID `default`. Optional profiles live in the private Studio data directory at `voices/<voice-id>.json`; IDs use lowercase letters, digits, underscores, or hyphens. Restart the app after adding a profile. Every profile's reference path is relative to the Studio data directory, even when its JSON is inside `voices/`.

`config/deep-narrator.example.json` is an optional synthetic-voice profile template, not a bundled or enabled narrator. Copy the template into your private `voices/` directory under your chosen voice ID, then supply the reference audio, its actual SHA-256, and the model path. The template defines one Natural engine; list only the engines you want available for that voice. A missing reference or model disables the corresponding voice/engine.

The Narrator menu reflects the profiles in your local data directory. To remove an optional narrator, move its profile out of `voices/` and restart the app. Existing recordings retain the voice identity saved when they were generated. Reference audio, local profiles, models, and generation evidence stay outside the source repository.

Profiles may set `default_engine` and `default_expression`. Selecting a voice applies these settings; API requests that omit them use the same defaults. Explicit request settings still take precedence, and reconnecting the browser preserves its current manual selection. An unavailable configured default disables rendering until the user chooses an available engine; the app does not silently substitute another model. Profiles without these fields retain Natural and expression 0.5 (or their sole defined engine).

API clients can send `voice_id` in a render request. Omitting it preserves the original default. `/api/config` keeps its legacy default `voice`/`engines` fields and adds `default_voice_id` and a `voices` map. Jobs freeze the selected name, reference hash, model, and synthesis settings at submission. Recording labels and MP3 metadata come from that saved identity. Changing the selected speaker or conditioning parameters reloads conditioning inside the one render worker; cached audio conditioning is never shared across different voice identities.

## Clean environment

Use Python 3.12, uv, FFmpeg/ffprobe, and Node for browser tests. The inference backend requires an Apple Silicon Mac; CPU tests do not require a voice model or a book.

```sh
uv sync --locked
uv run python -m unittest discover -p 'test_*.py'
node --test test_web.cjs
```

To install native inference dependencies on Apple Silicon:

```sh
uv sync --locked --extra native
```

Copy `local.example.toml` to `local.toml`, choose an external model/workspace root and a private Studio data directory, and put a `voice_profile.json` there using `config/voice_profile.example.json` as the schema. Supply your own reference WAV and replace the example's zero SHA-256 with its actual hash. Profile reference paths are relative to the Studio data directory; model paths are relative to the workspace root. Absolute paths are also supported.

Model files and tokenizer/cache assets must be installed separately before using the offline inference backend. `uv.lock` pins Python packages; the profile template pins model repositories/revisions, and the higher-precision book wrapper checks its model file hashes. The current extraction reuses an already prepared native installation; it is not an automated model or voice enrollment installer.

Configuration precedence is environment variable, then local TOML, then an ignored `data/` directory:

| TOML key | Environment override | Purpose |
| --- | --- | --- |
| `workspace_root` | `VOICE_STUDIO_DATA_ROOT` | Models, tokenizer cache, book manifests, and jobs |
| `studio_data_dir` | `VOICE_STUDIO_PROFILE_DIR` | Saved profile/reference, recordings, and server state |
| `python` | `VOICE_STUDIO_PYTHON` | Python used by `run.py` and the macOS launcher |

`VOICE_STUDIO_CONFIG` selects an alternative TOML file. Relative paths resolve beside that configuration file. `studio.py` also accepts `--data-dir` and `--workspace` for explicit runtime locations.

## Audiobook workflow

`python3 run.py book` dispatches the existing higher-precision edition wrapper. Its actions are `render`, `adjudicate`, `repair`, `audit`, `finalize`, and `verify`. Use `render --plan-only` to inspect the prepared plan without generating speech. `book-new-b` addresses the older 4-bit edition.

This workflow is specifically the existing 30-track, nine-folder *Thinking in Systems* recipe. It requires the externally prepared `work/narration_text/spoken_manifest.json`, `work/book_subsections.json`, source text paths, frozen voice profile, and model assets. It does not import arbitrary EPUBs or recreate the original chapter inventories. These inputs and historical jobs are data, not repository fixtures.

Run render, recognition, repair, and finalization stages sequentially. Do not generate in Studio during a book generation/recognition job: the app and book tools do not share a global GPU lock. Repair and adjudication helpers must not run concurrently with the renderer. Repairs retain source wording and their evidence; finalization accepts only verified checkpoints with matching identities and intact audio.

`precision_comparison.py` and `publish_precision_comparison.py` retain the historical A/B experiment. Its frozen source hash still refers to the original external Studio code. Historical checks deliberately reject changed inputs; do not rewrite fingerprints to force reuse. A new comparison needs separately prepared inputs and its own job identity.

## Repository contents

- `studio.py`, `web/`: local API, serialized render worker, and browser UI.
- `render_book.py` and helper scripts: resume, recognition checks, repair, assembly, and output verification.
- `higher_precision_book.py`, `new_b_book.py`: explicit saved-edition configuration and provenance.
- `project_paths.py`, `run.py`: portable source/data separation and runtime selection.
- `test_*.py`, `test_web.cjs`: synthetic fixtures, real FFmpeg export checks, API boundaries, and browser session recovery.

Recordings, books, generated audio, model weights, caches, credentials, and `local.toml` are ignored by Git. Public examples contain no personal voice recordings or book text. Keep new experiment data under an ignored runtime directory.

## Development checks

```sh
uv run python -m unittest discover -p 'test_*.py'
node --test test_web.cjs
uvx ruff check .
uvx ruff format --check .
uvx mypy --python-executable .venv/bin/python studio.py voice_profiles.py project_paths.py run.py
```

Tests use fake tone generation and real audio encoding. They establish plumbing and verification behavior; automated recognition does not guarantee flawless pronunciation or replace listening review. No production speech generation is triggered by the default tests.
