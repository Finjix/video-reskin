# Commands and file formats

All job commands: `& "<skill_root>/scripts/video_reskin.cmd" <command> --job-dir <job_dir>`. Times assume a warm
runtime cache; the first use adds the downloads (`runtime --feature media|login [--install]` inspects or prefetches).

| Command | Does | Typical time |
| --- | --- | --- |
| `auth [--key K]` | stores the Winky key; checks the key and the Lumos login | seconds |
| `login [--no-open] [--timeout 300]` | Feishu QR login to Lumos; prints `qr: <image>` for every new code until scanned | until scanned |
| `init --work-order -` | validates the order (JSON on stdin or a file), creates the delivery folder and job dir | seconds |
| `analyze [--only S01]` | media facts, camera cuts, camera speed, end-card candidate, still overlays, sheets | ~10 s per source |
| `survey [--only S01] [--redo]` | Gemini watches each whole source: scene views and subjects (re-boxed on stills) → `catalog/survey/<id>.json`, `catalog/<id>-survey.jpg` | ~1 min per source |
| `plan --edits <json>` | stores the edits, cuts each kept range into segments → `config/plan.json`, `reports/plan.md` | < 1 s |
| `refs [--only K] [--redo K]` | scene maps (GPT Image 2.6, 4K, high) and three-view sheets (1K, medium) → `refs/`, `sheets/<id>-refs.png` | 2-6 min per scene map, ~1 min per sheet, 3 at a time |
| `render [--only K] [--workers 4]` | Seedance 2.5 video edit of every segment at 1080p → `ai/`; segments of one shot one after the other (each gets the previous result's frame where it begins, `refs/chain/`), shots side by side; results that leave the source's camera are rejected and rendered once more | minutes per segment, 4 at a time |
| `assemble [--only S01] [--seams auto\|cut\|dissolve]` | retime frame by frame, colour-match, switch at seams inside shots with a plain cut or, where a cut would show, a light dissolve (`cut` / `dissolve`: always, to compare), join with the source audio → delivery folder | ~1 min per video |
| `review [--only S01] [--fps F]` | Gemini watches original and result side by side → `reports/review/<id>-r<n>.md/json`; agent stills → `review/stills/<id>-r<n>/` (every 3 s), `review/seams/<id>-<Kxx>.png` (5 frames per seam) | 2-4 min per video |
| `frames --source S01 --at T [T...]` | stills: original (top) vs restyled (bottom) at T-0.3, T, T+0.3 s → `review/frames/` | seconds |
| `redo --segment S01:K02 --reason R [--prompt P] [--replace] [--refs]` | sends confirmed segments back (twice per segment at most); `--replace` drops the earlier corrective sentences | - |
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
  textures, brightness and grading looked up from `assets/styles/realistic.jpg`) or an object with `label`, `goal`,
  `look`, `scene`, `subject`, `video`, `default_materials` and optionally `reference` (a look picture, absolute or
  relative to `assets/`) like the preset, for another look. All prompt texts are English. The reference picture goes
  with every scene map as its last image (look only, nothing copied from its content).
