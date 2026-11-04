# Commands and file formats

All job commands: `& "<plugin_root>/scripts/video_reskin.cmd" <command> --job-dir <job_dir>`. Times assume a warm
runtime cache; the first use adds the downloads (`runtime --feature media|login [--install]` inspects or prefetches).

| Command | Does | Typical time |
| --- | --- | --- |
| `auth [--key K]` | stores the Winky key; checks the key and the Lumos login | seconds |
| `login [--timeout 300]` | Feishu QR login to Lumos (opens the QR image; refreshes it when it expires) | until scanned |
| `init --work-order -` | validates the order (JSON on stdin or a file), creates the delivery folder and job dir | seconds |
| `analyze [--only S01]` | media facts, camera cuts, end-card candidate, still overlays, sheets | ~10 s per source |
| `lint [--edits <json>] [--only S01]` | Gemini lists objects (colours, times), logos and warning lines, screen-fixed text, HUD and the end card; checks the edits; drafts `config/edits.draft.json` → `catalog/lint.md`, `catalog/<id>-lint.jpg` | ~1 min per source |
| `plan --edits <json>` | stores the edits, cuts each kept range into segments → `config/plan.json`, `reports/plan.md` | < 1 s |
| `keyframes [--only K] [--redo K]` | anchor + the keyframe of every shot's first segment (GPT Image on Lumos) → `keyframes/`, `sheets/<id>-keyframes.jpg` | ~1 min per image, 3 at a time |
| `preview [--only S01\|S01:K02] [--at T...] [--redo]` | GPT Image previews of frames across every segment with the texts the video prompt uses there and the segment's corrective sentences (looked at; the `midframes` among them also go to Seedance) → `sheets/<id>-preview.jpg`; `--at`: these times instead → `sheets/<id>-preview-at.jpg`; `--redo`: make them again even when current; new previews get the identity check (`sheets/<id>-preview-identity-subjects.jpg`) | ~2 min per image, 6 at a time |
| `render [--only K] [--workers 4] [--no-gate]` | Seedance 2.5 video edit of every segment and patch at the job resolution (480p by default while developing; 720p or 1080p for delivery) → `ai/`; segments of one shot one after another, each from the previous result, longest shots first; each segment with a successor is checked first (seam measure + Gemini) and held when flawed | 5-10 min per segment, 4 shots at a time |
| `run [--only K] [--no-gate] [--no-preview]` | keyframes and previews (stops when any are new, to be looked at; `--no-preview` still makes the midframes); then render with checks, and assemble and review each source as soon as its segments are done (unchanged videos are not reviewed again) | as render |
| `accept --segment S01:K02 --reason R` | lets a held segment through (the check was wrong, as seen on its stills) | - |
| `patch --segment S01:K02 --at T1 [T2] --reason R [--prompt P]` / `patch --drop S01:K02:P1` | queues a local patch: 4-8 s of a finished segment render again from its own frame, the rest stays | - |
| `cost [--json]` | Seedance seconds by cause (new, redo, upstream, auto-retry, patch, plan, keyframe, unusable), resolution and source; GPT Image and Gemini calls | - |
| `assemble [--only S01]` | retime, colour-match, switch at the best-matching frame, copy the HUD back, join with the source audio → delivery folder; seam measurements and `sheets/<id>-seams.jpg` | ~1 min per video |
| `review [--only S01] [--fps F]` | Gemini compares original and result (4 fps up to 75 s, else 2) and rates its realism, checks that every recurring character and object keeps its keyframe look (close-ups → `review/<id>-identity-r<n>-subjects.jpg`), then checks every seam close up; measured seam jumps added; stills of every issue with a Gemini triage hint → `reports/review/<id>-r<n>.md/json`, `review/issues/` | 1-2 min per video |
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
 "style": "realistic",
 "options": {"resolution": "480p", "max_segment_seconds": 30, "fps": 30, "review_rounds": 3}}
```

- `sources`: video files (.mp4/.mov/.mkv/.m4v) or folders (their videos, not recursive).
- `style`: a preset name from `assets/prompts.json` (`realistic`: 写实版材质，风格不变) or an object with `label`,
  `goal`, `anchor`, `keyframe`, `video` and `default_materials` like the preset, for another look (optional:
  `look`, `rebuild` and `style_ref`, the texts the `{look}`, `{rebuild}` and `{style_ref}` placeholders take).
  `realistic` keeps layout, positions, counts, motion and colours and rebuilds each object's appearance as its
  real-world counterpart (`rebuild`).
- `options.max_segment_seconds`: 4-30 (default 30, Seedance 2.5 edits clips of 4-30 s and keeps their look over the
  whole clip; fewer, longer segments mean fewer seams). `fps`: output frame rate (default 30, never above the source).

## Catalog (`analyze`)

`catalog/catalog.md` per source: size, frame rate, duration, shots (`#2 59.58-63.58`), the end-card candidate
(`cut` or `transition`, or none) and still overlays (`O1 top-right box [x0, y0, x1, y1] 0.0-17.8 s`, fractions of
the frame). Still overlays are candidates only: logos and warning lines (remove) and HUD counters (keep) both show
up, scene objects that stand still too. `catalog/<id>.jpg` shows a frame every 1-2 s with cuts marked and overlay
boxes in red; `catalog/<id>-end.jpg` the last 12 s every 0.5 s.

