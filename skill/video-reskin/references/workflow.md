# Commands and file formats

All job commands: `& "<skill_root>/scripts/video_reskin.cmd" <command> --job-dir <job_dir>`. Times assume a warm
runtime cache; the first use adds the downloads (`runtime --feature media|login [--install]` inspects or prefetches).

| Command | Does | Typical time |
| --- | --- | --- |
| `auth [--key K] [--forget-login]` | stores the Winky key; checks the key and the Lumos login | seconds |
| `login [--no-open] [--timeout 300] [--check]` | Feishu QR login to Lumos; prints `qr: <image>` for every new code until scanned | until scanned |
| `init --work-order -` | validates the order (JSON on stdin or a file), creates the delivery folder and job dir | seconds |
| `analyze [--only S01]` | media facts, camera cuts, camera speed, end-card candidate, still overlays, sheets | ~10 s per source |
| `survey [--only S01] [--redo]` | Gemini watches each whole source (6 frames/s): scene views, subjects (re-boxed on stills), the end card, overlays to remove and HUD, non-game stretches (live action); then checks the views and crops as stills → `catalog/survey/<id>.json` (with `suggested` edits and `checks`), one marked still per removal, one boundary strip per non-game stretch and the end-card strip in `catalog/survey/<id>/`, all on `catalog/<id>-survey.jpg` | 1-2 min per source |
| `plan --edits <json>` | stores the edits, cuts each kept range into segments → `config/plan.json`, `reports/plan.md` | < 1 s |
| `refs [--only K] [--redo K]` | scene maps (GPT Image 2.6, 4K, high) and three-view sheets (1K, medium) → `refs/`; Gemini checks each new picture against its inputs → `reports/refs/<id>.md`, flagged ones side by side with their inputs in `refs/check/<key>.png`; all on `sheets/<id>-refs.png` | 2-6 min per scene map, ~1 min per sheet, 3 at a time |
| `render [--only K] [--workers 4]` | Seedance 2.5 video edit at 1080p of each segment's clip, cut from the source at 24 fps (`CLIP_FPS`: a faster source is brought down locally, not by the model) → `ai/`: the first segment of every shot, and each segment whose previous one in the shot is approved (it gets that result's frame where it begins, `refs/chain/`); the rest report `waiting`; shots side by side; results that leave the source's camera are rejected and rendered once more | minutes per segment, 4 at a time |
| `check [--segment K ...]` | the gate of each finished segment not approved yet (or the ones named): Gemini beside its source clip and the frame the next segment starts from, then the seam to the segment before on a preview of the segments joined so far (`work/preview/`), 1 s either side beside the original, with the start frame it was given beside its own first frame (6 frames/s, near-lossless up to 256 MB) → `reports/check/<id>-<Kxx>-a<n>.md/json`; for the agent one three-frame sheet per issue, and the seam's five-frame sheet and the full-size handoff frame where Gemini found nothing there → `review/check/<id>-<Kxx>-a<n>/` | 2-3 min per segment |
| `approve --segment S01:K02 [...] [--dismiss R]` | approves the current result of segments whose check of that result passed (a failed one only with `--dismiss`, why each issue is wrong; a source ID: all its finished segments); the next `render` continues their shots | - |
| `assemble [--only S01] [--seams auto\|cut\|dissolve]` | retime each clip at one constant speed, colour-match, switch at seams inside shots with a plain cut or, where a cut would show, a light dissolve (`cut` / `dissolve`: always, to compare), put the original footage back at each `passthrough` range (plain cuts), join with the source audio → delivery folder; there is no review after it | ~1 min per video |
| `frames --source S01 --at T [T...]` | stills of the delivery: original (top) vs restyled (bottom) at T-0.3, T, T+0.3 s → `review/frames/` | seconds |
| `redo --segment S01:K02 --reason R [--prompt P] [--replace] [--refs]` | sends confirmed segments back (five times per segment at most) and drops their approval; approved later segments of the shot keep their results (check them again: their seam changed); `--replace` drops the earlier corrective sentences | - |
| `status` | progress per source and the next step | - |

Keys: a source is `S01`; a segment `S01:K02`; a scene map `S01:scene1`; a subject sheet `S01:P02`.

## Work order (`init`)

```json
{"sources": ["D:/footage/ad1.mp4", "D:/footage/batch2"],
 "request": "<the user's request, verbatim>",
 "output_root": "<optional folder; default: the user's Desktop>",
 "style": "realistic",
 "options": {"max_segment_seconds": 30, "fps": 24, "render_resolution": "1080p"}}
```

