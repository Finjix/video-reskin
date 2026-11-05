---
name: video-reskin
description: Batch-restyle videos (by default into realistic materials with the style unchanged, "写实版材质，风格不变") into 1080p / 24 fps deliverables without game logos, warning lines or end cards: GPT Image keyframes, Seedance 2.5 video editing on Lumos, reassembly on the source clock with the original audio, and a Gemini review loop with frame-checked redos.
---

# Video reskin

## Runtime

- Windows 10/11 x64 with a local executor. The plugin root is two directories above this file.
- Every command: `& "<plugin_root>/scripts/video_reskin.cmd" <command> [--job-dir <job_dir>] ...`. Python, FFmpeg and
  libraries download into the plugin's `runtime-cache` on first use; never install or build an environment yourself.
  Results go to stdout, progress to stderr.
- Read the output, not the exit code (Codex shells report every failure as 1). `error:` lists what the input broke:
  fix it and run the same command again. `credential error:` names the key or login to provide. `runtime error:` /
  `service error:` name a network, disk or Lumos problem: fix the cause and run the same command again. Every command
  can be re-run and redoes only what changed; `render` also resumes generations still running on Lumos.
- `run` (and `render`) takes several minutes per segment on Lumos. Segments of one shot render one after another
  (each starts from the previous result), separate shots and sources 4 at a time, longest first; it prints a
  heartbeat every 2 minutes. Run it with a long timeout (an hour or more for a batch) and wait; never poll or edit
  `state/`, and never start a second `run`/`render` of the same job while one is running (it refuses: `another
  render of this job is still running`).
- After a crash or reboot mid-batch: `status`, then `run` again. Generations already submitted are picked up on Lumos,
  not paid again; finished results in `ai/` stay. An `error:` naming an unreadable file under `work/` is a cache:
  delete that file and run the command again. Under `state/` or `config/`, report it instead of guessing.
- Some consoles show Chinese output as garbled characters; the files and the prompts sent are fine. Read the job's
  JSON files, not the console, before you conclude that text was corrupted.
- Seedance is the cost: every segment and every redo is paid, and a redo inside a shot also renders the rest of that
  shot again. GPT Image images are paid too. The workflow is tuned for a stable video at low cost, not for the most
  realistic look (the user's decision of 2026-11-05: the 1080p batch spent 427 images and 87 renders, 1952 s, on 9
  videos of about 500 s, chasing realism). `cost` shows what was sent and why.
- The scripts do all media work, segmentation, colour matching and calls to the models. Your part is editorial:
  where each video stops, what is on screen, what to remove, judging keyframes, and confirming review findings on
  stills. Never write scripts that call Lumos, Winky or FFmpeg yourself.
- Files you write (work order, edits) are JSON. Every text in them that goes into a prompt (`materials`, `remove`,
  `extra`, `overlays` `what`, `--prompt` sentences) is English, like every prompt the plugin sends. Reply in the
  user's language.

## Credentials (first)

Run `auth`. It shows the stored Winky key and whether the Lumos login works.
- No Winky key: ask the user for theirs (Lumos → Winky → API Keys → 创建密钥), then `auth --key <key>`. The key is
  stored encrypted for this Windows user; never write it into job files, reports or replies.
- Not logged in to Lumos (or `credential error: ... run login`, also mid-batch): start `login` in the background
  (never wait on it in the foreground). As soon as it prints `qr: <path>`, send that image to the user, tell them to
  scan it with the Feishu app and then reply to confirm (e.g. "已扫码" / "继续"), and **end your turn right there** — do not poll, sleep or wait for
  the scan. The background `login` keeps waiting (up to 5 minutes, refreshing an expired code) and stores the login
  when scanned. When the user replies, run `login --check`; if it is not logged in (e.g. the code timed out), start
  `login` again and send the new QR the same way. Then continue where you left off.
