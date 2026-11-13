# Commands and data

Entry: `scripts/video_reskin.cmd`. Job creation:

```json
{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery"}
```

`init --work-order order.json` prints job_dir. Subsequent commands require `--job-dir <job_dir>`.

Sequence: analyze → survey → plan → refs → render → check → approve, repeating refs/render/check/approve per segment, finally assemble.
refs/render/check default to the first unapproved segment of each selected source.
Use `--only S01` to select sources and `--segment S01:K02` for a segment; approve and redo require it. redo needs an English reason.
auth/login/runtime take their own account/runtime options.

Job data:
- job.json: source identity, output root, fixed 24 fps timeline.
- catalog/: normalized media information.
- survey/: non-game intervals, end-card boundary, semantic cut candidates and English content inventory.
- plans/: half-open source-frame ranges, passthrough ranges, continuity and transition policy.
- references/: full-frame images, coverage IDs, generated-image dependencies, segment reference lists and predecessor generation identity.
- image-tasks/: image generation IDs for resuming.
- state/: video generation ID, retry count/history and version-bound approval.
- checks/: segment verdicts, issue times and current-generation token.
- review/: Gemini comparison clips; for segment issues, native-size PNG frames (frames entries with at/original/result); for failed reference images, paths to the original and generated files (images entries).
- raw/: original model outputs; ai/: outputs normalized to segment duration.
- work/: temporary previews and local normalized media.

Redoing a predecessor keeps later outputs but invalidates their approval. Approved results stay pinned when references change.
A segment shorter than two seconds is padded with its boundary frame and trimmed back at assembly.

## Joins

Gameplay joins use a fixed 4-frame transition (1/6 second) over the outgoing last two and incoming first two frames. Outgoing weights are 3/4, 1/2, 1/2, 1/4 over the frozen incoming first frame; incoming weights are 1/4, 1/2, 1/2, 3/4 over the frozen outgoing last frame; the third incoming frame is fully incoming. Boundaries with original/non-game footage and end cards are hard cuts. Total frame count and audio alignment are preserved.

## Media inputs

The working copy is H.264 CRF 16, preset slow, yuv420p, even source dimensions, no audio; the survey reads it directly at 6 frames/s. Segment comparison clips use the same encoding with each panel at working-copy size, capped to the inline upload size. Gemini receives reference images as the original files. Issue frames are extracted locally by FFmpeg at the times Gemini reports; no reduced or stitched comparison images are made.