- `options.max_segment_seconds`: 4-30 (default 30; Seedance 2.5 edits clips of 2-30 s, the result as long as the
  input). `render_resolution`: `1080p` (default) or `720p`, what Seedance renders at; deliveries are 1080p either way.
  `fps`: output frame rate (default 24, Seedance's rate, never above the source).

## Catalog (`analyze`)

`catalog/catalog.md` per source: size, frame rate, duration, shots (`#2 59.58-63.58`), the end-card candidate
(`cut` or `transition`, or none) and still overlays (`O1 top-right box [x0, y0, x1, y1] 0.0-17.8 s`, fractions of
the frame). Still overlays are candidates only: logos and warning lines (remove) and HUD counters (keep) both show
up, scene objects that stand still too. `catalog/<id>.jpg` shows a frame every 1-2 s with cuts marked and overlay
boxes in red; `catalog/<id>-end.jpg` the last 12 s every 0.5 s. `catalog/motion/<id>-camera.json` holds the
camera's screen speed per frame (frame widths/heights per second), used to place seams.

## Survey (`survey`)

`catalog/survey/<id>.json`: `scenes` (`name`, `start`, `end`, `views`: 3-8 times whose frames together show the
whole location) and `subjects` (`what`, `kind`, `appears` from_start/later, `start`, `end`, `at`: a moment it is
clearly in view, `box`: [x0, y0, x1, y1] fractions of that frame, found again by Gemini on the still).
`catalog/<id>-survey.jpg` shows every view and every crop. Correct them in the edits when a view is blurred or
covered or a crop misses its subject.

## Edits (`plan --edits`)

```json
{"defaults": {"extra": "雪地保持洁白。"},
 "sources": {
  "S01": {"keep": [0, 59.08],
          "materials": ["清澈的河水和浮冰", "积雪地面", "木板平台和原木围栏", "整条的金枪鱼", "穿冬季服装的人物",
                        {"what": "绿色的纸币堆", "start": 12.5, "end": 59.08}],
          "remove": [{"what": "右上角的游戏logo「WHITEOUT SURVIVAL」", "start": 0, "end": 36}],
          "subjects": [{"what": "蓝衣猎人主角", "kind": "character", "start": 0, "end": 59.2, "at": 7.75,
                        "box": [0.42, 0.38, 0.58, 0.52]}]},
  "S02": {"keep": [0, 61.0], "style_from": "S01"}}}
```

- `keep`: `[start, end]` seconds of the source to deliver (at least 4 s). The end is the last moment before the end
  card (its transition included). Default: up to the end-card candidate, else the whole source.
- `materials`: the real-world materials of what this video shows, in Chinese, each prominent object named as itself:
  one text, or a list whose items are texts or `{"what": text, "start": s, "end": s}` for objects on screen only part
  of the time. Each prompt names only the items on screen during its segment. Never list a material the video does
  not show or one another object could turn into (`三文鱼` turned stacks of whole tuna and of cash into salmon
  steaks).
- `remove`: on-screen elements to delete, in Chinese, each naming what it shows (`游戏logo「…」`,
  `画面底部的白色小字警示语`); merged with `defaults.remove`; text or a timed item. List only what the sheets show.
  Never list HUD counters, prices, labels or buttons.
- `scenes`: replaces the survey's scenes: `[{"name": text, "start": s, "end": s, "views": [seconds, ...]}]` (1-12
  views). `subjects`: replaces the survey's subjects (at most 8): `[{"what", "kind", "start", "end", "at", "box"}]`
  with `kind` one of character, animal, vehicle, object, prop, building. Only views and subjects inside `keep` are
  used.
- `style_from`: another source whose scene map gives this source's scene maps their look (sister cuts of one ad).
- `extra`: one short Chinese sentence added to every prompt of the source (e.g. `雪地保持洁白，不要偏紫`).
- `split_at`: extra split times (seconds) when a segment boundary must hit a particular moment.
- `max_segment_seconds`: per-source override (4-30); `skip: true` leaves a source out.

## Plan (`config/plan.json`, `reports/plan.md`)

Each source lists its segments `K01...` with `frames` (source frames it delivers), `render_frames` (what is sent to
Seedance: the delivered frames plus 0.3 s on either side of a seam inside a shot, padded to 4 s when shorter),
`render_seconds`, `seam` before it (`start`; `cut`: a camera cut hides the join; `continuous`: inside a shot, with
`seam_score`, how fast the camera moves there against the rest of the video, and `seam_camera`), `inner_cuts`, and
its references: `scenes` and `subjects` (indices, at most two, subjects first seen in the segment first; only
these sheets are made). Edit the edits file, not the plan.

## Outputs

- Delivery folder: `<source name>_写实.mp4` per source (1080 on the short side, H.264, AAC, the source audio of the
  kept range).
- `temp/refs/<id>-scene1.png`, `<id>-P01.png`; their inputs in `temp/refs/inputs/`; `temp/sheets/<id>-refs.png`.
- `temp/ai/<id>-<Kxx>.mp4`: Seedance results as delivered (24 fps, 1080p, slightly shorter than the clip); `temp/work/` the retimed clips and the
  seam switches. `temp/state/render/<id>-<Kxx>.json` `follow`: how well the result follows the source clip (matched /
  typical difference of edges and of coarse layouts; both above 0.93 / 0.80 means it left the source).
- `temp/state/assembled/<id>.json`: `seams` (inside a shot: the switch moment, `mode` cut/dissolve, `ratio` and
  `local_ratio`: the jump at the switch frame against the ordinary frame change, overall and in the worst region,
  `same`: how far apart both drawings are there, 0-255; a plain cut passes up to 1.15 / 1.2 / 30), `colour` (raw difference and residual per
  seam inside a shot, 0-255), `timing` (per segment: the constant `speed` and `offset` the clip plays at, frames `held`/`skipped` where the
  picture stands still, `camera_error`: camera on screen vs the source's, pixels at 270 wide, `typical`
  edge difference), segment times in the delivered video.
- `temp/reports/review/<id>-r<n>.md`: each issue with severity, category, times, segments and description.
