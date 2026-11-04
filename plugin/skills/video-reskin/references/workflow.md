# Commands and file formats

All job commands: `& "<plugin_root>/scripts/video_reskin.cmd" <command> --job-dir <job_dir>`. Times assume a warm
runtime cache; the first use adds the downloads (`runtime --feature media|login [--install]` inspects or prefetches).

| Command | Does | Typical time |
| --- | --- | --- |
| `auth [--key K]` | stores the Winky key; checks the key and the Lumos login | seconds |
| `login [--timeout 300]` | Feishu QR login to Lumos (opens the QR image; refreshes it when it expires) | until scanned |
| `init --work-order -` | validates the order (JSON on stdin or a file), creates the delivery folder and job dir | seconds |
| `analyze [--only S01]` | media facts, camera cuts, end-card candidate, still overlays, sheets | ~10 s per source |
| `plan --edits <json>` | stores the edits, cuts each kept range into segments → `config/plan.json`, `reports/plan.md` | < 1 s |
| `keyframes [--only K] [--redo K]` | anchor + the keyframe of every shot's first segment (GPT Image on Lumos) → `keyframes/`, `sheets/<id>-keyframes.jpg` | ~1 min per image, 3 at a time |
| `render [--only K] [--workers 4]` | Seedance 2.5 video edit of every segment at the job resolution (480p by default while developing; 720p or 1080p for delivery) → `ai/`; segments of one shot one after another, each from the previous result | 5-10 min per segment, 4 shots at a time |
| `assemble [--only S01]` | retime, colour-match, switch at the best-matching frame, copy the HUD back, join with the source audio → delivery folder; seam measurements and `sheets/<id>-seams.jpg` | ~1 min per video |
| `review [--only S01]` | Gemini compares original and result, then checks every seam close up; measured seam jumps added → `reports/review/<id>-r<n>.md/json` | 1-2 min per video |
| `frames --source S01 --at T [T...]` | stills: original (top) vs restyled (bottom) at T-0.3, T, T+0.3 s → `review/frames/` | seconds |
| `redo --segment S01:K02 --reason R [--prompt P] [--keyframe]` | sends confirmed segments back for another attempt | - |
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
  `goal`, `anchor`, `keyframe`, `video` and `default_materials` like the preset, for another look.
- `options.max_segment_seconds`: 4-30 (default 30, Seedance 2.5 edits clips of 2-30 s and keeps their look over the
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
  kept range).
- `temp/keyframes/<id>-<key>.png` (styled) and `-src.png` (original frame); `temp/sheets/<id>-keyframes.jpg`.
- `temp/ai/<id>-<Kxx>.mp4`: Seedance results as delivered (24 fps, slightly shorter than the source clip: `assemble`
  stretches them back exactly).
- `temp/state/assembled/<id>.json`: seams (`time` of the switch, `mismatch`: mean grey difference of the two versions
  there, `motion`: the picture's own change per frame, `local`: the largest colour change of one of 60 regions beyond
  its own motion, so one object changing colour counts; `jump`: clean, slight or visible), colour corrections, HUD
  mattes, segment times in the delivered video. `temp/sheets/<id>-seams.jpg`: the frames on both sides of each switch.
- `temp/reports/review/<id>-r<n>.md`: each issue with severity, category, times, segments and description.
