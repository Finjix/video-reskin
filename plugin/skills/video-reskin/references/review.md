# Review loop

The review has two layers that see different things, at two moments.

- **Gemini watches the video.** Every clip it gets is the video itself, uncut, each panel at the delivery's size
  (1080p, crf 18), read at the HIGH media resolution (264 tokens a frame; the default read a frame at 66, too coarse
  for small flaws even in a 1080p clip). It judges motion, continuity and the whole.
- **The agent looks at stills.** Full-size frames (JPEG 95), one moment per picture, the original above the restyle
  (side by side for portrait videos). Garbled letters, soft logo edges, a morphing hand or a colour step show there.
  Each check writes a card listing its stills, what to look for and the report format; sub-agents can take one card
  each.

Right after each segment renders (the segment check, before the next segment of its shot starts from it):
- Gemini: the segment beside its original (6 fps: continuity, bugs, UI, realism over time); when the segment before
  it in the video is rendered, both segments beside their originals in a 2 x 2 grid (one look across them: colour,
  brightness, scenery, subjects; changes the originals show too do not count).
- Agent: every 2 s the original above the segment; every 2 s the previous segment above this one at the same seconds
  into each.
- Measured: the seam to the segment before it inside the shot, wiped logo corners.

In each review round of the assembled video:
- Gemini: the whole video beside its original (4 fps up to 75 s, else 2: drifts across parts and cuts, end card,
  added text, realism timeline); every seam (none beside a live-action stretch): the 4 s around it, original beside
  delivery, at the video's frame rate, with one question: does the restyled picture change there (a jump, a fade, a
  colour or brightness step, other scenery or objects)?
- Agent: per seam, stills every second from 2 s before to 2 s after the switch and the frames on both sides of it
  (and of where the later segment's own frames start), on `review/seams/<id>-<Kxx>/card.md`; the stills of every
  issue.
- Measured: the `look` step (from `assemble`, `look` in the seam entry): for every pair of consecutive frames of the
  whole video, the change of saturation and of colour after the camera's shift (from the original) is taken out and
  the original's own change over the same pair subtracted; the seam's step is scored against the video's ordinary
  pairs (robust z: 8 or more visible, 5 slight). GOF3D10012's 26.4 s seam (the river less saturated, the fish bluer, a
  tree crown at the top edge in the game's cone shape) scored saturation 2.31 against a usual 0.19 (z 9.4); of 1291
  ordinary pairs of that video one reached z 8, in the original's own end-card flash. Seams `assemble` measured as
  `slight` (low) or `visible` (high) and wiped corners measured `soft` (low) or `smeared` (high) are added too.

The goal it judges against is a stable video with real materials and the original's shapes kept, not the most
realistic look. Gemini rates the realism over time (`realism`, 5 = looks like real camera footage, 3 = mixed, 1 =
cartoon) and lists what still looks game-like; the timeline is reported (`realism: 0.0-22.9 s 4/5; 45.6-65.7 s 3/5
(皮卡车)`), and only a range rated 2 or lower becomes a medium `not_realistic` issue. Findings of segment checks the
editor accepted go along to every later check and to the review, which do not report them again.

The identity check keeps every recurring character and object at one look from the first second to the last: Gemini
follows the edits' `subjects` on the original frames (without them it picks the protagonist and up to three more), the program cuts
close-ups of each from the styled keyframes (REF) and from moments across the video, and Gemini judges each pair on
build, rendering and colours (`identity` issues, sheets `review/<id>-identity-r<n>-subjects.jpg`). It runs in the
review only (not per segment or per preview).

The kept-element check holds every `ui` item with a `box` (game logos, warning lines, counters, captions, buttons)
to the original (static items strictly: any blur, soft or haloed edge is high; items marked `dynamic` may blur or
trail a little while they move, which is low; garbled characters are high on both): the review draws a sheet per item, `review/<id>-kept-r<n>-<k>.jpg` (eight moments, upper row
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
--at <start> [<end>]`): the upper row is the original, the lower row the result, at T-0.3 s, T and T+0.3 s. Gemini's
description is a second opinion only. Decide:

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

## Segment and seam cards

`run`/`render` checks every segment as soon as it is rendered and writes `review/segments/<id>-<Kxx>/card.md`; the
next segment of its shot waits until the agent passes it (`accept --segment <key>`) or sends it back (`redo`). A card
is HELD for a visible measured seam, a smeared corner or a high Gemini finding: confirm each on its stills (and
`issue-p<k>.jpg` around the findings); passing it needs `--reason`. An accepted finding (and its reason) is passed to
the later checks and the review, so the same flaw continuing in the next segments is not reported again. Each review
round writes a seam card per join, `review/seams/<id>-<Kxx>/card.md`; pass each with `accept --seam <id>:<Kxx>`
(FLAGGED ones with `--reason`), or `redo` the segment after the seam. A new assembly asks for the seams again.

`checks` lists every card waiting; `checks --wait` blocks until one waits or the running render ends. With
sub-agents, give each a card (several in parallel) and confirm what they report on the same stills before acting.

- `segment check failed (...); continuing unchecked` (an error before the card was written): the successor renders
  without the check; look at that segment's frames (`frames` after assembly) before you trust the review alone.
- A card that says Gemini failed: judge from its stills alone.
