# Install Voice Studio and verify a first render

Set up dependencies, download pinned models, configure your own voice reference,
then listen to a short recording before starting a book. Run commands from the
repository root unless stated otherwise. This procedure uses the default `data/`
layout and the Natural engine. Keep existing installations and their `local.toml`
intact; do not apply fresh-setup commands over a working configuration.

## Prerequisites

- Git, Python 3.12, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Ubuntu Linux x86-64 with a working NVIDIA driver compatible with CUDA 12, or
  Apple Silicon macOS with Metal. There is no CPU inference fallback.
- FFmpeg and ffprobe on `PATH`. Node is needed for developer browser tests only.
- Internet access for the initial package/model downloads. Narration runs offline
  afterward. Allow several GB for models and additional space for generated audio;
  this project does not enforce a disk-space budget.
- A WAV reference of the voice you intend to use. Supply a recording you are
  entitled to use; do not use a placeholder audio file or invent its provenance.

Clone and enter the repository:

```sh
mkdir -p ~/repos
cd ~/repos
git clone https://github.com/RoryGlenn/voice-studio.git
cd voice-studio
```

Choose **one** platform path below. If Python/uv or the NVIDIA driver are missing,
install those prerequisites before proceeding. Do not copy a virtual environment
from another operating system.

## Ubuntu NVIDIA

The existing runtime has been exercised on Ubuntu 24.04 and an RTX 3060 Ti with
8 GiB VRAM. That is tested hardware, not a universal minimum specification.

```sh
nvidia-smi
sudo apt-get update
sudo apt-get install ffmpeg
uv sync --locked --extra ubuntu
```

`nvidia-smi` should identify your GPU without a driver error. The locked Ubuntu
extra supplies MLX CUDA 12 plus runtime/compiler-header dependencies; it does
not install an NVIDIA display driver. Continue to local configuration below.

## Apple Silicon macOS

Install FFmpeg through your usual package manager. If you use Homebrew:

```sh
brew install ffmpeg
uv sync --locked --extra native
```

These instructions preserve the supported Metal path; this documentation update
was validated on Ubuntu, not a fresh Mac. Continue with the same configuration.

## Create local configuration

On a **new installation**, copy the template only if `local.toml` is absent:

```sh
python3 - <<'PY'
from pathlib import Path
with Path('local.toml').open('xb') as out:
    out.write(Path('local.example.toml').read_bytes())
PY
```

