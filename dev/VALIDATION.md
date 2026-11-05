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

## 2026-11-04: spend controls on GOF3D10057 (480p, 0-34 s, 3 chained segments of 11-12 s)

- `lint` listed the logos, the HUD counter, the end card (66.2 s) and the objects with colours and times; its edits
  check gave useful corrections (cash stack from ~17 s, the customer queue missing) and one wrong one (it claimed the
  top-right logo fades at 22 s; the sheet shows it until 34 s), so its output stays a hint.
- Previews: one of four showed the yellow cable drawn dark grey; `黄色的缆绳和黄色的吊架横杆` was added before any
  video was rendered.
- Segment check: K01 was held (Gemini: the yellowfin tuna turns dark and boat-shaped at 1.8-2.4 s, confirmed on the
  stills; a second finding was not visible and dismissed) before K02 and K03 were rendered. Without the check the
  final review would have sent back K01 and, through the chain, K02 and K03 (35 s instead of 12.4 s).
- Local patch: `patch --segment S01:K01 --at 1.75 2.5` rendered 0.27-4.27 s (4.0 s) from K01's own frame. The tuna is
  a yellow fish again at 2.12 s. Joins: in 5.29 vs motion 1.20, local 9.8 (clean); out 6.89 vs 1.31, local 22.6
  (slight, the emoji bubble popping in there); for comparison the chained seams of this video measured 5.16 / local
  11.7 (clean) and 4.89 / local 21.3 (slight). The Gemini close-up called both joins consistent, and the stills
  around them show no change of rocks, water or deck. K02 and K03 rendered from K01's unpatched end, unaffected.
- `run` assembled and reviewed as soon as the source's segments and patch were done: round 1 pass (0 high; two low
  measured seams, one medium continuity finding that the triage marked as also in the original).
- `cost`: Seedance 39.0 s at 480p (new 35.0 s, patch 4.0 s); GPT Image 6 (anchor, keyframe, 4 previews); Gemini 10
  calls (2 lint, 2 segment checks, review, 4 seam close-ups, triage).
- One trial: whole-segment redo stays the default; patches are for short, local flaws in long segments whose shot
  continues.

## 2026-11-04 (later): full GOF3D10057 run, 480p, 0-65.7 s, 3 chained segments of 20-23 s

- Keyframes and 11 previews passed on the sheets; one late preview (50.65 s) already showed the yellowfin pale.
  Nothing was changed, and the last segment drew it silver-grey from 45.6 s (redo with a colour sentence fixed it).
  Previews are now checked object by object for colour, the late ones included.
- K02 kept the grab arm blue after the 39.2 s upgrade although `materials` split blue/red at 39 s. The corrective
  `机械抓斗吊臂由蓝色变为红色` turned the tuna the arm held red; naming only the arm's post and boom and that the
  tuna stay blue/white fixed it. Corrective sentences accumulate, so `redo --replace-prompt` was added.
- A power loss mid-render: the submitted K02 generation was picked up on the next `run` (not paid twice), but a
  `work/` cache stamp was left as 117 zero bytes and `assemble` failed with a bare JSON error. JSON writes now fsync
  before the rename, an unreadable work stamp rebuilds its clip, and read errors name the file.
- `assemble` failed at 480p: the HUD crop (161 px) was odd, FFmpeg's yuv420p crop rounded it to 160 and alphamerge
  refused the matte. Crops are even now (test_hud_matte).
- K02's first segment check failed on a Gemini error and the run went on unchecked; the flaw it would have caught
  cost K03 a re-render.
- The 22.8 s seam measured `visible` (8.2 vs motion 7.1, local 39.8) in every round while the seam sheet, the stills
  and Gemini's close-up showed the same scene: the original's cash stack bursts into flying bills at that moment.
  Delivered with the note.
- The user asked for the original beside the result; `compare` now makes `<name>_写实_对比.mp4` (2160x1920 here).
- `cost`: Seedance 154.2 s (new 66.7, redo 67.0, upstream 20.6); GPT Image 13; Gemini 16. Wall time ~78 min, of
  which ~48 min Seedance waits; the two avoidable redos took ~16 min.

## 2026-11-05: stability and cost over realism (offline checks only, no paid run)

The user found blurred patches where logos and the warning line were removed (S01/S09 of the 1080p batch in
`D:\1080P成片`), a dissolve at GOF3D10374's 33.6 s seam, and the cost too high. The batch's ledger: 427 images (393
previews, 34 keyframes), 87 Seedance submissions for 1952 s (new 547, redo 661, plan 308, upstream 268, wipe 128,
patch 21, experiment 20) for 9 videos of about 500 s, and 529 Gemini calls (324 identity).

