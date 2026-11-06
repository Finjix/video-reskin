# Review loop

`review` sends Gemini (`gemini-3.8-flash`, media resolution HIGH) the whole video: the original and the restyled
video side by side at the delivery's own size (left/right for vertical videos, top/bottom for horizontal ones),
frame-synchronised, 24 fps, near-lossless (crf 4) with the bitrate capped to fill, not pass, the 256,000,000-byte inline limit, sampled
at 4 frames/s up to 75 s, with the seam times and the removal list. Each seam also gets its own Gemini close-up (2 s
on either side, `gemini-seam` issues). It returns issues in seven categories:

| Category | Means |
| --- | --- |
| `continuity` | 穿帮: something differs from the original (cartoon material left, an object changes shape, colour or count, things appear or vanish, motion no longer matches) |
| `artifact` | bug: morphing, melting, flicker, jitter, frozen frames, garbled HUD text or numbers, strong noise or a fragmented look |
| `added_text` | captions, warning lines, watermarks or garbled characters the original does not have (Seedance draws them when a prompt names text that is not on screen) |
| `seam_color` | 拼接色差: a jump in colour, brightness, texture or realism, mostly at seams; `measured` issues come from `assemble`: a `visible` seam inside a shot (high: the two results differ at the switch clearly more than the picture moves, or one region changes a lot) or a colour residual above 6 after matching |
| `leftover_overlay` | a logo, watermark or warning line still visible |
| `end_card` | closing-card content still visible |
| `style_drift` | the look changes between parts of the video |

Each issue lists the segments it falls in (`S01:K03`). Gemini is a second pair of eyes, not the judge.

## Your own review on stills

Gemini misses things (it passed a 0.5 s dissolve and cartoon trees). Every round, look at all stills yourself; they
are full size and lossless (PNG), the original on the left (top), the result on the right (bottom):

- `review/stills/<id>-r<n>/`: the whole video every 2 s. Compare every object with the original and with the rest
  of the video: game-like materials left (cartoon trees, flat wood), a place drawn differently when it comes back,
  colours, counts, garbled text. Look hard at the ground: flat, smooth or uniform soil and snow is the first thing
  that makes a result look fake.
- `review/seams/<id>-<Kxx>/`: each seam at -2, -1, -0.5 s, every frame across the switch, +0.5, +1, +2 s. Any step,
  fade or softening of texture, colour or sharpness across them is a seam issue, also when Gemini and the measure
  call the seam clean.

What you find there counts like a reported issue: confirm it, then redo.

## Confirm on stills

For every issue run `frames --source <id> --at <start> [<end>]` and look at the image: the top row is the original,
the bottom row the result, at T-0.3 s, T and T+0.3 s. Decide:

- **Confirmed**: the bottom row shows the flaw and the top row does not. Note what exactly is wrong.
- **Dismissed**: the original shows the same thing (game physics, clipping, motion blur), the flaw is not visible on
  the stills, or it is the intended material change. Note why.
- For a seam issue look at the seam time itself and 0.3 s on either side; a step in colour or texture between the
  middle and one side, or two drawings showing through each other (ghosting), is a confirmed seam issue.

## Redo

`redo --segment S01:K03 --reason "<what is wrong>" [--prompt "<one corrective Chinese sentence>"] [--keyframe]`

- `--prompt` is added to that segment's video prompt for every later attempt; keep it to one sentence about the
  confirmed flaw, e.g. `鱼的形状和数量与原视频保持一致，不要变形`, `去掉右上角残留的logo文字`,
  `整体颜色与前一段保持一致，雪地为白色`.
- `--keyframe` when the keyframe already shows the flaw (wrong objects, logo kept, colour cast): run
  `keyframes --redo S01:K03` and check the sheet before rendering.
- A seam colour jump or a visible seam: redo the segment after the seam (it is rendered from the one before it;
  segments chained after it render again too).
- Then `render` (only redone segments run), `assemble`, `review`. Each review round is numbered; after
  `review_rounds` rounds deliver and list what is still open.