- `sources`: video files (.mp4/.mov/.mkv/.m4v) or folders (their videos, not recursive).
- `style`: a preset name from `assets/prompts.json` (`realistic`: realistic materials, style unchanged; pronounced
  textures and a detailed ground, brightness and grading looked up from `assets/styles/realistic.jpg`) or an object
  like the preset, for another look: `label` (the delivery's name suffix), `look`, `default_materials`, the `scene`,
  `subject` and `video` templates, optionally `ground` (scene maps and videos only), `references` (what each
  reference picture of a video edit is) and `reference` (a look picture, absolute or relative to `assets/`); missing
  templates fall back to the preset's. All prompt texts are English. The look picture goes with every scene map as
  its last image (look only, nothing copied from its content).
- `options.max_segment_seconds`: 4-30 (default 30; Seedance 2.5 edits clips of up to 30 s, the result as long as the
  input). `render_resolution`: `1080p` (default) or `720p`, what Seedance renders at; deliveries are 1080p either way.
  `fps`: output frame rate (default 24, Seedance's rate, never above the source).

## Catalog (`analyze`)

`catalog/catalog.md` per source: size, frame rate, duration, shots (`#2 59.58-63.58`), the end-card candidate
(`cut` or `transition`, or none) and still overlays (`O1 top-right box [x0, y0, x1, y1] 0.0-17.8 s`, fractions of
the frame). Still overlays are candidates only: logos, warning lines and HUD counters (all kept) show
up, scene objects that stand still too. `catalog/<id>.jpg` shows a frame every 1-2 s with cuts marked and overlay
boxes in red; `catalog/<id>-end.jpg` the last 12 s every 0.5 s. `catalog/motion/<id>-camera.json` holds the
camera's screen speed per frame (frame widths/heights per second), used to place seams.

## Survey (`survey`)

`catalog/survey/<id>.json`: `scenes` (`name`, `start`, `end`, `views`: 3-8 times whose frames together show the
whole location, each area as early as it is clearly in view) and `subjects` (`what`, `kind`, `appears`
from_start/later, `start`, `end`, `at`: a moment it is clearly in view, `box`: [x0, y0, x1, y1] fractions of that
frame, found again by Gemini on the still), `end_card` (`start`: the last frame before its transition, `how`; null
when there is none), `overlays` (`id`, `kind` logo/warning/watermark/caption/hud, `text` as on screen, `what`: the
English phrase for the prompt, `where`, `start`, `end`, `at`, `box`, `remove`: true only for watermark/caption candidates; false for logo/warning/hud),
`non_game` (`start`, `end`, `what`: live action, real people, filmed hands or phones, non-game animation; a
small picture-in-picture over gameplay does not count), `non_game_stills` (per stretch: just before, at its start,
at its end and just after), `overlay_stills` (one marked frame per removal), `end_strip` (0.5 s before, at and after the end-card start),
`checks` (the views and crops Gemini flagged on the stills: `problem` blurred/covered/late/wrong_subject/partial,
`note`, `path`) and `suggested` (`keep`, `passthrough` and timed `remove` for the edits). `catalog/<id>-survey.jpg` shows every
view and every crop. Correct them in the edits where a flag holds (a view blurred, covered or late, the place
already full, or a crop that misses its subject).

## Edits (`plan --edits`)

```json
{"defaults": {"extra": "Keep the snow white."},
 "sources": {
  "S01": {"keep": [0, 59.08], "passthrough": [[0, 3.4]],
          "materials": ["clear river water with floating ice", "packed snow ground", "plank dock and log fence",
                        "whole tuna", "people in winter clothing",
                        {"what": "stacks of green banknotes", "start": 12.5, "end": 59.08}],
          "remove": [],
          "subjects": [{"what": "hunter in a blue coat", "kind": "character", "start": 0, "end": 59.2, "at": 7.75,
                        "box": [0.42, 0.38, 0.58, 0.52]}]},
  "S02": {"keep": [0, 61.0], "style_from": "S01"}}}
```

All texts are English: they go into the model prompts.

- `keep`: `[start, end]` seconds of the source to deliver (at least 4 s). The end is the last moment before the end
  card (its transition included). Default: up to the end-card candidate, else the whole source.
- `passthrough`: `[[start, end], ...]` seconds inside `keep` that are not gameplay (a real person talking to the
  camera, filmed hands on a phone, non-game animation), confirmed on the survey's non-game strips: `start` its first
  frame, `end` the first gameplay frame after it. They are not rendered or checked; the gameplay between them is
  segmented on its own (a cut seam at each boundary, renders never reaching into them) and `assemble` puts the
  original footage back in place with the source audio, so the delivery keeps the source's length and timing.
  Gameplay between two ranges (or between a range and the end of `keep`) must last at least 4 s: add a shorter bit
  to the range. Views and subject moments inside these ranges are dropped; removals there do nothing (the original
  stays as it is).
- `materials`: the real-world materials of what this video shows, each prominent object named as itself: one text,
  or a list whose items are texts or `{"what": text, "start": s, "end": s}` for objects on screen only part of the
  time. Each prompt names only the items on screen during its segment. Never list a material the video does not
  show or one another object could turn into (`salmon` turned stacks of whole tuna and of cash into salmon steaks).
- `remove`: on-screen elements to delete; merged with `defaults.remove`; text or a timed item. List only confirmed removal candidates. Never list original logos, warning lines, HUD counters, prices, labels or buttons. Clear legacy logo/warning removals from both defaults and sources when reusing edits. Generation prompts must not mention logos or warning lines, even to say to keep them.
- `scenes`: replaces the survey's scenes: `[{"name": text, "start": s, "end": s, "views": [seconds, ...]}]` (1-12
  views). `subjects`: replaces the survey's subjects (at most 8): `[{"what", "kind", "start", "end", "at", "box"}]`
  with `kind` one of character, animal, vehicle, object, prop, building. Only views and subjects inside `keep` are
  used.
- `style_from`: another source whose scene map gives this source's scene maps their look (sister cuts of one ad).
- `extra`: one short sentence added to the source's scene-map and video prompts (e.g. `Keep the snow white, never
  purple.`).
- `split_at`: extra split times (seconds) when a segment boundary must hit a particular moment.
- `sheets`: `{"K03": [8, 1]}` names the sheets a segment takes (subject numbers as in `reports/plan.md`, at most
  two), instead of the ones that fill it most: when a subject the segment was to carry from the previous result's
  frame was redrawn as something else (the fisherman drawn as a polar bear). Other segments keep their sheets.
- `motion`: `{"K04": {"what": "whole tuna on the conveyor belt", "at": 53.0, "box": [x0, y0, x1, y1], "step": 0.05}}`
  gives a segment one more reference picture: three crops of the source (`box`, fractions of the frame) `step` s
  apart from `at`, side by side (`refs/motion/<id>-<Kxx>.png`), as a motion reference only. Choose `step` well
  below the time the moving pattern takes to repeat (belt items one spacing on: else the crops look alike), and
  look at the strip before rendering. A tight crop can pull the video model into a close-up: keep `box` wide.
- `max_segment_seconds`: per-source override (4-30); `skip: true` leaves a source out.

## Plan (`config/plan.json`, `reports/plan.md`)

Each source lists its segments `K01...` with `frames` (source frames it delivers), `render_frames` (what is sent to
Seedance: the delivered frames plus 0.3 s on either side of a seam inside a shot, padded to 4 s when shorter),
`render_seconds`, `seam` before it (`start`; `cut`: a camera cut hides the join; `continuous`: inside a shot, with
`seam_score`, how fast the camera moves there against the rest of the video, and `seam_camera`), `inner_cuts`, and
its references: `scenes` and `subjects` (indices, at most two: the subjects that fill the segment most, seconds
on screen times box area; in a segment that continues a shot, the ones the previous result already drew from a
sheet come last; only these sheets are made). Edit the edits file, not the plan.

## Outputs

- Delivery folder: `<source name>_<style label>.mp4` per source (`_realistic` by default; 1080 on the short side,
  H.264, AAC, the source audio of the kept range).
- `temp/refs/<id>-scene1.png`, `<id>-P01.png`; their inputs in `temp/refs/inputs/`; the chained start frames in
  `temp/refs/chain/`; `temp/sheets/<id>-refs.png` (only the sheets some segment takes); Gemini's check of each
  picture in `temp/state/refs-check.json` (kept until the picture changes), `temp/reports/refs/<id>.md`, and each
  flagged one beside its inputs in `temp/refs/check/<key>.png`.
- `temp/ai/<id>-<Kxx>.mp4`: Seedance results as delivered (24 fps, 1080p, slightly shorter than the clip);
  `temp/work/` the retimed clips and the dissolves. `temp/state/render/<id>-<Kxx>.json` `follow`: how well the
  result follows the source clip (matched / typical difference of edges and of coarse layouts; both above 0.93 /
  0.80 means it left the source). `temp/config/approved.json`: the result (generation) approved per segment.
- `temp/reports/check/<id>-<Kxx>-a<n>.md/json` and `temp/review/check/<id>-<Kxx>-a<n>/`: each segment's check, per
  attempt (issues in delivered-video seconds, each with its `part` segment/seam and its `sheet`; `seam`: the join
  to the segment before, its `verdict`, the measured `mode`/`ratio`/`same`, the `preview` it was judged on and its
  `sheet` when Gemini found nothing there, or `pending`; `handoff`: where the next segment of the shot starts from,
  `handoff_still` its full-size frame when no issue covers it). `temp/config/dismissed.json`: why a failed check was
  approved. `temp/work/preview/<id>-upto-<Kxx>-<result>.mp4` (one per result of that segment): the segments joined up to that one.
- `temp/state/assembled/<id>.json`: `seams` (inside a shot: the switch moment, `mode` cut/dissolve, `ratio` and
  `local_ratio`: the jump at the switch frame against the ordinary frame change, overall and in the worst region,
  `same`: how far apart both drawings are there, 0-255; a plain cut passes up to 1.15 / 1.2 / 30), `colour` (raw
  difference and residual per seam inside a shot, 0-255), `timing` (per segment: the constant `speed` and `offset`
  the clip plays at, frames `held`/`skipped` where the picture stands still, `camera_error`: camera on screen vs
  the source's, pixels at 270 wide, `typical` edge difference), segment times in the delivered video.