- Wiped corners measured on those deliveries with `logos.quality` (fine detail inside the wiped letters against the
  scene around them): the blurred corners 0.10-0.38 (S01 four of five, its bottom-left one soft at 0.52; S09 all six;
  S02 top right; S03 bottom left; S08 top right over water), the clean ones 1.04-1.07 (S03 top right among trees, S08
  bottom right). Thresholds:
  smeared < 0.4, soft < 0.6. The review had reported the smears (`gemini-wipe`, medium) and the skill called them a
  known limit, so they were delivered. Where only the prompt removed a logo (S08 0-10 s, no box), it came out clean.
- The 13 in-shot seams re-measured: the old verdicts called 4 visible (delivered anyway), the new ones (excess over
  motion > 6, local > 24, or a mismatch above 8 and 2.2 times the motion) 8, among them S08:K03 at 33.6 s (12.42 vs
  motion 4.81, local 19.5: other logs, water texture, small objects; old verdict slight). A chained segment with only
  the previous result's frame as reference does not reproduce that result.
- Changes: chained segments start with the previous result's last second (1 s overlap, `chain_prefix`), a 2-frame
  switch, the step where the later segment's own frames start measured too (`inside`); the video prompt names wiped
  corners by position only; wipe quality measured in the gate, `assemble` and the review; 720p clips and renders,
  24 fps deliveries, GPT Image quality high at 1K; previews only on request (at most 4), no midframes, no patches, no
  identity check per segment or preview; materials real but shapes kept; realism only reported (2 or lower: medium);
  the gate holds only for visible seams, smeared corners and high findings; 2 review rounds.
- Offline simulation (stand-in for Lumos: grey, time-compressed 24 fps 720p results) of a 40 s single shot with a
  logo box: K02's clip starts with 30 frames of K01's result, its prompt has the prefix and position sentences and
  does not name the logo, the delivery is 720x1280 at 24 fps, the seam measures clean (switch 0.52, inside clean).
- Not verified on Lumos yet: whether Seedance keeps the 1 s lead as given and continues it without a step, whether
  the position sentence makes it paint scene over the wiped letters, and how 720p looks. Try one source first.

## 2026-11-05 (later): GOF3D10012 repaired at 720p (`D:\720P成片`)

- Wipe + position sentence (the default of the morning): K01 held by the segment check, every wiped corner smeared
  again (detail 0.17-0.25), and Seedance drew the exact 「无尽冬日」 logo into the top-right corner for the first 0.3 s
  although the clip was wiped from frame 0 and the reference picture was clean. A texture fill (push-pull colour plus
  the fine detail beside the box) copied ghost fence posts into the warning line offline, so it was dropped.
- 5 s probe, logos left in the clip and named: both top logos removed cleanly; the bottom warning line kept crisp.
- Full run on a source copy with the bottom 6% cropped (rows 0-1805 of 1920, width 1016 centred, scaled back), all
  logos named: K01 removed every logo; K02 (a chained segment) kept all three, because its lead second came from K01
  without logos and the logos "appeared" with its own frames. The lead frames now get the source's logo pixels
  painted back (`render.lead_logos`); the redone K02 removed every logo.
- `身穿棕色皮毛大衣的渔民雇工` (17.5-42 s) was named in all of K01 and turned the white protagonist brown from 0.2 s;
  contrasting wording fixed it (one K01 redo, K02 upstream).
- Final: seam at 25.46 s clean at the switch (6.95 vs motion 11.58) and where K02's own frames start (9.87 vs 9.54;
  18.3 vs 8.91 before the fixes); review round 2 pass (one medium identity note at 18.5 s); no leftover logo, no
  blur, no warning line on the wipe sheets. Spend: 6 images, 146.3 s of 720p Seedance (first try 28.9, probe 5.0,
  final job 112.4 with one redo and its upstream); the old 1080p version of this video took 50 images and 238.9 s.
- The run process died once at about 15 minutes when the shell that started it ended; the next `run` picked the
  generation up from Lumos without paying again.

## 2026-11-05 (evening): the 26.4 s jump in the repaired GOF3D10012

