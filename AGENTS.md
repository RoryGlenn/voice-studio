# Voice Studio contributor guidance

- Put application code in `src/voice_studio/`, frontend assets in the appropriate `web/studio/` or `web/audiobook/` directory, tests in `tests/`, and historical experiments in `tools/legacy/`.
- Use `run.py` as the stable launcher. Preserve compatibility entrypoints while saved job scripts or running supervisors use them. Do not relocate private `data/` jobs during source refactors.
- Read `docs/development.md` and the relevant operating guide before modifying a workflow. Use the Doc Agent skill for documentation changes.
- Inspect active worker state before restarting services. The progress/workspace service can restart independently of the audiobook worker.
- Keep book text, audio, models, and process information local. Never bypass job identities, GPU/checkpoint locks, or strict review evidence. Preserve original audio attempts.
- Validate with `python3 run.py test`, `node --test tests/test_web.cjs`, and `node tests/test_live_book_progress.cjs`; follow the lint/type checks in `docs/development.md`. Test review mutations with synthetic fixtures, not automatic decisions on the user's book.