## Edits (`plan --edits`)

```json
{"defaults": {"extra": "雪地保持洁白。"},
 "sources": {
  "S01": {"keep": [0, 65.7],
          "materials": ["清澈的河水和浮冰", "积雪地面", "木板平台和原木围栏", "整条的金枪鱼", "穿冬季服装的人物",
                        {"what": "绿色的纸币堆", "start": 16, "end": 65.7},
                        {"what": "蓝色皮卡车和柏油路面", "start": 56, "end": 65.7}],
          "remove": [{"what": "右上角的游戏logo「WHITEOUT SURVIVAL」", "start": 0, "end": 36},
                     {"what": "左下角的游戏logo「WHITEOUT SURVIVAL」", "start": 35, "end": 65.7}],
          "hud": [{"box": [0.0, 0.099, 0.28, 0.152], "what": "左上角金币计数"}],
          "anchor": 36.5},
  "S02": {"keep": [0, 61.0], "style_from": "S01"}}}
```

- `keep`: `[start, end]` seconds of the source to deliver. The end is the last moment before the end card (its
  transition included). Default: up to the end-card candidate, else the whole source.
- `materials`: the real-world materials of what this video shows, in Chinese, each prominent object named as itself
  (`整条的金枪鱼`, `绿色的纸币堆`, `原木`, `积雪地面`...): one text, or a list whose items are texts or
  `{"what": text, "start": s, "end": s}` for objects that appear only part of the time. Each prompt names only the
  items on screen during its keyframe or segment; the preset's generic list is the default. Never list a material
  the video does not show or one another object could turn into: a list naming `三文鱼` turned stacks of whole tuna
  and of green cash into salmon steaks, and an untimed `蓝色皮卡车` (trucks arrive near the end) put a truck into
  the opening seconds. Name the colour of prominent objects (`黄色的缆绳`, `黄色的黄鳍金枪鱼`): "realistic" otherwise
  tempts the models to recolour them, and a chained segment may then drift (a red crane flickering blue).
- `remove`: on-screen elements to delete, in Chinese, each naming what it shows (`游戏logo「…」`,
  `画面底部的白色小字警示语`); merged with `defaults.remove`. An item is text, or `{"what": text, "start": s,
  "end": s}` when the element is on screen only for part of the source: a prompt only names the items on screen
  during its keyframe or segment. List only what the sheets show: naming text that is not there makes Seedance
  draw garbled text in its place. Never list HUD counters, prices, labels or buttons.
- `anchor`: the source time whose frame sets the look (default: a lively moment of the longest shot). Pick a frame
  that shows the main scene sharply, without transitions or pop-ups.