- The user found a jump at 26.4 s (where K02's own frames start): the trees at the top back to the game's cartoon
  cones, the river lighter. The frame-to-frame measure called it clean (9.87 vs 9.54 during a fast pan), the
  3 s Gemini close-up was centred on the switch (25.46 s) and missed it, the review passed. A region-distance measure
  (restyled vs original per region) was no use either: ordinary action elsewhere scored higher.
- Cause: Seedance reads its edit from the clip's first frame and the reference picture. K02's clip began with K01's
  result and its reference was a copy of that frame, so it read "change nothing" and kept the source's look in its own
  part (trees; earlier the logos; the protagonist's source colour).
- Three 5 s probes across the boundary (15.5 s): P1 lead + the shot's keyframe as reference: trees fine, the logo
  kept (switch local 28.3, visible); P2 the shot's first 0.5 s + lead + own frames, reference the shot's keyframe:
  trees fine, logo removed, switch 4.65 vs 11.75 (clean), own-frames step 10.85 vs 11.42 (clean); P3 no lead:
  switch 11.98, another render. P2 is now how chained clips are built (`render.opening`, `head_frames` in the render
  record, the opening dropped in `assemble.work_clip`, 1.5 s added within the 30 s limit).
- A Gemini stills check of every in-shot seam (restyled 0.5 s before, at and after the switch and the own-frames
  moment, the original above) flagged the 26.4 s change on the faulty video ("雪松从写实的松针细节突然突变为原版低多边形卡通风格");
  any change there is now a high issue.
- K02 rendered again with the opening pair (30.0 s; keep shortened by 0.3 s to 54.87 s so the split, and K01, stay):
  25-28 s continuous on stills (realistic cedars, same river), switch 5.8 vs 11.71, own frames 12.31 vs 10.25, the
  stills check consistent, every logo removed, review round 3 pass. GOF3D10012 total: 6 images, 191.8 s of 720p Seedance
  (first try 28.9, logo probe 5.0, chain probes 15.5, renders 142.4).

- The user then saw the river change colour at 26 s and change back, "as if a piece were inserted": per-frame river
  colour was K01 [90,106,123] to 25.45 s, K02's copy of the lead [94,104,116] from the switch at 25.46 s to 26.37 s,
  then K02's own part [76,98,117]. Seedance keeps the lead's shapes but re-grades its colours. Grey-level measures,
  a per-region distance and a colour-window measure all missed it. Assembly now plays the earlier segment through
  the shared second and cuts to the later one's own frames, colours matched on 12 frames each side
  (`assemble.cut_join`, ASSEMBLY_VERSION 6): re-assembled without a render, the river runs [82,99,117] -> [78,97,116]
  across the cut, step 10.12 vs motion 11.05 (clean). Left: K02's own part draws one tree crown entering at the
  top-left edge in the game's light-blue cone shape for 26.38-26.9 s.
- A K02 redo (30 s) for that tree crown with `画面顶部边缘进入画面的每一棵树都是挂满积雪的写实雪松……绝不能出现浅蓝色圆锥形的
  卡通树冠` drew the game's cone trees across the whole top band from 26.38 s (the cut then 15.68 vs 10.95, slight).
  Naming the unwanted look drew it. The earlier generation (ec33d004) was downloaded again and put back (the redo's
  file kept as `ai/S01-K02-b84ed051-rejected.mp4`); delivered: the cut clean (10.12 vs 11.05), review round 5 with 0
  high issues (four medium ones dismissed on the stills). The small tree crown at 26.38-26.9 s stays. GOF3D10012
  total: 6 images, 221.8 s of 720p Seedance.

## 2026-11-05 (night): seam review strengthened

- The user still saw the 26.4 s seam (river saturation, fish colour, a tree crown, small scene changes) after the
  cut-based assembly measured it clean (10.12 vs motion 11.05). A per-region motion-compensated residual did not single
  it out (4.6 against ordinary action up to 17.2). Saturation and colour steps after the original's camera shift is
  taken out did: saturation 2.31 at the cut, the top of the 14-36 s window, usual 0.19 (z 9.4 over the whole video;
  1 of 1291 ordinary pairs at z >= 8, the original's end-card flash at 54 s). Built in as `assemble.look_scan` /
  `look_verdict` (ASSEMBLY_VERSION 7): the seam is now `visible`.
- Gemini on the two consecutive frames at full size (original below): at the cut `medium` (the low-poly tree crown,
  fish and water jumping); an ordinary pair at 20 s `low` (fish highlights, spray). Built in as `gemini-seam-pair`
  (medium or high -> high issue), replacing the 0.5 s stills.
- Review round 6 of GOF3D10012: both report the 26.4 s seam high, so the video fails, as the user judged it. The
  seam sheet shows the two frames at full size with a change map and the final verdict.

## 2026-11-05 (late): every prompt in English

At the user's request every text sent to the models is English: the preset templates and the chain, removal, wipe and
burn sentences (`assets/prompts.json`), positions and lists built by `prompts.py`, the preview prefix, the Chinese
labels and examples inside the Gemini prompts, `lint` drafting `materials`/`remove` and its suggestions in English,
and the skill telling the agent to write `materials`, `remove`, `extra`, overlay `what` and `--prompt` in English.
Gemini still writes report texts (issue descriptions, summaries) in Chinese for the user. Not yet rendered with the
English prompts.

## 2026-11-05 (late): back to 1080p renders and 2K images

At the user's request Seedance renders 1080p again (`job.RESOLUTION`, `lumos.submit_video_edit`; clips sent at 1080
on the short side, deliveries 1080p at 24 fps) and GPT Image runs at 2K (`lumos.submit_image_to_image`), quality high.
Existing jobs keep the resolution stored in their job.json. Not yet rendered at these settings.