- The Winky key is only for the Gemini review; keyframes (GPT Image) and videos (Seedance) run on the Lumos login.

## Talking to the user

Confirm once, at the start, in one message, and only what you cannot work out: the footage (files or folders), and
the style if it is not the default realistic material look. By default every video loses its end card, game logos and
bottom warning text (the user's standing request); keep HUD numbers, icons and buttons. Every job renders and
delivers 1080p at 24 fps (fixed; there is no resolution option). When the user asks to see the result next to the
original (对比, 并排, 左右对比), `compare` after the last `assemble` writes `<name>_写实_对比.mp4` beside the delivery
(original left, restyled right; `--layout top` stacks them); never build it with FFmpeg yourself. When the user asks
to redo a delivered video and compare versions (原版-旧版-新版), start a new job (`init`) so the old delivery stays,
and after its last `assemble` run `compare --only S01 --with <old delivery> --labels 原版 旧写实版 新写实版`. Then work
to the delivered batch without asking; an open question never stops production: take the default below and report
it at the end.

## What to fix, what to leave

A delivery must be stable: no jump, dissolve or change at a seam, no blurred or smeared logo corner, no leftover or
garbled text, nothing that morphs, vanishes or is added, and the prominent objects in their original colours. Those
are the only flaws worth a redo. How real a part looks is reported, not chased: the preset makes the materials real
and keeps the original's shapes, proportions and character designs (a chibi figure stays a chibi figure in real
cloth), and parts that look less real than the keyframe are accepted unless a large area is plainly the flat
cartoon original. Medium and low review issues go into the final summary, not into redos.

