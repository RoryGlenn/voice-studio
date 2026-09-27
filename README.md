# Voice Studio

Create local speech recordings and chaptered audiobooks using a saved voice.
Preview pasted text in the browser, export WAV or MP3, or turn an EPUB into an M4B
with resumable generation and automated speech checks. Models run on your
computer; narration does not require a cloud API or a Codex session.

## Start here

- **New computer:** [install models, configure a voice, and verify a first render](docs/installation.md).
- **Existing installation:** [narrate text in the browser](docs/narrating-text.md).
- **Whole book:** [prepare an EPUB and create a draft](docs/audiobooks.md), then [review quality](docs/quality-review.md).
- **Unattended work:** [install a background job or recover a failure](docs/operations.md).
- **This workstation:** [control the configured DDIA audiobook](docs/workstation-ddia.md).

Native inference supports Apple Silicon with MLX Metal and Ubuntu Linux x86-64
with MLX CUDA 12 and an NVIDIA GPU. Python 3.12 and FFmpeg are required. Models
and a voice reference are separate assets; cloning this repository alone is not
a ready-to-render installation. See [tested scope](docs/validation.md).

A draft includes passages held for review. A verified export passes the software's
wording, signal, and timing checks, but still needs listening review.

## Existing installation

From the configured repository on Ubuntu:

```sh
./launch-voice-studio.sh
```

On macOS, open `Launch Voice Studio.command`. Use the launcher's local URL,
including its private session token. [Browser instructions](docs/narrating-text.md)
explain previews, controls, downloads, and competing workers.

## Run narration yourself

The [task index](docs/running-narration.md) routes you to the appropriate workflow.

## Clean environment

See [installation](docs/installation.md) for platform setup and assets, or
[development](docs/development.md) for model-free test dependencies.

## Ubuntu installation

Follow [Ubuntu setup](docs/installation.md#ubuntu-nvidia).

## Narrator selection

Use [saved voices](docs/reference.md#voice-profiles) to configure narrators and
[the browser](docs/narrating-text.md#choose-a-narrator) to choose one.

## Reusable EPUB audiobook jobs

Follow [EPUB preparation](docs/audiobooks.md), [review and export](docs/quality-review.md),
and the [command reference](docs/reference.md#commands).

## Supervised audiobook recovery

See [background services and troubleshooting](docs/operations.md). Workers resume
checkpoints and retry recognized CUDA faults in fresh processes; persistent errors
stop for inspection. Recovery does not claim to fix the underlying GPU fault.

## Audiobook workflow

The old `book`, `book-new-b`, and `progress` commands are edition-specific.
Keep using [historical workflow instructions](docs/legacy-workflows.md) for those
saved editions; use `audiobook` for new EPUBs.

## Repository contents

See the [architecture and source map](docs/development.md). Media, reference voices,
models, and `local.toml` stay outside Git. Runtime data is private and local.

## Development checks

The [development guide](docs/development.md#development-checks) lists the required
Python, browser, lint, formatting, and type checks. [Documentation validation](docs/validation.md)
records what has and has not been demonstrated.

## Project layout

Application code lives in `src/voice_studio/`, browser interfaces in `web/studio/` and `web/audiobook/`, tests in `tests/`, and older book-specific tools in `tools/legacy/`. Use `python3 run.py --help` for launch commands and `python3 run.py test` for the Python suite. Private jobs, recordings, and models stay in ignored runtime directories. See [development](docs/development.md) and the [audiobook workspace](docs/operations.md#use-the-audiobook-workspace).