- `hud`: screen-fixed UI whose numbers change (coin counters, timers, scores): `{"box": [x0, y0, x1, y1],
  "start": s, "end": s, "what": text}` with the box in fractions of the frame, a little larger than the element (the
  catalog's still-overlay boxes are a good start). Seedance redraws such digits wrongly; `assemble` copies the pixels
  that stay still inside the box (icon, panel and the digits they enclose) back from the original, frame by frame.
  Only screen-fixed elements: price tags or labels that move with the scene stay restyled.
- `overlays`: screen-fixed, flat 2D text and UI the program burns in after the restyle, so they are always exact
  (captions, labels, banners, buttons the models garble). Each item:
  `{"what": "底部字幕「钓到大金枪鱼！」", "poly": [[x, y], ...] | "box": [x0, y0, x1, y1], "start": s, "end": s,
  "shape": "box" | "ellipse", "colors": ["#ffffff"], "tolerance": 60, "ai": "remove" | "keep"}`, or a drawn one
  with `"text": "钓到大金枪鱼！"` and `"color"`, `"stroke"`, `"fill"` (`#rrggbb[aa]`), `"font"` (`bold`, `regular`,
  `heavy` or a font file).
  - Region: `box` or `poly` (points, fractions of the frame), hugging the element: whatever it holds comes from the
    original (or the drawn text) instead of the restyle. A translucent plate keeps the original's tint, like the HUD.
  - `colors`: only original pixels close to these colours come back (`tolerance`, RGB distance, default 60), e.g.
    white letters over a moving scene.
  - `ai`: by default (`remove`) `what` joins the removals of the keyframe and video prompts during `start`-`end`,
    so the models leave the element out and no garbled copy shows around the exact one; write `what` as the
    element's plain name. `keep` lets the models draw it underneath (only when the burn covers it fully).
  - Add overlays before `keyframes`: the removal is in the keyframes and renders.
  - Only screen-fixed, flat elements. UI standing in the scene (floor price tags, bubbles over characters, signs in
    perspective) and text with complex motion (zooms, bounces, 3D) stay with the models: a pasted copy does not
    keep up with the moving scene. Never add text or UI the original does not show.
- `style_ref`: a texture reference for the anchor keyframe (the look the user points at): an image path, or
  `{"path": <image or video>, "at": <seconds, for a video>, "box": [x0, y0, x1, y1]}` (fractions; crop away logos,
  warning lines and objects that could be copied). It goes along as the anchor's second picture, with a sentence to
  learn only its realism, texture, light and colour grading, never its content; stored as
  `keyframes/<id>-styleref.png`. Keyframes and segments follow the anchor; Seedance never gets it.
- `midframes`: preview times (source seconds) whose previews go to Seedance as extra reference pictures for the
  segment that contains them, so the realism of that moment holds (a list, or `"auto"`: the regular preview moments
  of every segment). At most 3 per segment, at least 2 s after its first rendered frame; each comes with a sentence
  naming its second in the segment and telling the model not to draw its objects before the original shows them.
  For segments that drift back to the game look later on (toy trucks arriving, a crowd, flat snow), never for the
  first seconds (the keyframe or chain covers them). Adding or moving midframes renders the segment again (cost
  cause `keyframe`); the previews they use must be right, so look at them (`(Seedance reference)` on the sheet).
  Patches do not get them.
- `style_from`: another source whose styled anchor this source's anchor follows (sister cuts of one ad).
- `extra`: one short Chinese sentence added to every prompt of the source (e.g. `雪地保持洁白，不要偏紫`).
- `split_at`: extra split times (seconds) when a segment boundary must avoid or hit a particular moment.
- `max_segment_seconds`: per-source override; `skip: true` leaves a source out.

## Plan (`config/plan.json`, `reports/plan.md`)

Each source lists its segments `K01...` with `frames` (source frames it delivers), `render_frames` (what is sent
to Seedance: the delivered frames plus 0.5 s before a seam inside a shot, where both versions overlap), `seam` before
it (`start`, `cut`: a camera cut hides the join, `continuous`: inside a shot) and `inner_cuts` (camera cuts inside
the segment). A shot longer than the limit is split into equal pieces at still moments. The segments of one shot form
a chain: the first starts from its keyframe, each later one from the previous result's frame at its first moment,
so redoing a segment also renders the rest of its chain again. Edit the edits file, not the plan.

## Outputs

- Delivery folder: `<source name>_写实.mp4` per source (1080 on the short side, H.264, AAC, the source audio of the
  kept range); `<source name>_写实_对比.mp4` after `compare`.
- `temp/keyframes/<id>-<key>.png` (styled) and `-src.png` (original frame); `temp/sheets/<id>-keyframes.jpg`.
- `temp/ai/<id>-<Kxx>.mp4`: Seedance results as delivered (24 fps, slightly shorter than the source clip: `assemble`
  stretches them back exactly).
- `temp/state/assembled/<id>.json`: seams (`time` of the switch, `mismatch`: mean grey difference of the two versions
  there, `motion`: the picture's own change per frame, `local`: the largest colour change of one of 60 regions beyond
  its own motion, so one object changing colour counts; `jump`: clean, slight or visible), colour corrections, HUD
  mattes, overlays (frames they cover), segment times in the delivered video. `temp/sheets/<id>-seams.jpg`: the frames on both sides of each switch.
- `temp/reports/review/<id>-r<n>.md`: each issue with severity, category, times, segments, description and triage
  hint; `temp/review/issues/<id>-r<n>-p<k>.jpg`: the stills of every issue (upper row original, lower row restyled).
- `temp/review/gate/<id>-<Kxx>-p<k>.jpg`: stills of a held segment's findings; the verdict is in
  `state/render/<id>-<Kxx>.json` (`gate`).
- `temp/state/ledger.jsonl`: one line per paid call (Seedance segment or patch, GPT Image keyframe or preview, Gemini
  check) with its cause; `cost` sums it.

## Patches (`patch`)

A patch renders the flaw plus 0.5 s on both sides (at least 4 s, at most 8 s, kept 0.25 s from the segment's edges
and clear of the next segment's overlap) from the segment's own frame at its first moment, with the segment's video
prompt, the chain sentence, the segment's redo sentences and the patch's `--prompt`. `assemble` switches into the
patch and back at the best-matching overlap frames (colours matched to the segment) and measures both joins like
seams (`S01:K02:P1:in`, `:out` in the seams list and the review). A patch belongs to the segment render it was made
for: when the segment renders again, its patches are dropped.
