# Documentation validation and known limits

This record describes checks performed for the documentation rewrite on
September 27, 2026. It is evidence for these instructions, not a promise that
all future machines, model releases, or books will behave identically.

## What was checked

- CLI names/options and configuration behavior were compared with the local
  implementation and command help. Existing service status and the live
  localhost progress endpoint were read without stopping the book job.
- The model host's metadata confirmed the Natural, Whisper, and S3TokenizerV2
  repositories, pinned revisions, and snapshot filenames used in installation.
  The installed `snapshot_download` signature accepts the documented arguments.
  Model contents were not redownloaded as part of this documentation task.
- All 33 shell blocks were parsed with `bash -n`; four embedded Python snippets were compiled.
  The local-configuration and voice-profile snippets ran in an isolated temporary
  directory with synthetic fixture bytes. The resulting profile passed the
  application's profile loader. This checks configuration creation, not speech
  suitability of a real voice recording.
- The background-service example generated both units in an isolated temporary
  home and passed `systemd-analyze --user verify`. Those example units were not
  enabled, and the computer was not rebooted for this check.
- The original sample generator produced **The Library Garden**, two chapters
  and four passages. It refused to overwrite the EPUB. Preparation, rendering,
  checking, draft packaging, and verified packaging ran with synthetic tone
  generation and a recognition fixture. Both M4Bs had two chapters and passed
  full FFmpeg decoding. This verifies workflow/packaging, not native speech.
- Local Markdown file links and section anchors were checked, including the
  compatibility entry points retained in the README and former combined guide.
- Repository checks passed: 107 Python tests (two skipped), 16 browser tests,
  Ruff lint/formatting, and mypy on the documented ten source files. See the PR
  for results tied to the reviewed commit.

## What is not established

The fresh-install sequence has not been executed end-to-end on a clean Ubuntu or
macOS machine for this rewrite. The existing workstation already had its models,
driver, and voices. Native speech quality, a fresh model download, macOS behavior,
and service boot behavior therefore still require environment-specific acceptance.
No claims of a fixed CUDA root cause or a universally sufficient VRAM threshold
are made. GPU admission checks are not reservations.

## Acceptance on your machine

1. Follow installation using an actual reference recording and the pinned models.
2. Run `doctor` successfully, generate the documented short preview, and listen.
3. Generate the original sample EPUB, prepare it as a new job, and run the
   supervisor with actual speech generation and recognition.
4. Check the two-chapter output, play the M4B, and investigate any held passages.
5. If you install a service, confirm that it owns the correct job, that its
   dashboard reflects its health, and that a controlled stop/start resumes saved
   passages. Test reboot recovery only when it is safe to reboot your machine.

Keep local logs and reports as evidence; do not publish private audio, tokens, or
book content in bug reports. Report the command, runtime versions, symptom, and
redacted error. Update this record when broader validation is actually performed.