| Situation | Default |
| --- | --- |
| End card start unclear | the last frame before the transition into it (wipe, flash, blur, logo zoom); judge from `<id>-end.jpg` |
| A logo on the sheets | a `remove` item per position and time with a `box` from the catalog's still overlays (check it on a frame). By default the logo stays in the clip and the prompts name it: Seedance removed every logo of GOF3D10012 that way (720p), corners clean, the HUD icon under one rebuilt; GPT keyframes remove it too. In a chained segment the lead frames (the previous result's last second) get the source's logo pixels painted back in, so the logo never 'appears' mid-clip: without that, Seedance kept every logo of K02 as new content. `"wipe": "before"` (the program wipes the letters first) is not for logos: Seedance kept each wiped corner as a blurred patch (detail 0.1-0.38 of the scene around it) and once redrew the game's logo over one |
| A warning line along the bottom edge (广告创意仅供参考…, 健康游戏忠告) | Seedance keeps it however it is handled (named: kept crisp; wiped: a blurred band), like any screen-fixed layer. The only clean removal is cropping it off a copy of the source (GOF3D10012: rows 0-1805 of 1920, width 1016 centred, scaled back to 1080x1920, a 6.3% zoom). That changes the framing: stop and ask the user before cropping, and report it. Leave the warning line out of `remove` for a cropped source |
| `assemble`, a held segment or the review shows `wiped corner ... smeared` (or soft) | only items with `"wipe": "before"` are measured: the wipe came back as a blurred patch. Drop `"wipe"` (the default names it for Seedance instead), `plan`, `redo` the segments it spans. A video with a smeared corner is not done |
| The review's wipe check shows leftover letters | look at `review/<id>-wipes-r<n>-<k>.jpg`: the box or its times miss the logo (step through stills where it moves; fix, `plan`, `redo` the segments it spans), or Seedance kept it: `redo` the segment once with `--prompt` naming the corner (`the game logo "…" in the bottom-right corner is removed; paint the surrounding scene there`) |
| A segment check or the review finds a logo the model drew anyway | make sure its item has a `box` that covers where the model drew it, then `redo` the segment |
| A logo that flies from one corner to another | one boxed item per corner, each a second longer than the logo rests there: the logo is still on its way (and seen by the model) for about a second after it leaves a corner |
| A logo or warning line shows only part of the time | a timed item `{"what": "...", "start": s, "end": s}`: naming text that is not on screen makes Seedance draw garbled text there |
| A segment comes back with its first seconds still in the game's look (then turns real) | `redo` with `--prompt "From its first frame to its last, this segment has the realistic look of image 1."`: Seedance now and then returns the start of a clip unrestyled |
| Objects that could be mistaken for each other (whole fish vs fish steaks, cash vs food) | name each as itself in `materials` (`whole tuna`, `green stacks of banknotes`) and leave out materials that are not on screen |
| An object comes out in another colour than in the original | name its colour in `materials` (`yellow ropes`), with its lightness when it came out darker (`a bright blue parka, the same bright blue as in the original`), timed when the original changes it (`the blue metal crane` until the upgrade, `the red metal crane` after) |
| An object changes colour during a shot (an upgrade turns a blue crane red) | two timed `materials` items split at the change; a segment spanning the change can still keep the old colour: then `redo` it with a `--prompt` naming only the part that changes and the moment in seconds into the segment |
| A crowd in one colour | name only that colour and say everyone wears it (`every single person in the queue wears a bright yellow down jacket`); a crowd with two looks: one item per look, each timed to when it shows |
| Food on plates, a stack of plates (any object resting on another) | one item each, both colours named (`every fish steak lies on a bright blue plate`, `a column of bright blue plates`) |
| Objects that appear later in the ad (trucks, a new machine) | timed `materials` items: an untimed one gets drawn into the opening seconds too. A timed item is named in the whole prompt of every segment it overlaps, and its colours leak onto look-alikes: the hired fishermen in brown fur coats from 17.5 s turned GOF3D10012's white protagonist into a brown fur figure from 0.2 s. Name the protagonist and the others apart (`the protagonist always wears a light blue-white winter coat`, `only these hired hands wear brown coats; the protagonist stays light blue-white`) |
| A coin counter, timer or other screen-fixed HUD with changing numbers | a `hud` box around it: it is copied back from the original frame by frame, so the digits are always right |
| Screen-fixed, flat text or UI (captions, labels, banners, buttons) comes out garbled or changed | an `overlays` item for each (a `box` or `poly` hugging it): the models leave it out and the program burns the original's pixels (or the drawn `text`) back. Already rendered: add it, `plan`, `assemble` (nothing renders again) |
| The user wants it more realistic | name each prominent object's real material in `materials` (`real spruce trees laden with snow`, `rocks half covered in snow`); keep its shape. Tell the user that stronger realism (redesigned shapes, realistic adults for chibi figures) made seams and characters drift in the 1080p batch |
| The user gives a reference picture or video for the look | `style_ref` in the edits: `{"path": <video>, "at": <seconds>, "box": [x0, y0, x1, y1]}` (or an image path), cropped away from logos and warning lines. The anchor keyframe learns its texture (never its content); check the anchor for copied objects |
| Two sources are the same ad in other sizes | style the second after the first (`style_from`), so both look alike |
| A keyframe changes the layout, adds objects, keeps a logo, changes colours or looks noisy | adjust `materials`/`extra`, then `keyframes --redo <key>` (at most twice, then keep the best and note it) |
| A moment the keyframes do not show is doubtful (an object arriving later, a colour change mid-shot, a new `--prompt`) | `preview --only S01 --at T [T...]` (at most 4 per call; each is a paid image): it restyles that frame with the texts the video prompt uses there. Never preview a whole video |
| `lint` suggests a change | it is Gemini's reading of the video: check it on the sheets; take what the sheets confirm |
| A segment is held by the segment check | it is held only for a visible seam, a smeared logo corner or a high Gemini finding. Look at its stills (`review/gate/<id>-<Kxx>-p1.jpg`): confirmed -> `redo` it; a false alarm -> `accept --segment <key> --reason <why>` (later checks and the review are told); then `run` again |
| A confirmed flaw of the kinds above | `redo` that segment with a reason and, when it helps, a short corrective `--prompt` (inside a shot this also renders the later segments of that shot again). Never redo for realism scores, a slightly different texture or a medium/low issue |
| Writing a corrective `--prompt` | describe only the look you want, never the look to avoid: `... never light-blue cone-shaped cartoon tree crowns` made Seedance draw exactly those cartoon trees across the top of GOF3D10012's K02 (a redo wasted; the earlier generation was put back). Name only the part that changes and what stays, e.g. `only the arm's metal post and boom turn to red paint; the tuna keep their blue-white bodies`: a bare colour change turned the tuna it held red too. Sentences accumulate; one that made things worse: `redo --segment <key> --reason R --replace-prompt --prompt <new sentence>` |
| A visible seam (measured, Gemini or `gemini-seam-stills`) | look at `sheets/<id>-seams.jpg` and `review/<id>-seam-<Kxx>-stills.jpg` (restyled 0.5 s before, at and after the switch and where the segment's own frames start, the original above). A chained segment's clip opens with its shot's first 0.5 s and then the previous result's last second, and `assemble` cuts from the earlier segment's last frame to the later one's first own frame (colours matched there); a change at that cut (an area turning back to the cartoon, the water changing colour) means Seedance restyled its own part differently: `redo` that segment once. If it stays, set `"chain_prefix": false` for the source in the edits (one reference picture only, as before) or `split_at` a stiller moment, `plan`, `redo` |
| A seam measured `visible` while the seam sheet shows the same scene and the Gemini close-up calls it consistent | check the original at the seam (`frames`): an effect bursting there (flying bills, a flash) is counted as a jump. Dismiss it with that reason and report it; no redo |
| `segment check failed (...); continuing unchecked` | the next segment renders anyway; after assembly look at that segment's frames yourself (`frames`) |
| A segment fails on Lumos twice | shorter prompt (`extra` off), or split it differently with `split_at`; then render again |
| Gemini reports a flaw the original shows too | dismiss it (note it) |
| Checking a seam yourself | the seam sheet (`sheets/<id>-seams.jpg`) shows, per seam, the frame before and the frame after at full size and a map of what changes between them; the review's pair sheet (`review/<id>-seam-<Kxx>-<t>-pair.jpg`) adds the original's two moments. Compare the two frames region by region (water colour and saturation, trees at the edges, ground, objects): GOF3D10012's 26.4 s seam passed every grey-level measure during a pan and was plain on the two full-size frames. A video with a seam whose look step or pair check is high is not done |
| Issues remain after `review_rounds` (2) | deliver; list each remaining issue with its time. Never run a third round on your own |

Stop to ask only when nothing can continue: footage missing or unreadable, no key, or a Lumos QR code to scan (send
it and stop; see Credentials). Say what you tried and the minimum you need.

## Workflow

Commands and file formats: [workflow](references/workflow.md). Review loop: [review](references/review.md).

1. `init --work-order -` (JSON on stdin) creates `<output_root>/视频换风格_YYYYMMDD_HHMMSS/temp`, the job dir.
2. `analyze` finds cuts, the end-card candidate and still overlays; `lint` then has Gemini list what each source
   shows (objects with colours and times, logos and warning lines, screen-fixed text, HUD, end card) and drafts
   `config/edits.draft.json`. Read `catalog/catalog.md` and `catalog/lint.md`, look at every `catalog/<id>.jpg`,
   `catalog/<id>-end.jpg` and `catalog/<id>-lint.jpg`.
3. Write the edits file (start from the draft; keep only what the sheets confirm): per source the kept range (`keep`,
   ending before the end card), the `materials` of what is on screen with their colours, what to `remove` (only logos
   and warning lines you actually saw, timed, with boxes; HUD stays), `hud` boxes for screen-fixed counters,
   `overlays` for screen-fixed flat text and UI the models garble, and an `anchor` time when the automatic one would
   be a poor frame. `lint --edits <file>` checks the edits against the video; `plan --edits <file>` cuts every kept
   range into as few segments as Seedance allows (at most 30 s each, at camera cuts where possible) and writes
   `reports/plan.md`. Settle the edits (boxes, keep, splits) before the first render: a later change of a box, a
   kept range or a split renders the segments it touches again (the 1080p batch paid 436 s for such changes).
4. `run` makes the keyframes (one anchor per source, the first frame of every shot), then stops. Look at every
   `sheets/<id>-keyframes.jpg`: same layout, objects and colours as the original, real materials, no logo or warning
   text, no strong noise or fragmented look. Fix the edits or redo keyframes before rendering: a whole shot follows
   its keyframe. Preview a later moment only when the table says so.
5. `run` again renders every segment with Seedance 2.5 on Lumos at 1080p, chaining the segments of each shot (each
   later segment's clip starts with the previous result's last second), and checks each segment that has a
   successor before the successor starts. As soon as all segments of a source are done it assembles and reviews that
   source while the rest renders:
   - `assemble` retimes each segment to its source duration, matches colours, switches between segments at their
     best-matching frame inside that shared second, copies the `hud` back from the original, burns in the `overlays`,
     joins everything with the source audio and writes `<name>_写实.mp4` (1080p, 24 fps) into the delivery folder. It
     measures every seam (`clean`, `slight`, `visible`: the switch, and where the later segment's own frames start),
     draws `sheets/<id>-seams.jpg`, and measures every wiped logo corner (`clean`, `soft`, `smeared`, `plain`).
   - `review` has Gemini compare the video with its original (continuity errors, bugs, seam colour jumps, leftover logos/text, realism
     reported as a timeline), checks that recurring characters and objects keep their keyframe look, looks at every
     seam and wiped corner close up, adds the measured seams and corners, draws the stills of every issue
     (`review/issues/<id>-r<n>-p<k>.jpg`) and lets Gemini triage them (a hint, not a verdict).
   (`keyframes`, `preview`, `render`, `assemble`, `review` also run one by one.)
6. Held segments first: look at their stills, `redo` or `accept`. Then confirm the high review issues on their stills
   (more moments: `frames --source <id> --at <t>`), `redo` only the confirmed flaws the table names, and `run` again:
   only what changed renders, unchanged videos are not reviewed again. At most `review_rounds` (2) rounds. A video
   with a visible seam or a smeared corner is not done.
7. If the user asked for a comparison, `compare` (after the last `assemble`).
8. Final summary, in your reply: videos delivered and the folder; per video the kept range, what was removed and how
   each wiped corner measured, the seam measurements, how real it looks (the realism timeline, reported plainly),
   review rounds and verdict; issues dismissed (and why) or still open (with times); redos, accepted holds and
   keyframe redos; and the `cost` lines (images, and seconds sent to Seedance by cause).

## Rules

1. **Honest work.** Judge only from sheets, stills, reports and command output you read. Never claim a review passed,
   that you watched a video, or that a fix worked without the stills or a new review round to show it.
2. **Same video, real materials, one look.** Composition, motion, timing, characters, shapes, counts, positions,
   colours and HUD stay as in the original; only the materials become real, plus the removals. The restyled scene
   never changes at a seam. Never ask for added objects, text or effects.
3. **No logos, warning lines or end cards** in deliveries unless the user asks to keep them, and no blurred patch
   where one was.
4. **Confirm before redoing.** Gemini can be wrong: look at the stills of every issue (and of every held segment)
   before `redo` or `accept`; redo only confirmed segments, never the whole video for a local flaw.
5. **Delivery folder.** It holds only the finished `.mp4` files and `temp/`. Everything else (edits drafts, stills,
   logs) lives in `temp/`; revisions reuse the same folder.
