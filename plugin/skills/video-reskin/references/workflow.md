# Commands and file formats

All job commands: `& "<plugin_root>/scripts/video_reskin.cmd" <command> --job-dir <job_dir>`. Times assume a warm
runtime cache; the first use adds the downloads (`runtime --feature media|login [--install]` inspects or prefetches).

| Command | Does | Typical time |
| --- | --- | --- |
| `auth [--key K]` | stores the Winky key; checks the key and the Lumos login | seconds |
| `login [--timeout 300]` | Feishu QR login to Lumos (opens the QR image; refreshes it when it expires) | until scanned |
| `init --work-order -` | validates the order (JSON on stdin or a file), creates the delivery folder and job dir | seconds |
| `analyze [--only S01]` | media facts, camera cuts, end-card candidate, still overlays, sheets | ~10 s per source |
| `lint [--edits <json>] [--only S01]` | Gemini lists objects (colours, times), logos and warning lines, screen-fixed text, HUD, live-action stretches and the end card; checks the edits; drafts `config/edits.draft.json` → `catalog/lint.md`, `catalog/<id>-lint.jpg` | ~1 min per source |
| `plan --edits <json>` | stores the edits, cuts each kept range into segments → `config/plan.json`, `reports/plan.md` | < 1 s |
| `keyframes [--only K] [--redo K]` | anchor + the keyframe of every shot's first segment (GPT Image on Lumos, quality high, 2K) → `keyframes/`, `sheets/<id>-keyframes.jpg` | ~1 min per image, 3 at a time |
| `preview --only S01 --at T [T...] [--redo]` | GPT Image previews of at most 4 moments of one source, with the texts the video prompt uses there and the segment's corrective sentences; only looked at → `sheets/<id>-preview.jpg` | ~1-2 min per image |
| `render [--only K] [--workers 4] [--no-gate]` | Seedance 2.5 video edit of every segment at 1080p → `ai/`; segments of one shot one after another, each later one starting with the previous result's last second, longest shots first; each segment with a successor is checked first (seam and wiped-corner measures + Gemini) and held when flawed | several minutes per segment, 4 shots at a time |
| `run [--only K] [--no-gate]` | keyframes (stops when any are new, to be looked at); then render with checks, and assemble and review each source as soon as its segments are done (unchanged videos are not reviewed again) | as render |
| `accept --segment S01:K02 --reason R` | lets a held segment through (the check was wrong, as seen on its stills) | - |
| `cost [--json]` | Seedance seconds by cause (new, redo, upstream, auto-retry, plan, keyframe, wipe, unusable), resolution and source; GPT Image and Gemini calls | - |
| `assemble [--only S01]` | retime, colour-match, switch at the best-matching frame, copy the HUD back, join with the source audio → delivery folder (1080p, 24 fps); seam and wiped-corner measurements, `sheets/<id>-seams.jpg` | ~1 min per video |
| `review [--only S01] [--fps F]` | Gemini compares original and result (4 fps up to 75 s, else 2), reports its realism, checks that every recurring character and object keeps its keyframe look (close-ups → `review/<id>-identity-r<n>-subjects.jpg`), checks every seam and wiped corner close up; measured seams and corners added; stills of every issue with a Gemini triage hint → `reports/review/<id>-r<n>.md/json`, `review/issues/` | 1-2 min per video |
| `frames --source S01 --at T [T...]` | stills: original (top) vs restyled (bottom) at T-0.3, T, T+0.3 s → `review/frames/` | seconds |
| `redo --segment S01:K02 --reason R [--prompt P] [--replace-prompt] [--keyframe]` | sends confirmed segments back for another attempt; corrective sentences accumulate over attempts, `--replace-prompt` drops the earlier ones first | - |
| `compare [--only S01] [--layout left\|top] [--with <video>...] [--labels <text>...]` | the delivered video beside its original (original left or top, each side at the original's size, delivered audio) → `<name>_写实_对比.mp4` in the delivery folder; `--with` puts earlier versions on the delivered clock (a previous delivery) between the two, `--labels` captions each column (original, each `--with`, result); only when the user asks for a comparison | ~20 s per minute of video, more with `--with` |
| `status` | progress per source and the next step | - |

Keys: a source is `S01`; its anchor keyframe `S01:anchor`; a segment `S01:K02` (its keyframe has the same key).

## Work order (`init`)

```json
{"sources": ["D:/footage/ad1.mp4", "D:/footage/batch2"],
 "request": "<the user's request, verbatim>",
 "output_root": "<optional folder; default: the user's Desktop>",
 "delivery_dir": "<optional: the exact folder the user named for the videos, instead of output_root>",
 "style": "realistic",
 "options": {"max_segment_seconds": 30, "fps": 24, "review_rounds": 2}}
```

- `delivery_dir`: when the user names the folder the videos go to (`成片放 D:\成片`), the deliveries go exactly
  there and the job into its `temp/`; it must be new or empty. Otherwise `output_root` gets a new
  `视频换风格_YYYYMMDD_HHMMSS/` folder.
- `sources`: video files (.mp4/.mov/.mkv/.m4v) or folders (their videos, not recursive).
- `style`: a preset name from `assets/prompts.json` (`realistic`: 写实版材质，风格不变) or an object with `label`,
  `goal`, `anchor`, `keyframe`, `video` and `default_materials` like the preset, for another look (optional:
  `look` and `style_ref`, the texts the `{look}` and `{style_ref}` placeholders take). `realistic` keeps layout,
  shapes, proportions, positions, counts, motion and colours and makes the materials real.
- Resolution is fixed: Seedance renders 1080p and the deliveries are 1080 on the short side (`options.resolution`
  accepts only `1080p`). `fps`: output frame rate, 12-30 (default 24, the rate Seedance renders at; never above the
  source). `max_segment_seconds`: 4-30 (default 30; fewer, longer segments mean fewer seams). `review_rounds`:
  default 2. `chain_prefix`: default true (see Plan); false sends chained segments only the previous result's frame.

## Catalog (`analyze`)

`catalog/catalog.md` per source: size, frame rate, duration, shots (`#2 59.58-63.58`), the end-card candidate
(`cut` or `transition`, or none) and still overlays (`O1 top-right box [x0, y0, x1, y1] 0.0-17.8 s`, fractions of
the frame). Still overlays are candidates only: logos and warning lines (remove) and HUD counters (keep) both show
up, scene objects that stand still too. `catalog/<id>.jpg` shows a frame every 1-2 s with cuts marked and overlay
boxes in red; `catalog/<id>-end.jpg` the last 12 s every 0.5 s.

## Edits (`plan --edits`)

```json
{"defaults": {"extra": "The snow stays white."},
 "sources": {
  "S01": {"keep": [0, 65.7],
          "materials": ["clear river water and drifting ice", "snow-covered ground", "a plank platform and log fences",
                        "whole tuna", "people in winter clothes",
                        {"what": "green stacks of banknotes", "start": 16, "end": 65.7},
                        {"what": "a blue pickup truck and an asphalt road", "start": 56, "end": 65.7}],
          "remove": [{"what": "the game logo \"WHITEOUT SURVIVAL\" in the top-right corner", "start": 0, "end": 36, "box": [0.73, 0.02, 1.0, 0.11]},
                     {"what": "the game logo \"WHITEOUT SURVIVAL\" in the bottom-left corner", "start": 35, "end": 65.7, "box": [0.0, 0.85, 0.36, 1.0]}],
          "hud": [{"box": [0.0, 0.099, 0.28, 0.152], "what": "the coin counter at the top left"}],
          "anchor": 36.5},
  "S02": {"keep": [0, 61.0], "style_from": "S01"},
  "S03": {"like": "S01",
          "remove": [{"what": "the game logo \"WHITEOUT SURVIVAL\" in the top-left corner", "box": [0.0, 0.0, 0.3, 0.06]}],
          "hud": [{"box": [0.0, 0.05, 0.3, 0.09], "what": "the coin counter at the top left"}]}}}
```

- `keep`: `[start, end]` seconds of the source to deliver. The end is the last moment before the end card (its
  transition included). Default: up to the end-card candidate, else the whole source.
- `materials`: the real-world materials of what this video shows, in English, each prominent object named as itself
  with its colour (`whole tuna`, `green stacks of banknotes`, `yellow ropes`, `snow-covered ground`...): one text, or a list whose items are
  texts or `{"what": text, "start": s, "end": s}` for objects that appear only part of the time. Each prompt names
  only the items on screen during its keyframe or segment; the preset's generic list is the default. Never list a
  material the video does not show or one another object could turn into: a list naming salmon turned stacks of
  whole tuna and of green cash into salmon steaks, and an untimed `blue pickup truck` (trucks arrive near the end) put
  a truck into the opening seconds.
- `remove`: on-screen elements to delete, in English, each naming what it shows (`the game logo "…" in the top-right
  corner`, `the small white warning text at the bottom of the frame`); merged with `defaults.remove`. An item is text, or `{"what": text, "start": s,
  "end": s}` when the element is on screen only for part of the source. List only what the sheets show: naming text
  that is not there makes Seedance draw garbled text in its place. Never list HUD counters, prices, labels or buttons.
  - A screen-fixed logo or warning line gets `"box": [x0, y0, x1, y1]` (fractions, a little larger than the
    element; the catalog's still-overlay boxes are a good start, check them on a frame). By default
    (`"wipe": "none"`) it stays in the clip and every prompt names it for removal during its time; in a
    chained segment the lead frames get the source's pixels under its mask painted back in (`work/logos/`,
    a mask of the pixels that stay still in the box while the scene moves), so it is in the clip from the
    first frame like in a first segment. The box also drives the review's wipe check sheets.
  - `"wipe": "before"`: the letters are wiped from the clip (filled from the scene around them) and the video
    prompt only says where the patch is. Seedance kept such patches as blurred smears at 1080p and 720p
    (`assemble` measures them: `wipes`), so this is not for logos.
  - A warning line along the bottom edge stays whatever is done (Seedance keeps screen-fixed layers);
    cropping it off a copy of the source is the clean fix (see the skill), after asking the user.
  - Time the items to the frame where the logo moves (step through stills a tenth of a second apart): a logo that
    jumps corners at 36.65 s with items switching at 37.5 s stayed visible in the new corner for most of a second. A
    box that overlaps a segment's render span by 0.1 s or more (`reports/plan.md`) renders that segment again when
    the box changes.
- `anchor`: the source time whose frame sets the look (default: a lively moment of the longest shot). Pick a frame
  that shows the main scene sharply, without transitions or pop-ups.
- `hud`: screen-fixed UI whose numbers change (coin counters, timers, scores): `{"box": [x0, y0, x1, y1],
  "start": s, "end": s, "what": text}` with the box in fractions of the frame, a little larger than the element.
  Seedance redraws such digits wrongly; `assemble` copies the pixels that stay still inside the box (icon, panel and
  the digits they enclose) back from the original, frame by frame. Only screen-fixed elements.
- `overlays`: screen-fixed, flat 2D text and UI the program burns in after the restyle, so they are always exact
  (captions, labels, banners, buttons the models garble). Each item:
  `{"what": "the caption \"Caught a big tuna!\" at the bottom", "poly": [[x, y], ...] | "box": [x0, y0, x1, y1], "start": s, "end": s,
  "shape": "box" | "ellipse", "colors": ["#ffffff"], "tolerance": 60, "ai": "remove" | "keep"}`, or a drawn one
  with `"text": "钓到大金枪鱼！"` and `"color"`, `"stroke"`, `"fill"` (`#rrggbb[aa]`), `"font"` (`bold`, `regular`,
  `heavy` or a font file).
  - Region: `box` or `poly` (points, fractions of the frame), hugging the element: whatever it holds comes from the
    original (or the drawn text) instead of the restyle. A translucent plate keeps the original's tint, like the HUD.
  - `colors`: only original pixels close to these colours come back (`tolerance`, RGB distance, default 60), e.g.
    white letters over a moving scene.
  - `ai`: by default (`remove`) `what` joins the removals of the keyframe and video prompts during `start`-`end`,
    so the models leave the element out and no garbled copy shows around the exact one. `keep` lets the models draw
    it underneath (only when the burn covers it fully).
  - Only screen-fixed, flat elements. UI standing in the scene (floor price tags, bubbles over characters, signs in
    perspective) and text with complex motion stay with the models. Never add text or UI the original does not show.
- `style_ref`: a texture reference for the anchor keyframe: an image path, or `{"path": <image or video>, "at":
  <seconds, for a video>, "box": [x0, y0, x1, y1]}` (fractions; crop away logos, warning lines and objects that could
  be copied). Stored as `keyframes/<id>-styleref.png`; keyframes and segments follow the anchor.
- `style_from`: another source whose styled anchor this source's anchor follows (sister cuts of one ad).
- `live`: stretches that are not game footage (live-action video of real people, cars, landscapes or weather; real
  photos; a real human face pasted onto a game object): `[{"start": s, "end": s, "what": text}]`, not overlapping,
  timed to the hard cuts around them (an end within 0.25 s of a camera cut snaps onto it). Each becomes a segment of
  its own (`"live": true` in the plan, hard cuts on both sides) whose frames `assemble` takes from the original; no
  keyframe, no Seedance, no segment check, and the review leaves its issues and realism out. The anchor never lies in
  one. Gameplay shorter than 4 s next to a live span is refused (Seedance takes 4 s at least): let the live span cover
  it. `remove` items over a live span stay visible there (`reports/plan.md` notes them). A `like` twin takes `live`
  too. To leave a live-action opening out of the delivery instead, start `keep` after it.
- `like`: the same ad in another size (the portrait of a landscape source; a separate framing, not a crop). The
  source takes the named twin's `keep`, `materials`, `extra`, `anchor`, `split_at`, `max_segment_seconds`,
  `chain_prefix` and `live` (its own keys win; an inherited `keep` ends at its own length) and `style_from` the twin; `remove`,
  `hud` and `overlays` are its own, since the boxes sit elsewhere in another framing. The twin must have its own edits,
  no `like` and no `skip`; lengths more than 0.3 s apart need an own `keep` (another cut: check the times). Each
  segment spanning the same seconds as one of the twin's (within 0.1 s, the same seam kind) also takes that segment's
  corrective sentences (`redo --prompt`, listed first; `redo --replace-prompt` on the segment drops them).
  `render` and `run` leave the source out until the twin's delivery is current, reviewed since its last change and has
  no redo queued; `--only <id>` renders it anyway. `reports/plan.md` lists which segments take the twin's fixes.
- `extra`: one short English sentence added to every prompt of the source (e.g. `The snow stays pure white.`).
- `split_at`: extra split times (seconds) when a segment boundary must avoid or hit a particular moment (keep a seam
  off an effect burst or a fast camera move). Inside a shot it is a chained seam like any other. Corrective
  sentences name seconds into a segment: after moving a split, recompute them from `reports/plan.md`.
- `chain_prefix`: per source, overrides the job option (false: chained segments get only the previous result's frame).
- `max_segment_seconds`: per-source override; `skip: true` leaves a source out.

## Plan (`config/plan.json`, `reports/plan.md`)

Each source lists its segments `K01...` with `frames` (source frames it delivers), `render_frames` (what is sent
to Seedance: the delivered frames plus 1 s before a seam inside a shot), `seam` before it (`start`, `cut`: a camera
cut hides the join, `continuous`: inside a shot) and `inner_cuts` (camera cuts inside the segment). A shot longer than
the limit is split into equal pieces at still moments. The segments of one shot form a chain: the first starts from
its keyframe; each later one's clip is: the shot's first 0.5 s (the frames its keyframe was made from), then the
previous result's last second (stretched back to the source clock, the logos Seedance removes painted back in), then
its own frames, with the shot's keyframe as the reference picture. Seedance reads the edit from the clip's first frame
and the reference picture: with the previous result's frame first and a copy of it as reference it read "change
nothing" and left trees cartoon and logos in (GOF3D10012 K02 at 26.4 s); with the opening pair it restyles the own
frames like the first segment and continues the lead's shapes. `assemble` drops the 0.5 s opening, plays the earlier
segment through the shared second and cuts to the later one's own frames, colours matched on the frames on both
sides of the cut: Seedance re-grades the lead it copies (GOF3D10012: the river warmer for 0.9 s between two cooler
parts when assembly switched inside it). A chained clip thus adds 1.5 s within Seedance's 30 s (`seconds` in the plan includes it). (With a reference
picture alone, 8 of the 13 in-shot seams of the 1080p batch measure visible by the current measure.) Redoing a segment also renders the
rest of its chain again. Edit the edits file, not the plan.

## Outputs

- Delivery folder: `<source name>_写实.mp4` per source (1080 on the short side, 24 fps, H.264, AAC, the source audio
  of the kept range); `<source name>_写实_对比.mp4` after `compare`.
- `temp/keyframes/<id>-<key>.png` (styled) and `-src.png` (original frame); `temp/sheets/<id>-keyframes.jpg`.
- `temp/ai/<id>-<Kxx>.mp4`: Seedance results as delivered (24 fps, slightly shorter than the source clip: `assemble`
  stretches them back exactly).
- `temp/state/assembled/<id>.json`:
  - `seams`: `time` of the switch, `mismatch` (mean grey difference of the two versions there), `motion` (the
    picture's own change per frame), `local` (the largest colour change of one of 60 regions beyond its own motion),
    `inside` (the step where the later segment's own frames start, against the steps around it) and `jump`
    (clean, slight or visible, the worse of the two).
  - `wipes`: per wiped logo corner the `share` of fine detail inside the wiped letters against the scene around
    them and the `verdict` (clean, soft below 0.6, smeared below 0.4, plain when the scene is too plain to tell),
    with the moments it was smeared.
  - colour corrections, HUD mattes, overlays (frames they cover), segment times in the delivered video.
- `temp/sheets/<id>-seams.jpg`: the frames on both sides of each switch.
- `temp/reports/review/<id>-r<n>.md`: each issue with severity, category, times, segments, description and triage
  hint; `temp/review/issues/<id>-r<n>-p<k>.jpg`: the stills of every issue (upper row original, lower row restyled).
- `temp/review/gate/<id>-<Kxx>-p<k>.jpg`: stills of a held segment's findings; the verdict is in
  `state/render/<id>-<Kxx>.json` (`gate`).
- `temp/state/ledger.jsonl`: one line per paid call (Seedance segment, GPT Image keyframe or preview, Gemini check)
  with its cause; `cost` sums it.
