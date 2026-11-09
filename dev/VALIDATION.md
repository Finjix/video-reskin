# Validation record

## 2026-11-09: MiniMax H3 with shared scene maps and subject sheets

GOF3D10232 射冰取鱼 (1080x1920, 60 fps), kept 0-30 s, one long shot. Lumos login of the developer.

| Step | Result |
| --- | --- |
| H3 probe, prompt only (6 s, 768P) | composition and motion kept, but the picture stayed cartoon (wood a little darker): the look must come from pictures |
| `survey` (Gemini, 720p proxy, 51 MB) | 1 scene with 6 views, 8 subjects. Boxes read off the video missed two characters by a body width; boxes found again on the stills were tight |
| `refs` scene map (4K, max, 4 views + style picture) | 3200x2560 PNG (20 MB) in about 4 min. Layout of the views kept (river, ice blocks, dock, camp floor, pads, bench, hut, log fence), realistic, no characters, UI or text |
| `refs` 7 three-view sheets (1K, high) | about 1 min each, shapes and colours kept. The first try of one sheet failed: Lumos timed out fetching the 20 MB PNG, so pictures over 8 MB go as JPEG and look references go reduced to 2048 px |
| `render` 3 segments (12 + 12 + 9 s, 2K, 5 pictures each) | all three side by side, done after 13-18 min. 1440x2560, 24 fps, 2-5 % longer than asked, realism far above the prompt-only probe |
| `assemble` | 33 s. Colour residuals at the two seams 0.14 and 0.12; both seams `whip` at 41-46 px of camera motion per frame. No ghosting or jump on the frame-by-frame look; at a 64 px peak the blur read as a deliberate swish, now 48 px at most |
| `review` round 1 | Gemini: 2 medium issues, both confirmed on stills (a tuna drawn early on an empty pad in K01, a carried tuna melting into a dark blob in K03). Seam sheets clean |

Findings that changed the skill:

- H3 is no frame-locked editor. With reference pictures it keeps the story and composition but sets its own pace:
  in K01 the camera ran up to 1.7 s ahead and fell back by the end. Even without pictures, a 6 s clip came back
  6.58 s long, on time for 2.5 s, then half a second slow. `assemble` therefore matches every output frame to
  the result frame of the same moment (dynamic time warping on edge maps, holds and skips up to 0.75 s). On the
  stills the retimed clips line up with the source to about a frame. Match quality against the typical frame
  difference is 0.35 for a weak restyle and 0.7-0.92 for a strong one; only values near 1 mean it failed.
- Seams placed where the camera moves fast and hidden under a 9-frame directional blur plus cross-fade showed no
  step between the ±0.5 and ±1 s frames, although the two results differed at the switch by 25-46 levels.
- Segments no longer depend on each other: a redo resubmitted only the redone segments; the third segment was
  reused.

## 2026-11-03: first end-to-end runs on `.素材`

Lumos login of the developer, Winky key `dev`. The key may not use any GPT Image model
(`winky_permission_error`), so every keyframe went through the Lumos image-to-image API with
`gpt-image-2.5-sunburst` (about 50 s per image). Seedance 2.5 `video_edit` at 1080p took 3.5-6 min per 10-12 s
segment, 4 in flight.

| Source | Kept | Segments | Rounds | Result |
| --- | --- | --- | --- | --- |
| GOF3D10232 射冰取鱼 1080x1920 | 0-59.08 s | 6 (all inside one shot) | 3 | open: whole-tuna stacks drawn as salmon steaks (`materials` listed 三文鱼) |
| GOF3D10374 锤木过桥卖炭 1920x1080 | 0-61.0 s | 6 (1 at a cut) | 3 | pass; open (medium): a carried stack of thin logs drawn as one trunk |
| GOF3D10057 甩枪获鱼 1080x1920 | 0-65.7 s | 6 | 2 | pass, 0 issues; logo removed in both corners; seams matched |

Findings that changed the plugin:

- Seedance returns 24 fps clips slightly shorter than the input (5.00 s → 4.71 s) with the whole motion uniformly
  compressed (measured: source time = 1.069 × output time); `assemble` stretches each clip back exactly.
- Two Seedance segments of the same moment differ by 7-30 levels of mean brightness. A chained match drifted;
  a least-squares gain/offset per clip over each run of in-shot seams leaves residuals of 0.03-0.45, with a 0.5 s
  cross-fade on top.
- Naming text that is not on screen (`画面底部的警示文字` in a source without one) makes Seedance draw garbled
  warning text there; untimed materials (`蓝色皮卡车`, present only at the end) put trucks into the opening; an
  anchor from another part of the ad passed as a second reference leaked its coal pile and chimney into other
  segments; `三文鱼` in the materials turned tuna and cash stacks into salmon steaks. Hence timed `remove` and
  `materials` items, keyframe-only video references, "不要添加任何新的文字…" and "下面的列表只说明材质，不要据此添加物体"
  in every prompt, and an `added_text` review category.
- Gemini (`gemini-3.8-flash`, inline side-by-side clip at 2 fps, about 25 s per minute of video) found every
  defect later confirmed on stills except one garbled caption it described as a ground change; its seam reports
  matched the measured colour steps. It accepts 8-11 MB inline clips.
- Known limitation: fast-changing HUD counters are redrawn by Seedance and may show other digits for a few frames.

`dev/test_bootstrap.py` (cold start in a Unicode temp path, JSON stdin, offline reuse) and `dev/run_tests.py`
(19 tests) pass.
