# Voice Studio contributor guidance

- Put application code in `src/voice_studio/`, frontend assets in the appropriate `web/studio/` or `web/audiobook/` directory, tests in `tests/`, and historical experiments in `tools/legacy/`.
- Use `run.py` as the stable launcher. Preserve compatibility entrypoints while saved job scripts or running supervisors use them. Do not relocate private `data/` jobs during source refactors.
- Read `docs/development.md` and the relevant operating guide before modifying a workflow. Use the Doc Agent skill for documentation changes.
- Inspect active worker state before restarting services. The progress/workspace service can restart independently of the audiobook worker.
- Keep book text, audio, models, and process information local. Never bypass job identities, GPU/checkpoint locks, or strict review evidence. Preserve original audio attempts.
- Validate with `python3 run.py test`, `node --test tests/test_web.cjs`, and `node tests/test_live_book_progress.cjs`; follow the lint/type checks in `docs/development.md`. Test review mutations with synthetic fixtures, not automatic decisions on the user's book.

## Dashboard as the default workspace

- For every Voice Studio task, including audiobook rendering, diagnostics, development, and maintenance, ensure the local audiobook workspace site is running. Reuse a healthy existing instance; start the appropriate dashboard service if it is stopped. Verify its HTTP response and current job state, and give the user its local URL.
- For the configured DDIA job, the dashboard service is `voice-studio-ddia-progress.service` and the URL is `http://127.0.0.1:8765/`. Consult `docs/operations.md` for other jobs; associate each dashboard with the correct job and output directory rather than showing an unrelated book as current work.
- Keep the site available throughout long-running work. Starting the dashboard does not authorize starting, resuming, or restarting narration; preserve the worker's existing state unless the task requires a change.
- As project workflows evolve, proactively maintain useful dashboard information and controls within the authorized task: accurate stages, progress, review explanations, diagnostics, and actionable errors. Prefer focused improvements grounded in real work; verify changes and keep all data local. Ask about unresolved product choices or substantial scope expansions.
