# Review loop

`review` sends Gemini (`gemini-3.8-flash`) one clip with the original and the restyled video side by side (left/right
for vertical videos, top/bottom for horizontal ones), frame-synchronised, with the seam times and the removal list,
then a 3-second close-up of every seam (patch joins included) at 10 fps with one question: does the restyled scene
change there? It adds the seams `assemble` measured as `slight` (low: rarely visible in playback) or `visible` (high).
It draws the stills of every issue and asks Gemini once more, on those stills, whether each is visible and whether the
original shows the same: the `triage` hint. Issues come in seven categories:

| Category | Means |
| --- | --- |
| `continuity` | 穿帮: something differs from the original (cartoon material left, an object changes shape, colour or count, things appear or vanish, motion no longer matches) |
| `artifact` | bug: morphing, melting, flicker, jitter, frozen frames, garbled HUD text or numbers, strong noise or a fragmented look |
| `added_text` | captions, warning lines, watermarks or garbled characters the original does not have (Seedance draws them when a prompt names text that is not on screen) |
| `seam_color` | 拼接色差 / scene change at a seam: colour, brightness, texture, terrain, cable colours or realism jump; source `measured` (seam measurement or colour residual), `gemini-seam` (close-up) or `gemini` |
| `leftover_overlay` | a logo, watermark or warning line still visible |
| `end_card` | closing-card content still visible |
| `style_drift` | the look changes between parts of the video |

Each issue lists the segments it falls in (`S01:K03`). Gemini is a second pair of eyes, not the judge.

## Confirm on stills

For every issue look at its stills in `review/issues/<id>-r<n>-p<k>.jpg` (more moments: `frames --source <id> --at
<start> [<end>]`): the upper row is the original, the lower row the result, at T-0.3 s, T and T+0.3 s. The triage hint
is a second opinion only. Decide:

- **Confirmed**: the bottom row shows the flaw and the top row does not. Note what exactly is wrong.
- **Dismissed**: the original shows the same thing (game physics, clipping, motion blur), the flaw is not visible on
  the stills, or it is the intended material change. Note why.
- For a seam issue look at the seam time itself and 0.3 s on either side; a step in colour or texture between the
  middle and one side is a confirmed seam issue.

## Redo

`redo --segment S01:K03 --reason "<what is wrong>" [--prompt "<one corrective Chinese sentence>"] [--keyframe]`

- `--prompt` is added to that segment's video prompt for every later attempt; keep it to one sentence about the
  confirmed flaw, e.g. `鱼的形状和数量与原视频保持一致，不要变形`, `去掉右上角残留的logo文字`,
  `整体颜色与前一段保持一致，雪地为白色`.
- `--keyframe` when the keyframe already shows the flaw (wrong objects, logo kept, colour cast): run
  `keyframes --redo S01:K03` and check the sheet before rendering.
- A seam jump: look at `sheets/<id>-seams.jpg` and the stills at the seam time, then redo the segment after the
  seam (it starts again from the previous result's frame; the later segments of that shot follow). If the same seam
  jumps again, move it to a stiller moment with `split_at` (plan again: the shot's segments render anew).
- Whole-segment redo is the default. A short (a few seconds), local flaw inside a long segment whose later
  segments are done can be patched instead (`patch --segment S01:K03 --at <start> <end> --reason ... [--prompt ...]`):
  only 4-8 s render again and the rest of the shot stays. Not for flaws spread over the segment, a wrong colour or
  look of the whole segment, fast camera motion or a cut at the patch's edges. After `assemble` both patch joins
  must be clean or slight; a visible one -> `patch --drop S01:K03:P1`, then `redo` the segment.
- Garbled screen-fixed text or UI needs no render: an `overlays` item over it (`box`/`poly`, no `colors`), `plan`,
  `assemble`.
- Then `run` (only redone segments and patches render, with their checks; assembly and review follow per source).
  Each review round is numbered; after `review_rounds` rounds deliver and list what is still open.

## Held segments (segment check)

`run`/`render` checks every segment that has a successor in its shot before the successor starts: the seam to the
segment before it is measured, and Gemini compares the segment with its original (same categories; the HUD and
burned overlays are not in yet). A visible seam, a high issue or two medium ones hold the segment; the rest of the
shot waits, other shots go on. Confirm on `review/gate/<id>-<Kxx>-p<k>.jpg` like a review issue: `redo` (one segment
renders again instead of the whole shot) or `accept --segment <key> --reason <why>`.
