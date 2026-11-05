# Review loop

`review` sends Gemini (`gemini-3.8-flash`) one clip with the original and the restyled video side by side (left/right
for vertical videos, top/bottom for horizontal ones), frame-synchronised, with the seam times and the removal list,
then a close-up of every seam at 10 fps (from 1.5 s before the switch to 1.5 s after the moment the later segment's
own frames start) with one question: does the restyled scene change there? Every in-shot seam is then checked
twice more, because a seam between two renders hides in a pan (GOF3D10012 at 26.4 s: the river less saturated, the
fish bluer, a tree crown at the top edge in the game's cone shape; every grey-level measure and the 3 s close-up passed
it):
- `measured` look step (from `assemble`, `look` in the seam entry): for every pair of consecutive frames of the whole
  video, the change of saturation and of colour after the camera's shift (from the original) is taken out and the
  original's own change over the same pair subtracted; the seam's step is scored against the video's ordinary pairs
  (robust z: 8 or more visible, 5 slight). 26.4 s scored saturation 2.31 against a usual 0.19 (z 9.4); of 1291
  ordinary pairs of that video one reached z 8, in the original's own end-card flash.
- `gemini-seam-pair`: the last frame before the seam and the first after it at full size, the original's two moments
  below (`review/<id>-seam-<Kxx>-<t>-pair.jpg`), with the question which differences the original does not show.
  Medium or high is a high issue; low is the flicker any frame pair has (an ordinary pair of the same video: low). It adds the
seams `assemble` measured as `slight` (low: rarely visible in playback) or `visible` (high), and the wiped logo
corners it measured as `soft` (low) or `smeared` (high). It draws the stills of every issue and asks Gemini once more,
on those stills, whether each is visible and whether the original shows the same: the `triage` hint.

The goal it judges against is a stable video with real materials and the original's shapes kept, not the most
realistic look. Gemini rates the realism over time (`realism`, 5 = looks like real camera footage, 3 = mixed, 1 =
cartoon) and lists what still looks game-like; the timeline is reported (`realism: 0.0-22.9 s 4/5; 45.6-65.7 s 3/5
(皮卡车)`), and only a range rated 2 or lower becomes a medium `not_realistic` issue. Findings of segment checks the
editor accepted go along to every later check and to the review, which do not report them again.

The identity check keeps every recurring character and object at one look from the first second to the last: Gemini
finds the recurring subjects on the original frames (the protagonist always, up to three more), the program cuts
close-ups of each from the styled keyframes (REF) and from moments across the video, and Gemini judges each pair on
build, rendering and colours (`identity` issues, sheets `review/<id>-identity-r<n>-subjects.jpg`). It runs in the
review only (not per segment or per preview).

The kept-element check holds every `ui` item with a `box` (game logos, warning lines, counters, captions, buttons)
to the original: the review draws a sheet per item, `review/<id>-kept-r<n>-<k>.jpg` (eight moments, upper row
original, lower row delivered), on which Gemini compares the element character by character (`gemini-keep`):
garbled, blurred, doubled (a second copy beside it), changed (moved, resized, recoloured, restyled letters, partly
missing) are always high. The models draw these elements, so a fix renders: wrong on the keyframe too, sharpen the
`ui` description, `plan`, `redo --keyframe`; only in the video, `redo` the segment with a `--prompt` naming the
element and its exact text. A `ui` item without a box is checked only by the whole-video review.

The wipe check (only when the user asked to remove a logo: game logos and warning lines are kept by default and
not reported) looks at every corner a logo or warning line was removed from, over its whole time: per boxed `remove`
item a sheet `review/<id>-wipes-r<n>-<k>.jpg` of eight moments (upper row the original with the logo, lower row the
delivered video), and Gemini reports leftover letters (`leftover_overlay`), objects copied in from beside the box,
upside-down objects, smears and rectangles (`artifact`, source `gemini-wipe`). The measured check (`assemble`'s
`wipes`, source `measured`) is the reliable one for smears: on the 1080p batch the blurred corners scored
0.1-0.38 and the clean ones about 1.05.

Issues come in nine categories:

| Category | Means |
| --- | --- |
| `continuity` | continuity error: something differs from the original (an object changes shape, colour or count, things appear or vanish, motion no longer matches) |
| `artifact` | bug: morphing, melting, flicker, jitter, frozen frames, garbled HUD text or numbers, strong noise, a blurred or smeared patch where a logo was |
| `added_text` | captions, warning lines, watermarks or garbled characters the original does not have |
| `seam_color` | colour jump / scene change at a seam: colour, brightness, texture, terrain, cable colours or realism jump; source `measured`, `gemini-seam` (close-up) or `gemini` |
| `leftover_overlay` | an element listed in `remove` still visible (game logos and warning lines are kept by default) |
| `end_card` | closing-card content still visible |
| `style_drift` | the look changes between parts of the video |
| `identity` | a recurring character or object looks different in another part of the video, compared with the styled keyframes; a gradual drift counts |
| `not_realistic` | a large area kept the original's flat cartoon rendering (outlines, flat fills, plastic); shapes kept as in the original do not count |

