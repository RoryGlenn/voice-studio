# Repository Guidelines

## Project Structure

Application modules live in `src/voice_studio/`; browser assets in `web/studio/` and `web/audiobook/`; tests in `tests/`; service templates in `config/systemd/`; historical experiments in `tools/legacy/`. Use `run.py` as the stable launcher. Preserve compatibility entrypoints and private job locations during refactors.

## Development Commands and Style

Use Python 3.12, uv, Node, and FFmpeg/ffprobe. Follow [installation](docs/installation.md) for platform-specific inference dependencies.

- `python3 run.py studio`: launch the local narration interface.
- `python3 run.py test`: discover Python `unittest` tests named `test_*.py`.
- `node --test tests/test_web.cjs`: test Studio browser behavior.
- `node tests/test_live_book_progress.cjs`: test audiobook monitoring and review behavior.
- `uvx ruff check .` and `uvx ruff format --check .`: check Python lint and formatting.

Use four-space Python indentation, `snake_case` functions/modules, and `PascalCase` classes. Follow surrounding JavaScript conventions. Read [development](docs/development.md) and applicable operating guides before workflow changes; development includes the mypy command. Use Doc Agent for documentation work.

## Validation

For code changes, run the tests above, Ruff, and mypy; verify the affected user flow. Use synthetic fixtures for review mutations, never automatic decisions on a user's book. Mock speech tests do not establish narration quality; no numeric coverage threshold is configured.

For documentation-only changes, verify commands, links, and accuracy. For local configuration changes, validate the saved configuration and live behavior; run broader tests when runtime logic changes. Report checks performed and limitations.

## Private Data and Runtime Safety

Keep books, recordings, models, and process information local. `data/` and `local.toml` are Git-ignored; never force-add private configuration or credentials. Restart idle Studio after profile changes; existing jobs retain frozen identities. Never bypass job identities, GPU/checkpoint locks, or review evidence. Preserve original attempts.

Keep the correct job's dashboard available for every task; reuse healthy instances, verify HTTP/state, and provide its URL. DDIA uses `voice-studio-ddia-progress.service` at <http://127.0.0.1:8765/>. Inspect worker state before service changes; starting the dashboard does not authorize narration. Improve dashboard information within scope; see [operations](docs/operations.md). Ask about unresolved product decisions or substantial scope expansions.

## Commits and Pull Requests

Use concise imperative commit subjects, such as “Simplify audiobook overview progress cards.” PRs should explain the problem, resulting behavior, validation, relevant issue links, and screenshots for visual changes.

Issue creation, commit, push, PR creation, and merge require distinct authorization. “Take this from issue to merge” authorizes the complete workflow, including finding/creating a scoped issue. Honor required CI, reviews, and branch protections; verify the final remote state.
