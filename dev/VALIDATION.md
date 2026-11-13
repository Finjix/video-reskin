# Validation record

The evidence behind the skill's current rules and thresholds. Each finding names the run it comes from; findings
measured with a model the skill no longer uses are marked, so they can be checked again with Seedance 2.5.

## Runs

| Date | Footage | Setup | Outcome |
| --- | --- | --- | --- |
| 2026-11-03 | `.素材`: GOF3D10232 射冰取鱼 1080x1920, GOF3D10374 锤木过桥卖炭 1920x1080, GOF3D10057 甩枪获鱼 1080x1920 | Seedance 2.5 `video_edit` 1080p on per-segment keyframes (since replaced by scene maps and sheets) | 2 of 3 passed; open: whole tuna drawn as salmon steaks, a stack of thin logs drawn as one trunk |
| 2026-11-09 | GOF3D10232, 0-30 s, one long shot | MiniMax H3 (dropped 2026-11-10) with survey, one scene map, three-view sheets, 3 segments | round 3 without high issues; open (medium): a tuna about a second early, a carried cash stack melting |
| 2026-11-10 | GOF3D10176 | Seedance 2.5 with the scene map and sheets | seams of parallel segments jumped; led to chaining and constant-speed retiming |

## Findings behind current rules

**Seedance timing.** Results come back 24 fps and slightly shorter than the input with the motion compressed
evenly (5.00 s → 4.71 s; source time = 1.069 × output time; 2026-11-03). Retiming: following an edge-map match
frame by frame froze 187 of 495 moving frames and played half-second stretches at half speed; a speed fitted on
camera velocity left two seams a second apart (a still camera fits any timing); ffmpeg `minterpolate` returned the
same frame for 157 frames of K01 (all GOF3D10176, 2026-11-10). Hence one constant speed per clip pinned at its seams,
holds and skips where the picture stands still, no interpolation. The warp's hold/skip cost scales with how much the
best match beats the typical one: scaled to the typical difference it trailed 1.25 s under a strong restyle
(GOF3D10232, 2026-11-09).

**Chaining inside a shot.** Segments rendered side by side from the same scene map differed too much at the seams
(GOF3D10176, 2026-11-10, Seedance). A segment that continues a shot now takes the previous result's frame where it
begins, so the segments of one shot render one after the other.

**Colours.** Two results of the same moment differ by 7-30 levels of mean brightness; a chained match drifted, a
least-squares gain/offset per clip over each run of in-shot seams left residuals of 0.03-0.45 (2026-11-03) and
0.10-0.14 (2026-11-09).

**Seams.** Plain-cut thresholds (`assemble.CUT_*`): a switch at 1.00 / 0.99 / 24-25 (jump against the frame
change, worst region, drawings apart) showed only an ice block popping in; one at 1.14 / 1.05 / 49 jumped in
brightness, in how the crowd and fence are drawn, and a bench of fish steaks vanished, so it gets the 6-frame
dissolve (GOF3D10232, 2026-11-09).

**Prompt wording** (Seedance, 2026-11-03). Naming text that is not on screen (a warning line in a source without
one) made the model draw garbled warning text there; untimed materials (a pickup truck present only at the end) put
trucks into the opening; `salmon` in the materials turned whole tuna and cash stacks into salmon steaks. Hence timed
`remove` and `materials` items, "the list only describes materials, do not add anything because of it" and "do not
add any new text" in every prompt, and the `added_text` review category.

