# Gate and checks

Every segment passes a gate before anything continues from it, and there is no review of the whole video
afterwards: a video whose every segment and every seam passed is done. Gemini (`gemini-3.8-flash`, media resolution
HIGH) does the watching; you confirm what it reports and look for anything else.

## The gate: `check`, then `approve`

A segment that continues a shot is drawn from the previous segment's frame where it begins (the handoff frame), so
`render` submits it only after that segment is approved. After every `render` run, `check` the new results. For each:

1. **The segment.** Gemini watches it beside its source clip: both side by side at the delivery's own size
   (left/right for vertical videos, top/bottom for horizontal ones), frame-synchronised, 24 fps, near-lossless (crf 4)
   with the bitrate capped to fill, not pass, the 256,000,000-byte inline limit, sampled at 6 frames/s (whatever its
   length), with the removal list. It also gets, as a still, the frame the next segment will start from: a flaw
   there is reported high.
2. **The seam to the segment before.** The script joins every segment up to this one (a preview, no audio: the same
   retimed clips, colour matching and switch the delivery gets), and Gemini watches the join 1 s either side beside
   the original; inside a shot it also compares the frame this segment was told to start from (A) with its own
   first frame (B) and the original (C). The measured seam figures (a dissolved seam whose two drawings differ a
   lot, a colour residual above 6 after matching) join the issues. At a camera cut only the look must carry over.
   While the segment before has no approved result the seam is `pending`: check again after approving it.
3. **The verdict.** `fail` on a high issue anywhere, two or more medium issues in the segment, any medium or high
   issue at the seam, or a pending seam. `approve --segment <key>` takes a passed check of the current result; a
   failed one only with `--dismiss "<why each issue is wrong>"` once you saw every sheet and none holds. Anything
   you confirmed goes to `redo`.

Issue categories (segment and seam alike):

| Category | Means |
| --- | --- |
| `continuity` | 穿帮: something differs from the original (cartoon material left, an object changes shape, colour or count, things appear or vanish) |
| `motion` | 运动不一致: what the original moves (conveyor items, walking characters, flying objects, the camera) stands still, moves the other way or at another speed, or something still starts moving |
| `artifact` | bug: morphing, melting, flicker, jitter, frozen frames, garbled HUD text or numbers, strong noise or a fragmented look |
| `added_text` | captions, warning lines, watermarks or garbled characters the original does not have (the model draws them when a prompt names text that is not on screen) |
| `seam_color` | 拼接色差: a jump in colour, brightness, texture or realism at the join |
| `leftover_overlay` | an explicitly requested removal still visible; original logos and warning lines are allowed |
| `end_card` | closing-card content still visible |
| `style_drift` | the look changes between parts of the video |

Gemini is a second pair of eyes, not the judge: it misses things and reports things that are not there.

## Your part: Gemini first, then you

`review/check/<id>-<Kxx>-a<n>/` holds lossless PNGs, the original row above, the restyled row below:

- `<nn>-<category>.png`: one sheet per issue, at its start, middle and end (a short issue at its middle and 0.3 s
  either side). Confirm or dismiss the issue on it (below).
- `seam-<t>.png`: the seam every 0.25 s from -0.5 to +0.5 s, five frames, **only when Gemini found nothing at the
  seam**: review it yourself. Inside a shot the 0 s frame is the switch (a plain cut, or the middle of a light 6-frame
  dissolve); judge whether the frames before and after it show the same place, objects, colours, materials and
  realism: a step is a seam issue, also when the measure calls it clean. Where Gemini reported a seam issue you judge
  that issue on its own sheet instead.
- `handoff-<t>.png`: the full-size frame the next segment starts from, **only when no issue Gemini reported covers
  it**: review it yourself; unrequested changes to source overlays, a changed or missing character, crowd or object, a wrong
  material there is copied into the rest of the shot, so a segment whose handoff frame shows one is not approved.

On every picture you look at, never only the reported issue: also look over the whole picture for anything else
wrong (game-like materials left such as cartoon trees or flat wood, a place drawn differently, colours, counts,
garbled text or logos, a changed or missing character, and the ground: flat, smooth or uniform soil and snow is the
first thing that makes a result look fake). What you find counts like a reported issue. When you have reason to
doubt a stretch Gemini passed (a part the user named, a fast or crowded moment), `frames --source <id> --at <t>`
gives three frames of the delivery there.

## Confirm on the sheets

For every issue look at its sheet: the top row is the original, the bottom row the result. Decide:

- **Confirmed**: the bottom row shows the flaw and the top row does not. Note what exactly is wrong.
- **Dismissed**: the original shows the same thing (game physics, clipping, motion blur), the flaw is not visible on
  the sheet, or it is the intended material change. Note why (it goes into `--dismiss` when all are dismissed).

A `motion` issue does not show on any single still: pick one item that stands out (the first fish on a belt, a
character's foot) and compare where it is in the three frames of each row against something fixed (a post, the belt's
end). Evenly spaced things (belt items, fences, crowds) look the same after a step of one spacing, so also look at
the gaps and ends, not only the pattern.

## Redo

`redo --segment S01:K03 --reason "<what is wrong>" [--prompt "<one corrective English sentence>"] [--replace] [--refs]`

- Every segment goes back at most five times (`redo` refuses a sixth): after that ask the user when the flaw
  would carry into the rest of the shot, otherwise deliver it and list what is open. Automatic retries after a
  failed generation do not count.
- The first two redos of a flaw go without a new sentence or with a minimal one (a result often differs by chance).
  Once two have not fixed it, inject a targeted sentence that names exactly how it must look or move, naming the
  things as the original shows them (`whole tuna`, not `fish`: a general word lets the model draw another thing of
  that kind from the materials), and change the pictures where they cause the flaw: `sheets` (a character redrawn
  as another), `motion` (items that must move), `split_at` (a part that needs a clip of its own).
- `--prompt` is added to that segment's video prompt for every later attempt; keep it to one sentence about the
  confirmed flaw's look, e.g. `Keep every fish whole, with the shape and count of the original.`, `Keep the overall colours of the reference images; the snow stays white.`.
  A sentence about where something is or when it appears (`Keep the collection box empty.`) can make the video
  model move its camera: prefer none, and `--replace` drops the earlier sentences of a segment.
- A result that cut, zoomed or framed differently from the source is caught by `render` itself (it compares the
  result's edges and coarse layout with the source clip) and rendered once more automatically.
- `--refs` when the scene map or a subject sheet already shows the flaw (wrong objects, cartoon parts, colour cast):
  run `refs --redo S01:scene1` (or `S01:P02`) and look at its check before rendering. A changed scene map re-renders
  every segment that uses it.
- A seam flaw: redo the later segment (it was drawn to continue the earlier one); only that segment renders again.
- Then `render` (only redone segments run) and `check` the new result.
