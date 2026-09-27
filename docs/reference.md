# Configuration and command reference

Use this page for exact behavior after [installation](installation.md). Commands
are relative to the repository root. `python3 run.py` selects the configured Python;
direct scripts use the interpreter you supply, such as `.venv/bin/python`.

## Paths and configuration precedence

For path settings, precedence is environment override, then `local.toml`, then
the default. `VOICE_STUDIO_CONFIG` selects another TOML file. Relative configured
paths resolve beside that file, not beside whichever terminal directory you use.

| TOML key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `workspace_root` | `VOICE_STUDIO_DATA_ROOT` | repository `data/` | Models, job data, and outputs |
| `studio_data_dir` | `VOICE_STUDIO_PROFILE_DIR` | workspace `studio/` | Profiles, references, and browser recordings |
| `python` | `VOICE_STUDIO_PYTHON` | repository `.venv/bin/python` | Interpreter dispatched by `run.py` |

The launch scripts also support an interpreter override. `studio.py` accepts
`--data-dir` and `--workspace`; EPUB preparation accepts the corresponding flags.
Reference paths in voice profiles resolve from the Studio data directory; model
paths resolve from the workspace. Absolute paths are accepted.

## GPU and runtime settings

Environment values override the `[runtime]` TOML section.

| TOML key | Environment variable | Default | Constraint/effect |
| --- | --- | --- | --- |
| `backend` | `VOICE_STUDIO_BACKEND` | `cuda` on Linux, `metal` otherwise | Selected GPU must be available; no CPU fallback. |
| `memory_mib` | `VOICE_STUDIO_MEMORY_MIB` | `4096` | Positive integer; soft MLX CUDA memory guideline, not a hard ceiling. |
| `cuda_conv_cache_size` | `VOICE_STUDIO_CUDA_CONV_CACHE_SIZE` | `512` | Positive integer convolution-cache setting, not MiB. The configured long-book job uses 2048. |

CUDA graphs are disabled and the CUDA MLX cache limit is zero. These controls do
not guarantee immunity from CUDA faults. Runtime identity includes platform,
Python and package versions, and runtime settings. A changed identity rejects
checkpoint reuse. Keep frozen settings when resuming; a different model or runtime
requires a new validated job rather than editing identity hashes.

`VOICE_STUDIO_GPU_LOCK` overrides the shared GPU-lock path. Otherwise it is
`$XDG_STATE_HOME/voice-studio/gpu.lock`, with `~/.local/state` as the fallback.
All installations sharing the GPU should use the same lock. A competing worker
fails with a busy error instead of silently running concurrently.

`VOICE_STUDIO_MIN_FREE_GPU_MIB` is a **supervisor admission setting**, not a TOML
runtime field. Default `0` disables admission polling. A positive value requires
that much free GPU-0 memory and utilization at most 35% before rendering/checking.
The service template sets 6000 MiB. Admission does not reserve the GPU or evict apps.
Do not set the NVIDIA-specific check for the macOS Metal workflow.

## Voice profiles

The default profile is `STUDIO_DATA/voice_profile.json`, ID `default`. Additional
profiles are `STUDIO_DATA/voices/<id>.json`. IDs match
`[a-z0-9][a-z0-9_-]{0,63}` and must be unique, including `default`. Restart Studio
after adding/removing a profile. Existing jobs retain their frozen voice identity.

| Field | Meaning |
| --- | --- |
| `name` | Nonempty display/narrator name |
| `reference` | Nonempty reference-audio path relative to Studio data |
| `reference_sha256` | 64 lowercase hexadecimal SHA-256 characters; must match the recording |
| `reference_provenance` | Optional private record path; if configured for a book it must exist |
| `engines` | Nonempty mapping of supported `natural`/`expressive` engines, each with `path` and `label` |
| `synthesis` | Engine parameters; start from the supplied example rather than inventing fields |
| `default_engine` | Must be defined in `engines`; otherwise Natural if present, else the first engine |
| `default_expression` | Finite number from 0 to 1, default 0.5; Natural has no expression slider |

`repository` and `revision` in the engine examples identify intended assets; the
profile is not a downloader. Natural needs model weights, conditioning, config,
and text-tokenizer files. Expressive requires its separately installed snapshot.
The optional [Deep Narrator template](../config/deep-narrator.example.json) does
not bundle or enable a narrator. Supply actual reference audio/provenance, hashes,
and the intended model before selecting it.

## Commands

Inspect `--help` for the full current argument list. `audiobook` commands require
`--job PATH`; preparation also requires `--epub PATH`.

