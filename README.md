# Voice Studio

Local saved-voice narration with a browser interface, MP3/WAV export, and resumable EPUB-to-audiobook jobs on Apple Silicon macOS and NVIDIA Ubuntu. The historical *Thinking in Systems* workflow remains available.

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

Use Python 3.12, uv, FFmpeg/ffprobe, and Node for browser tests. Native inference uses MLX Metal on Apple Silicon or MLX CUDA 12 on Linux x86-64 with a supported NVIDIA GPU. CPU tests do not require a voice model or a book.

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

## Ubuntu installation

Use Python 3.12, `uv`, FFmpeg, and an NVIDIA driver compatible with CUDA 12. The tested target is Ubuntu 24.04 with an RTX 3060 Ti (8 GiB VRAM). Install the OS encoder utilities, then the locked Python profile:

```sh
sudo apt-get update
sudo apt-get install ffmpeg
uv sync --locked --extra ubuntu
cp local.example.toml local.toml
```

Configure the external workspace and Studio profile directory in `local.toml`, copy your private voice profiles and reference audio with checksum verification, and install the exact model snapshots named by those profiles. Do not copy the Mac Python environment. The `ubuntu` extra includes the CUDA runtime, NVCC header, and CCCL header packages needed by MLX kernel compilation.

The offline Hugging Face cache belongs at `<workspace>/work/hf-narration-cache`. It must include `mlx-community/S3TokenizerV2` revision `e0c9886f0e1c35ae85b1f27277416fb19fc72bec`, whose `model.safetensors` SHA-256 is `928726bc1f206a613d36b8f49e297eae9c5593a21bf9b92ddfe2c23f85eb92cc`. Its cached `refs/main` must point to that revision because the pinned upstream loader requests `main`. Missing or failed tokenizer initialization stops rendering.

```sh
python3 run.py doctor
./launch-voice-studio.sh
# For an SSH-only session:
./launch-voice-studio.sh --no-browser
```

`doctor` checks actual GPU kernel compilation, encoder tools, reference checksums, primary model weights, text-tokenizer loading, and the pinned offline speech tokenizer. `doctor --kernel-only` checks the runtime without claiming voice readiness. Model loading and conditioning are additionally checked before actual generation. The browser server retains loopback binding and session-token authentication; remote access should use SSH port forwarding, with the printed token-bearing launch URL.

Linux defaults disable CUDA graphs, set the MLX cache limit to zero, use a 4096 MiB **soft** memory guideline, and set the convolution cache to 512. These preserve model precision; they do not promise a hard VRAM ceiling. The convolution setting also supports the saved Expressive engine on the tested GPU. Optional `[runtime]` values in `local.toml` are `backend = "cuda"` or `"metal"`, and `memory_mib = 4096`; `VOICE_STUDIO_BACKEND` and `VOICE_STUDIO_MEMORY_MIB` override them. An unavailable selected GPU fails explicitly.

The shared GPU lock defaults to `~/.local/state/voice-studio/gpu.lock` (or `$XDG_STATE_HOME/voice-studio/gpu.lock`). `VOICE_STUDIO_GPU_LOCK` may select one common lock path for installations sharing a GPU. Do not give competing installations different lock paths. Models and recognizer caches are released before relinquishing ownership, including ordinary Python exception paths. A native-process abort is recovered by the OS releasing the lock; rerun the interrupted stage to resume its saved checkpoints.

## Reusable EPUB audiobook jobs

`audiobook` creates a new job for each book. Existing historical jobs remain governed by their original edition commands and fingerprints; this command does not rewrite their identities or resume a book paused in another workspace.

```sh
python3 run.py audiobook prepare --epub /path/to/book.epub --job /path/to/new-job --code include
python3 run.py audiobook status --job /path/to/new-job
python3 run.py audiobook render --job /path/to/new-job --max-segments 10
python3 run.py audiobook check --job /path/to/new-job
python3 run.py audiobook review --job /path/to/new-job
```

Preparation preserves source locations, paragraph and heading roles, an omission ledger, the original EPUB, voice reference and its optional provenance record, full voice settings, model-file hashes, and runtime identity. It follows the linear EPUB spine; navigation and non-linear resources are recorded as omissions. XHTML must be well-formed and resources unencrypted. A cover is read from EPUB 2/3 metadata or supplied with `--cover`; the original RGB artwork is preserved with an appended AUDIOBOOK band. The default recognizer is `<workspace>/work/whisper-small.en-mlx`; set `--asr-model` for a separately installed local model. `--workspace`, `--data-dir`, and `--voice` select private assets. This pipeline currently uses the Natural engine.