**Scene maps.** Views taken late in a build-up video (2-63.5 s) gave a map of the finished camp (fish rack, steak
tower, cash, lit pot), and the video model drew them from 9 s on; views of the still empty camp (2-18 s, the crowd
named in `materials`) fixed it for every segment, only a small prop on the map still leaked into the first seconds
(GOF3D10176, 2026-11-09, H3). Hence the survey takes each area as early as it is clearly in view. Early views
alone were not enough while the map prompt named every material of the scene's whole time range: GOFCN48484's
dock map (views 7.5-42.5 s) drew the road, pickup trucks, sharks and jet ski that arrive at 50-70 s, and the
conveyor robots of the shot before it (a material ending at the scene's start); naming only the materials on screen
between the first and last view removed them all (2026-11-10). The survey put that cut at 7.2 s, the catalog at
7.30 s, so each segment beside it took both scene maps: the dock segment drew the conveyor and robot arms over the
bay, the conveyor segment a fence and stairs from the dock. A scene or subject now needs 0.5 s inside a segment to
send its picture along. A 20 MB 4K PNG
timed out when Lumos fetched it as a reference (2026-11-09), so pictures over 8 MB go as JPEG and a scene map used
only as a look reference goes at 2048 px.

**Survey boxes.** Boxes read off the video missed two characters by a body width; boxes found again on the still
were tight (GOF3D10232, 2026-11-09).

**Sheets and the camera** (H3, GOF3D10232, 2026-11-09; not yet re-checked with Seedance). With 4 sheets K01 cut to
a close-up of its own in three of four attempts; with the scene map alone it kept the source's camera at the first
try with the same realism, and an early tuna on an empty pad was gone. Hence at most two sheets per segment and the
automatic retry with the scene map only. A corrective sentence about where things are (`keep the collection box
empty`) made K01 cut to a close-up from 4 s on, hence `redo --replace`. A prompt alone left H3's picture cartoon:
the look comes from the pictures.

**Follow check** (`assemble.FOLLOW_*`, H3, GOF3D10232, 2026-11-09; not yet re-checked with Seedance). Matched /
typical difference of edges and coarse layouts: results that followed 0.71-0.92 / 0.50-0.61, results that left the
source 0.97-0.98 / 0.83-0.89.

**Review.** Gemini (`gemini-3.8-flash`) found every defect later confirmed on stills except one garbled caption it
described as a ground change (2026-11-03). On a small crf-30 proxy it passed a visible 0.5 s dissolve and cartoon
trees, hence near-lossless clips filling up to the 256,000,000-byte inline limit (a 60 s 2160x1920 side-by-side
clip of about 200 MB was accepted, 2026-11-06). Gemini samples an inline video on its side at
`videoMetadata.fps`, not at the clip's frame rate: at 4 frames/s rounds 1-2 of GOFCN64128 抢鱼之熊 (2026-11-10) and
the K03 check passed a conveyor whose fish stood still while the belt ran (51.5-55.3 s, seen by the user in the
delivery), a belt item moving about one spacing per 0.25 s. Sampling every frame (24/s) on Winky: a 55.3 s, 207 MB
clip took 339,761 prompt tokens (about 256 per frame at media resolution HIGH, 2026-11-11); with every frame and the
`motion` category and movement sentence in the prompt (round 4) Gemini still did not report the standing fish, so
the frame rate is necessary, not sufficient, and motion of evenly spaced items is not yet caught by any check.
`GEMINI_FPS` 6 for every clip whatever its length is the user's ceiling (2026-11-11): about 1,540 tokens per second
of video, a 55 s review about 85k tokens. The agent stopped reading the whole video every 3 s (20 full-size pairs a
round here, 24 more per set of segment checks) and reads one three-frame sheet per reported issue, the seam sheets
and the handoff frames.

**Per-segment gate, no whole-video review** (user rule, 2026-11-11). On GOFCN64128 抢鱼之熊 every flaw the
whole-video rounds found had been in a segment check or at a seam already (the warning text K02 carried over from
K01's handoff frame, the cash drawn early in K03), and each round cost a 207 MB, 86-340k-token clip. So the seam is
judged when the later segment is checked, on a preview of the segments joined so far (the same retimed clips as the
delivery; colour matching is solved again per shot at the end, residuals of about 0.05 here), Gemini also sees the
handoff frame as a still, approval needs a passed check of the current result (or a written dismissal), and a
segment may go back three times.

**Items riding a conveyor** (GOFCN64128 抢鱼之熊 K03, Seedance 2.5, 2026-11-11, judged by the user on the videos).
The fish lying on the running belt stood still in every result: with the conveyor sheet and a motion sentence
("Every fish on the conveyor belt rides along with the belt at the belt's speed, exactly as the input video moves
them."), with that sentence plus the fisherman's sheet, and with the sentence and no conveyor sheet (that attempt
also lost the pine forests along the belt). The belt part on its own (`split_at` 51.0, a 4.6 s K04) did not move
them either, sent at the source's 60 fps or brought down to 24 fps first (`CLIP_FPS`, kept: the model gets its own
rate). A stronger sentence ("The conveyor belt runs fast toward the counter, and every fish on it slides quickly
along with the belt in every frame, exactly as fast as in the input video: no fish on the belt ever stands still.")
made the belt items move, but the model drew salmon steaks on the belt (the materials list them) and lost the crowd;
naming them `whole tuna` brought the tuna and the crowd back (crowd sheet instead of the fisherman's) and the tuna
stood still again, twice. A motion strip (three crops 0.05 s apart) did not move them and pulled the result into a
close-up of the counter from 53.9 s (the follow check passed it: only the last 1.2 s left the framing). In the
source each tuna moves one spacing every 0.15 s, so the belt looks the same every 0.15 s; a video model that steps
through time in about that interval would see a belt standing still (a guess, not measured). Untried: the belt
part sent slowed down and the result sped up again. Without a carried sheet the fisherman was once redrawn as a
polar bear in his parka (polar bears are on screen in that segment): hence `sheets` in the edits.

**Survey and refs checks** (GOFCN64128 抢鱼之熊, 2026-11-11). The survey's one watch (6 frames/s, 95k tokens) also
named the end card (55.6-55.67 s against the measured 55.32 s and the transition the agent judged at about 55.3 s: the
strip around it is still for the agent to confirm), the logo and the warning lines; it gave the logo as top-left for
the whole video though it moves to the bottom corners at 15 s, hence one strip per removal (start, middle, end) and
analyze's still-box span beside Gemini's (0-15 s here). The still check (14k tokens for 12 pictures) caught the
P03 crop that showed tuna instead of the polar bear and the late 52.3 s view with the conveyor; told only "later
things", it also flagged views because characters stood in them, hence "late" defined as the place itself changed.
The refs check (7 pictures, about 25k tokens) flagged the scene map's queue area drawn as pine forest (the cause of
the crowd vanishing from K01's first result), the cash sheet drawn as one stack instead of two, and the conveyor
sheet's back view; one note (fish drawn in the water) was wrong, the source shows them.

**Segment gate** (`render.Waiting`, `check`/`approve`; GOFCN64128 抢鱼之熊, 2026-11-10). Without it one `render`
run chained a whole shot: a K01 redo kept the bottom warning text, K02 was drawn from that frame and kept the text
for its whole 28 s, and K02/K03, clean in the previous round, were re-rendered only because their chained frame
changed (both regressed; three Seedance generations lost). A per-segment `check` on the K03 result found, beside the
agent's stills, the missing pine forest along the conveyor; stills and the Gemini clip line up with the source.

**Which sheets** (`plan.assign_refs`; GOFCN64128 抢鱼之熊, 2026-11-10, Seedance 2.5). Picked by first appearance,
K01 took player and tuna (both from 0 s), K02 and K03 the conveyor (on screen 0.2 s of K02) and the player again,
whose look K02's chained frame already carried; the cash stacks (sheet never made), the salmon steaks (not listed
by the survey: `materials` text only), the guards and the yellow crowd stayed in the game's look for the whole
video, the sheeted tuna came out realistic. Hence sheets ranked by seconds on screen times box area, the subjects
the previous result drew from a sheet last, and the survey listing different forms of one thing (whole fish and
steaks, goods and cash) and produced or carried items as subjects of their own. With seven subjects and two sheets
per segment some still go without one: a third sheet with Seedance is not yet tested.

**Known limitation.** Fast-changing HUD counters are redrawn by the video model and may show other digits for a few
frames.

## Offline checks

`dev/test_bootstrap.py` (cold start in a Unicode temp path, JSON stdin, offline reuse) and `dev/run_tests.py` pass.

## Remake / schema 2 — 2026-11-12

Offline validation uses the managed runtime and mocks all Lumos/Winky operations. `test_remake.py` exercises a complete two-segment reference/render/check/approve cycle with real FFmpeg media, interrupted submission recovery without duplicate generations, exact last-frame chaining, 2-second padding and final source-clock assembly. Separate synthetic media verifies 30→24 fps normalization, original non-game/end-card intervals, audio, 1080p delivery, and a six-frame duration-preserving blend. Gate tests cover seam-first failure, transition decisions before application without post-transition review, stale approvals, two shared quality retries, reused references and missing new-prop coverage.

The pre-remake 62 tests remain as regressions for retained internal helpers and the pre-existing uncommitted approved-result preservation fix. Schema-2 tests validate the new public entry point's orchestration separately. No paid model calls were made; visual generation quality and live service acceptance remain unverified.
