# Review loop

`review` sends Gemini (`gemini-3.8-flash`) one clip with the original and the restyled video side by side (left/right
for vertical videos, top/bottom for horizontal ones), frame-synchronised, with the seam times and the removal list,
then a 3-second close-up of every seam at 10 fps with one question: does the restyled scene change there? It adds the
seams `assemble` measured as `slight` (medium) or `visible` (high). Issues come in seven categories:

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

For every issue run `frames --source <id> --at <start> [<end>]` and look at the image: the top row is the original,
the bottom row the result, at T-0.3 s, T and T+0.3 s. Decide:

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
- Then `render` (only redone segments run), `assemble`, `review`. Each review round is numbered; after
  `review_rounds` rounds deliver and list what is still open.