`--code include` retains standalone `<pre>` blocks. Choosing `--code omit` omits those blocks for this book and records their source text and hashes; surrounding explanations, inline code, and captions remain. Publisher-specific class names, complex tables, and unusual note conventions need source-plan review. Long paragraphs split into bounded passages while keeping each sentence dash with both anchor words and retaining paragraph identity. The command fails on unsupported spine resources or unbreakable tokens that exceed the passage limit. Review `plan.json` and its omission ledger before a full render.

`render` generates pending passages only. `check` performs unprompted recognition after synthesis models have been released, preserves raw ASR, and holds wording, signal, or timing uncertainty. A held passage stays held during ordinary resume. To request a new seed for one held passage:

```sh
python3 run.py audiobook repair --job /path/to/new-job --segment 1/3 --seed-offset 1000
```

Repairs retain prior raw WAVs, recognition, checkpoints, and attempt evidence. For an ASR mistake confirmed by listening, save the actually heard transcript in a UTF-8 file and bind a deliberate review to that exact recording:

```sh
python3 run.py audiobook review --job /path/to/new-job --segment 1/3 --transcript /path/to/heard.txt --note "Listening confirms the source wording; recognition misheard the name."
```

Review does not bypass signal or dash-timing checks. Raw transcripts remain unchanged. Avoid approving a passage solely because the expected text is known.

```sh
python3 run.py audiobook pause --job /path/to/new-job
python3 run.py audiobook resume --job /path/to/new-job
python3 run.py audiobook check --job /path/to/new-job
python3 run.py audiobook finish --job /path/to/new-job --output /path/to/audiobook-output
python3 run.py audiobook verify --job /path/to/new-job
```

Pause takes effect between passages. Resume renders remaining passages; run `check` afterward. Each mutating stage holds the job lock; `status` remains readable while a worker runs. Changing the source, voice, model, runtime, or frozen plan rejects checkpoint reuse and requires a new job. Original attempt evidence survives an interruption before its current checkpoint is written.

Finalization requires every passage to pass wording, signal, and timing checks. Total quiet targets are 0.85 seconds after headings, 0.70 between paragraphs, 1.00 before headings, and 0.35 at sentence dashes. Existing quiet counts toward those targets; the pipeline inserts silence without deleting audio. Dash insertion additionally requires adjacent recognized words, sufficient timing confidence, and an existing quiet gap. Native pauses longer than the target are retained. Chunks within one paragraph receive no extra paragraph pause.

Choose an empty output directory; finalization refuses to overwrite a directory owned by another job or containing unrelated files. Temporary media is created beside its destination, so output may reside on a separate drive. Outputs are ordered 24 kHz PCM24 WAV chapters, 128 kbps MP3 chapters, and `audiobook.m4b` with AAC-LC audio, QuickTime chapters, original cover plus AUDIOBOOK strip, and audiobook metadata. Final checks include passage-to-chapter waveform conservation, chapters and ordering, cover and metadata readback, duration, and full audio decoding. `verification.json` records the evidence. Automated recognition is a gate, not a substitute for listening to the final book.

## Audiobook workflow

`python3 run.py book` dispatches the existing higher-precision edition wrapper. Its actions are `render`, `adjudicate`, `repair`, `audit`, `finalize`, and `verify`. Use `render --plan-only` to inspect the prepared plan without generating speech. `book-new-b` addresses the older 4-bit edition.

This workflow is specifically the existing 30-track, nine-folder *Thinking in Systems* recipe. It requires the externally prepared `work/narration_text/spoken_manifest.json`, `work/book_subsections.json`, source text paths, frozen voice profile, and model assets. It does not import arbitrary EPUBs or recreate the original chapter inventories. These inputs and historical jobs are data, not repository fixtures.

Run render, recognition, repair, and finalization stages sequentially. Studio and book inference share a user-level GPU lock; a competing process exits with a busy error. Generation models are released before recognition. Repair and adjudication helpers must not run concurrently with the renderer. Repairs retain source wording and their evidence; finalization accepts only verified checkpoints with matching identities and intact audio.

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
uvx mypy --python-executable .venv/bin/python studio.py voice_profiles.py project_paths.py run.py runtime.py doctor.py audiobook.py book_prepare.py book_pacing.py book_package.py
```

Tests use fake tone generation and real audio encoding. They establish plumbing and verification behavior; automated recognition does not guarantee flawless pronunciation or replace listening review. No production speech generation is triggered by the default tests.
