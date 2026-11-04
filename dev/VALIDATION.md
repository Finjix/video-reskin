# Validation record

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

## 2026-11-03 (later): seam consistency, HUD digits, stronger review (GOF3D10057 only)

The user found the scene changing at the 11 s seam of GOF3D10057 (cable grey then yellow, other rocks and deck
texture) with a visible dissolve. Cause: every segment was imagined anew from its own keyframe, and the 0.5 s
cross-fade blended two different pictures.

- Seedance 2.5 `video_edit` accepts 20 and 30 s clips (19.71 / 29.71 s back, timing linear within 15 ms, look stable
  to the end): segments now run up to 30 s, split evenly, so 65.7 s needs 3 segments and 2 seams instead of 6 and 5.
- Segments of one shot are chained: the next starts from the previous result's frame at its first moment. The
  seam frames then match (measured grey jump 4.5-5 against 19-25 for the independent version).
- The switch happens at the best-matching overlap frame with a 4-frame blend instead of a 0.5 s dissolve.
- Seam QC: whole-frame jump plus the largest colour change of one of 60 regions (`local`); clean seams 2.5-7.9,
  a fish turning yellow to silver 16.8, the old independent seams 46-78. Gemini also checks every seam close up
  (3 s at 10 fps). On the old video the review now reports the 11 s seam three ways; on the chained one both seams
  pass all three.
- HUD: the coin counter is copied back from the original through a matte of its still pixels (icon, panel and the
  digits they enclose); digits now match the original frame by frame (AI had drawn 300/121 for 229/95).
- Prompts now say to keep every object's own colour (the cable had turned red, the crane flickered blue/purple);
  colours that change in the original are named as timed `materials`. A redo with a corrective sentence fixed the
  yellowfin tuna drawn silver; chained segments after it re-rendered on their own.
- Final round 3: pass; two Gemini findings dismissed after stills showed the original does the same.
- Rendering one 65.7 s video as a chain takes about 14 min (3 × 4.5-5 min, one after another).
- `assemble` refuses clearly when the delivered video is open in a player; `review` refuses to judge a video older
  than its segments.