An existing destination raises an error rather than overwriting your settings.
The defaults use `data/` for the workspace and `data/studio/` for saved voices.
If you change these, adapt every destination in this guide consistently; see
[path resolution](reference.md#paths-and-configuration-precedence).

## Download the Natural model and recognition assets

The following downloads public model snapshots from Hugging Face into your local
workspace; it does not send a book or reference voice. Run it once for setup.
Repositories/revisions were checked against the model host; downloads and native
inference have not been repeated on a clean machine for this rewrite.

Sources: [Natural model](https://huggingface.co/mlx-community/chatterbox-turbo-fp16/tree/b2d0a13aa7cfff0a06d9acb247ae91c8f19a6d75),
[Whisper](https://huggingface.co/mlx-community/whisper-small.en-mlx/tree/52a88bf6e98b114a210c21bb83e22d6e1505cb73),
and [speech tokenizer](https://huggingface.co/mlx-community/S3TokenizerV2/tree/e0c9886f0e1c35ae85b1f27277416fb19fc72bec).
The script uses the `huggingface_hub` package in the installed environment.

```sh
.venv/bin/python - <<'PY'
import hashlib
from pathlib import Path
from huggingface_hub import snapshot_download

root = Path('data').resolve()
snapshot_download(
    'mlx-community/chatterbox-turbo-fp16',
    revision='b2d0a13aa7cfff0a06d9acb247ae91c8f19a6d75',
    local_dir=root / 'work/own_voice/models/chatterbox-turbo-fp16',
)
snapshot_download(
    'mlx-community/whisper-small.en-mlx',
    revision='52a88bf6e98b114a210c21bb83e22d6e1505cb73',
    local_dir=root / 'work/whisper-small.en-mlx',
)
revision = 'e0c9886f0e1c35ae85b1f27277416fb19fc72bec'
cache = root / 'work/hf-narration-cache/hub'
snapshot = Path(snapshot_download(
    'mlx-community/S3TokenizerV2', revision=revision, cache_dir=cache,
))
with (snapshot / 'model.safetensors').open('rb') as handle:
    actual = hashlib.file_digest(handle, 'sha256').hexdigest()
expected = '928726bc1f206a613d36b8f49e297eae9c5593a21bf9b92ddfe2c23f85eb92cc'
if actual != expected:
    raise RuntimeError('Speech tokenizer checksum mismatch')
# The offline upstream loader resolves main, so bind it to the verified snapshot.
ref = cache / 'models--mlx-community--S3TokenizerV2/refs/main'
ref.parent.mkdir(parents=True, exist_ok=True)
ref.write_text(revision)
print('Model snapshots downloaded; pinned speech tokenizer verified.')
PY
```

Keep complete snapshots, including text-tokenizer and conditioning files. A lone
weights file is insufficient. Whisper is used for whole-book checking; `doctor`
checks speech assets but does not by itself prove the full recognition workflow.
The model repository name says `fp16`; previously inspected Natural weights were
F32. Do not infer memory consumption or precision solely from that name.

## Configure a saved voice

Copy your chosen reference recording to a new `data/studio/profile/reference.wav`.
This script refuses to overwrite either the reference or an existing profile.
Replace `/path/to/your-reference.wav` before running:

```sh
.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

source = Path('/path/to/your-reference.wav')
directory = Path('data/studio')
reference = directory / 'profile/reference.wav'
profile_path = directory / 'voice_profile.json'
if reference.exists() or profile_path.exists():
    raise SystemExit('Reference/profile already exists; review it instead of overwriting.')
audio = source.read_bytes()
reference.parent.mkdir(parents=True, exist_ok=True)
with reference.open('xb') as handle:
    handle.write(audio)
profile = json.loads(Path('config/voice_profile.example.json').read_text())
profile['name'] = 'My narrator'
profile['reference_sha256'] = hashlib.sha256(audio).hexdigest()
# This minimal setup installs only Natural and has no invented provenance file.
profile['engines'] = {'natural': profile['engines']['natural']}
profile.pop('reference_provenance', None)
with profile_path.open('x') as handle:
    json.dump(profile, handle, indent=2)
print('Voice profile saved. Run doctor, then listen to a preview.')
PY
```

The template already points at `profile/reference.wav`. If you have a genuine
provenance record, store it privately and add `reference_provenance` with its path
relative to `data/studio`. A configured but missing provenance file blocks EPUB
preparation. Additional voices and Expressive are optional; see [voice profiles](reference.md#voice-profiles).

## Verify and listen

Close GPU-heavy apps or wait for a free GPU; stop competing narration workers
before this check. On Ubuntu:

```sh
python3 run.py doctor
./launch-voice-studio.sh
```

On macOS, run `python3 run.py doctor`, then open `Launch Voice Studio.command`.
`doctor` should exit successfully and report `kernel_compilation: passed`, the
speech-tokenizer check, and model fingerprints. It checks real GPU computation,
encoders, and assets; it does not generate a speech preview.

In the browser, select your narrator and Natural. Paste:

> A small garden grows beside the library. Each morning, we water the plants and record what changed.

Choose **Preview first 120 words**, listen, then **Generate audio** and download
WAV or MP3. Confirm that the file plays and contains the intended words. If the
voice or pronunciation is unsuitable, adjust the reference and prepare a **new**
book job; do not alter a reference frozen into an existing job.

Next: [create the original sample EPUB](audiobooks.md#try-the-original-sample),
then follow the audiobook workflow. See [validation limits](validation.md) and
[troubleshooting](operations.md#understand-progress-and-recover-from-a-stop).
