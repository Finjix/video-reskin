# Commands and data

Entry: `scripts/video_reskin.cmd`. Job creation:

```json
{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery","style":"dark"}
```

`style` is required: `realistic` or `dark` (a key of `styles` in `assets/prompts.json`), chosen from the user's request as SKILL.md describes. A style may have an example picture, `assets/styles/<style>.jpg`, which the first converted frame of each source uses as its look; image-tasks/ records it as style_picture. The delivery is `<source name>_S01_<style>.mp4`.

`init --work-order order.json` prints job_dir. Subsequent commands require `--job-dir <job_dir>`.

Sequence: analyze → survey → plan → refs → render → check → approve, repeating refs/render/check/approve per segment, finally assemble.
refs/render/check default to the first unapproved segment of each selected source.
Use `--only S01` to select sources and `--segment S01:K02` for a segment; approve, redo and select-best require it. redo and select-best need an English `--reason`.
`init --job-dir` must name a folder called temp: its parent becomes the delivery folder.
auth/login/runtime take their own account/runtime options.

Job data:
- job.json: source identity, output root, style, fixed 24 fps timeline.
- catalog/: normalized media information.
- survey/: non-game intervals, end-card boundary, semantic cut candidates and English content inventory.
- plans/: half-open source-frame ranges, passthrough ranges and continuity.
- references/: full-frame images, coverage verdicts per image file, generated-image dependencies, segment reference lists and predecessor generation identity.
- reference-history/: images replaced by `refs --redo`.
- image-tasks/: image generation IDs and chosen look references for resuming.
- state/: video generation ID, the references and predecessor it was rendered from, retry count/history and version-bound approval.
- service-failures/: failed image and video generations; they are resubmitted and never count as retries.
- checks/: segment verdicts, issue times and current-generation token.
- review/: Gemini comparison clips and tail verdicts; for segment issues, working-copy-size PNG frames (frames entries with at/original/result); for failed reference images, paths to the original and generated files (images entries).
- segments/: working-copy cuts uploaded for video edits.
- raw/: original model outputs; ai/: outputs normalized to segment duration.
- work/: the working copy, survey copy, join clips and other local media.

A changed predecessor revokes the following approval and requires refs and render again. Approved results stay pinned when references change.
A gameplay stretch that needs several segments is split into pieces of at least two seconds; a stretch shorter than two seconds on its own is padded with its boundary frame and trimmed back. Gameplay under half a second between or beside original stretches stays original.

## Joins

Gameplay joins use a fixed 4-frame transition (1/6 second) over the outgoing last two and incoming first two frames. Outgoing weights are 3/4, 1/2, 1/2, 1/4 over the frozen incoming first frame; incoming weights are 1/4, 1/2, 1/2, 3/4 over the frozen outgoing last frame; the third incoming frame is fully incoming. The joined clips are encoded once more into the delivery. Boundaries with original/non-game footage and end cards are hard cuts. Total frame count and audio alignment are preserved.

## Media inputs

Intermediate encodes are H.264 CRF 16, yuv420p. The delivery is H.264 CRF 20, preset slow, yuv444p (High 4:4:4 profile), 24 fps, AAC audio from the source. The working copy (preset slow, no audio) has even source dimensions, scaled down to 1080 on the short side when larger. The survey reads it at 6 frames/s, or a bitrate-capped copy when it exceeds the inline upload size. Segment comparison clips use preset slow with each panel at working-copy size, capped to the inline upload size. Gemini receives reference images as the original files. Issue frames are extracted locally by FFmpeg at the times Gemini reports; no reduced or stitched comparison images are made.