Each issue lists the segments it falls in (`S01:K03`). Gemini is a second pair of eyes, not the judge.

## Confirm on stills

For every high issue look at its stills in `review/issues/<id>-r<n>-p<k>.jpg` (more moments: `frames --source <id>
--at <start> [<end>]`): the upper row is the original, the lower row the result, at T-0.3 s, T and T+0.3 s. The triage
hint is a second opinion only. Decide:

- **Confirmed**: the bottom row shows the flaw and the top row does not. Note what exactly is wrong.
- **Dismissed**: the original shows the same thing (game physics, clipping, motion blur), the flaw is not visible on
  the stills, or it is the intended material change. Note why.
- For a seam issue look at the seam time itself and 0.3 s on either side; a step in colour or texture between the
  middle and one side is a confirmed seam issue. The seam entry also names the moment where the later segment's own
  frames start (`inside`): look there too.
- Medium and low issues (and realism scores) are reported in the summary; they are not redone.

## Redo

`redo --segment S01:K03 --reason "<what is wrong>" [--prompt "<one corrective English sentence>"] [--keyframe]`

Redo only what a viewer sees at once: a visible seam, a smeared or leftover logo, garbled or added text, an object
that morphs, vanishes or appears, a prominent object in the wrong colour.

- `--prompt` is added to that segment's video prompt for every later attempt; keep it to one sentence about the
  confirmed flaw, describing the look wanted, e.g. `The fish keep the shape and number they have in the original video.`,
  `The colours match the previous segment; the snow is white.`.
- Name only the part that changes and what must stay, with the moment in seconds into the segment (the segment's
  start is in `reports/plan.md`). Colour words leak onto whatever is nearby: `the grab arm turns from blue to red` turned the
  tuna the arm was holding red; `About 16 s into this segment, after the golden upgrade flash, only the grab arm's
  metal post and boom turn to red paint; the tuna being lifted and those in the river keep their blue-white and silver
  bodies.` fixed it. Describe the look wanted, never the look to avoid: naming cartoon tree crowns to forbid them made
  Seedance draw them.
- Corrective sentences accumulate over attempts. When one made things worse, `redo ... --replace-prompt --prompt
  "<new sentence>"` drops the earlier ones (never edit `config/redo.json` by hand).
- `--keyframe` when the keyframe already shows the flaw (wrong objects, logo kept, colour cast): run
  `keyframes --redo S01:K03` and check the sheet before rendering.
- A seam jump: look at `sheets/<id>-seams.jpg` and the stills at the seam time, then redo the segment after the
  seam (its clip starts again with the previous result's last second; the later segments of that shot follow). A
  jump where that segment's own frames start means Seedance restyled its own part differently from the lead it was
  given: one redo; if it comes back, `"chain_prefix": false` for the source or a `split_at` at a stiller moment, then
  `plan` and redo.
- A seam measured `visible` while the seam sheet shows the same scene on both sides and the Gemini close-up calls it
  consistent: check whether the original itself changes abruptly there (an effect bursting, a stack of bills flying
  off, a flash). The measure counts that motion as a jump. Confirm it on `frames --source <id> --at <seam time>`;
  if so, dismiss it with that reason.
- Only when the user asked to remove a logo: a smeared logo corner (an item with `"wipe": "before"`): drop the `wipe` (Seedance is then told to remove the logo
  itself), `plan`, redo the segments it spans. A logo Seedance kept: redo once with a `--prompt` naming the corner.
  A warning line along the bottom edge it always keeps: cropping the source is the fix (ask the user first).
- Garbled screen-fixed text or UI: make its `ui` item exact (quoted text, look, place, times), `plan`, then redo
  the keyframe (wrong there too) or the segment with a `--prompt` naming it. After two failed redos report it as
  open; nothing is burned in.
- Then `run` (only redone segments render, with their checks; assembly and review follow per source). Each review
  round is numbered; keep going until a round shows no visible flaw (no limit by default; a work order's
  `review_rounds` caps it), then deliver and list the medium and low issues.

## Held segments (segment check)

`run`/`render` checks every segment that has a successor in its shot before the successor starts: the seam to the
segment before it and the segment's wiped logo corners are measured, and Gemini compares the segment with its
original (same categories, the `ui` elements included). A visible seam, a smeared corner or a high
Gemini finding holds the segment; the rest of the shot waits, other shots go on. Confirm on
`review/gate/<id>-<Kxx>-p<k>.jpg` like a review issue: `redo` (one segment renders again instead of the whole shot) or
`accept --segment <key> --reason <why>`. An accepted finding (and its reason) is passed to the later checks, so the
same flaw continuing in the next segments does not hold them again.

- `segment check failed (...); continuing unchecked` (a Gemini error): the successor renders without the check; look
  at that segment's frames (`frames` after assembly) before you trust the review alone.
