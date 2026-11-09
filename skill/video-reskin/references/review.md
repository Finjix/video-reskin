# Review loop

`review` sends Gemini (`gemini-3.8-flash`, media resolution HIGH) the whole video once: the original and the restyled
video side by side at the delivery's own size (left/right for vertical videos, top/bottom for horizontal ones),
frame-synchronised, 24 fps, near-lossless (crf 4) with the bitrate capped to fill, not pass, the 256,000,000-byte
inline limit, sampled at 4 frames/s up to 75 s, with the seam times and the removal list. There are no separate
Gemini seam close-ups; the seams are yours (below). It returns issues in seven categories:

| Category | Means |
| --- | --- |
| `continuity` | 穿帮: something differs from the original (cartoon material left, an object changes shape, colour or count, things appear or vanish, motion no longer matches) |
| `artifact` | bug: morphing, melting, flicker, jitter, frozen frames, garbled HUD text or numbers, strong noise or a fragmented look |
| `added_text` | captions, warning lines, watermarks or garbled characters the original does not have (the model draws them when a prompt names text that is not on screen) |
| `seam_color` | 拼接色差: a jump in colour, brightness, texture or realism, mostly at seams; `measured` issues come from `assemble`: a dissolved seam inside a shot whose two drawings differ a lot (medium; check its sheet) or a colour residual above 6 after matching |
| `leftover_overlay` | a logo, watermark or warning line still visible |
| `end_card` | closing-card content still visible |
| `style_drift` | the look changes between parts of the video |

Each issue lists the segments it falls in (`S01:K03`). Gemini is a second pair of eyes, not the judge.

## Your own review on stills

Gemini misses things. Every round, look at all stills yourself; they are lossless (PNG), the original on the left
(top), the result on the right (bottom):

- `review/stills/<id>-r<n>/`: the whole video every 3 s. Compare every object with the original and with the rest
  of the video: game-like materials left (cartoon trees, flat wood), a place drawn differently when it comes back,
  colours, counts, garbled text. Look hard at the ground: flat, smooth or uniform soil and snow is the first thing
  that makes a result look fake.
- `review/seams/<id>-<Kxx>.png`: each seam at -1, -0.5, 0, +0.5 and +1 s (original row above, restyled row below).
  Inside a shot the 0 s frame is the switch (a plain cut, or the middle of a light 6-frame dissolve); judge whether the -1/-0.5 frames
  and the +0.5/+1 frames show the same place, objects, colours, materials and realism: a step between the two
  sides is a seam issue, also when the measure calls the seam clean. At a camera cut only the look must carry over.

What you find there counts like a reported issue: confirm it, then redo.

## Confirm on stills

For every issue run `frames --source <id> --at <start> [<end>]` and look at the image: the top row is the original,
the bottom row the result, at T-0.3 s, T and T+0.3 s. Decide:

- **Confirmed**: the bottom row shows the flaw and the top row does not. Note what exactly is wrong.
- **Dismissed**: the original shows the same thing (game physics, clipping, motion blur), the flaw is not visible on
  the stills, or it is the intended material change. Note why.

## Redo

`redo --segment S01:K03 --reason "<what is wrong>" [--prompt "<one corrective Chinese sentence>"] [--refs]`

- Every segment goes back at most twice (`redo` refuses a third time): after that deliver it and list what is open.
  Automatic retries after a failed generation do not count.
- `--prompt` is added to that segment's video prompt for every later attempt; keep it to one sentence about the
  confirmed flaw's look, e.g. `鱼的形状和数量与原视频保持一致，不要变形`, `去掉右上角残留的logo文字`,
  `整体颜色与参考图保持一致，雪地为白色`. A sentence about where something is or when it appears
  (`收集框保持空的，不要提前放入鱼`) made H3 cut to a close-up of its own (GOF3D10232, 2026-11-09): prefer none, and
  `--replace` drops the earlier sentences of a segment.
- A result that cut, zoomed or framed differently from the source is caught by `render` itself (it compares the
  result's edges and coarse layout with the source clip) and rendered once more automatically.
- `--refs` when the scene map or a subject sheet already shows the flaw (wrong objects, cartoon parts, colour cast):
  run `refs --redo S01:scene1` (or `S01:P02`) and check the sheet before rendering. A changed scene map re-renders
  every segment that uses it.
- A seam step: redo the segment on the side that looks wrong against the scene map (usually the one whose look
  differs from the rest of the video); only that segment renders again.
- Then `render` (only redone segments run), `assemble`, `review`. Each review round is numbered.