| Entry point | Action |
| --- | --- |
| `python3 run.py studio` | Launch/reopen the browser app; `--no-browser` prints a URL without opening it. |
| `python3 run.py doctor` | Check GPU computation, encoders, and speech assets; `--kernel-only` does not establish voice readiness. |
| `python3 run.py audiobook prepare` | Create a new immutable job; `--voice`, `--engine natural`, `--code include\|omit`, `--cover`, and `--asr-model` select inputs. |
| `python3 run.py audiobook status` | Read current passage counts without owning the job lock. |
| `python3 run.py audiobook render` | Generate pending passages; optional positive `--max-segments` limits this process. Respects pause. |
| `python3 run.py audiobook resume` | Remove the pause marker and render; does not start the supervisor or run all later stages. |
| `python3 run.py audiobook pause` | Request a stop between passages. |
| `python3 run.py audiobook recover` | Validate/recover checkpoints while holding the job lock; normally run by the supervisor. |
| `python3 run.py audiobook check` | Transcribe rendered passages and apply checks; optional positive `--max-segments` limits newly checked passages. |
| `python3 run.py audiobook review` | List held passages; `--segment`, `--transcript`, and `--note` bind a listening review. |
| `python3 run.py audiobook repair` | Regenerate an explicitly held `--segment TRACK/PASSAGE` with positive `--seed-offset`, then check. |
| `python3 run.py audiobook finish` | Export verified passages; `--output` defaults to `JOB/exports`. |
| `python3 run.py audiobook verify` | Verify the saved final export. |
| `python3 run.py pipeline --job PATH --output PATH` | Supervise recovery, generation, checking, and draft packaging. |
| `python3 run.py draft --job PATH --output PATH` | Package checked passages as a draft, retaining held status. |
| `python3 run.py workspace --job PATH --output PATH --port 8766 --service UNIT` | Serve localhost-only progress; default port is 8765. |

Supervisor flags `--render-batch-size` (default 20) and `--check-batch-size`
(default 50) set the maximum new passages per worker. Both require positive
integers. Recognition reuses Whisper weights within each checking worker while
keeping each passage independent, then releases the model when the worker exits.

The retry limit of 5 without progress, backoff from 3 up to 60 seconds, 15-minute
progress timeout, and six-hour packaging cap are implementation defaults, not CLI
flags.
Recognized retry strings are `illegal memory access` and `Cache thrashing`.
Unknown errors stop. See [operations](operations.md).

## Files and completion evidence

| Job/output file | Purpose |
| --- | --- |
| `plan.json` | Frozen source mapping, identities, segment text, and omission ledger |
| `status.json` | Passage counts; `ready` is not a worker-health signal |
| `pipeline-status.json` | Supervisor heartbeat, stage, failure budget, message, and worker log path |
| `segments/TRACK/PASSAGE.json` | Current passage checkpoint |
| `segments/TRACK/attempts/` | Original audio and evidence; preserve across repairs |
| `worker-logs/` | One local log per worker invocation |
| `recovery/` | Preserved unreadable records |
| `paused` | Persistent between-passage pause request |
| `draft_report.json` in output | Draft metadata, held locations, audio hash and decode result |
| `verification.json` in output | Verified package evidence |
| `final_verification.json` in job | Saved export used by `verify` |

WAV chapters use 24 kHz PCM24; MP3 chapters use 128 kbps. The M4B uses AAC-LC with
chapters, cover art, and audiobook metadata. Extra export bit depth does not add
new source detail. Output ownership checks prevent mixing unrelated files/jobs.

## Local HTTP interface

Studio's authenticated API is separate from the read-only progress server.
Studio requires a permitted localhost Host/Origin and its launcher session token,
accepted as a Bearer header or `token` query parameter. POST requests require
JSON. Avoid logging or publishing token-bearing URLs.

The UI uses `GET /api/config`, `GET /api/jobs`, `GET /api/jobs/ID`,
`POST /api/jobs`, `POST /api/jobs/ID/cancel`, and `POST /api/quit`.
`voice_id` is optional and defaults to the default voice. `/api/config` retains
legacy `voice`/`engines` fields alongside `default_voice_id` and `voices`.
Submitted jobs freeze voice name, reference hash, model, and synthesis settings;
conditioning is reloaded when the voice changes. This is an internal UI
API, not a claim of a separately versioned public contract; inspect `studio.py`
and its validation tests before writing a client.

The progress server binds only to `127.0.0.1` and exposes `/` and `/api/progress`.
It does not use Studio's token. Keep it on localhost; no remote deployment is
provided. Its percentage counts passages, not elapsed or remaining time.
