---
name: video-reskin
description: Batch-restyle videos (by default into realistic materials with the style unchanged, "写实版材质，风格不变") into 1080p deliverables without game logos, warning lines or end cards: GPT Image keyframes, Seedance 2.5 video editing on Lumos, reassembly on the source clock with the original audio, and a Gemini review loop with frame-checked redos.
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
- `run` (and `render`) takes 5-10 minutes per segment on Lumos. Segments of one shot render one after another (each
  starts from the previous result), separate shots and sources 4 at a time, longest first; it prints a heartbeat every
  2 minutes. Run it with a long timeout (an hour or more for a batch) and wait; never poll or edit `state/`, and never
  start a second `run`/`render` of the same job while one is running (it refuses: `another render of this job is
  still running`).
- After a crash or reboot mid-batch: `status`, then `run` again. Generations already submitted are picked up on Lumos,
  not paid again; finished results in `ai/` stay. An `error:` naming an unreadable file under `work/` is a cache:
  delete that file and run the command again. Under `state/` or `config/`, report it instead of guessing.
- Some consoles show Chinese output as garbled characters; the files and the prompts sent are fine. Read the job's
  JSON files, not the console, before you conclude that text was corrupted.
- Seedance is the cost: every segment and every redo is paid. A redo inside a shot also renders the rest of that shot
  again, so `run` checks each segment that has a successor before the successor starts (the segment check) and holds
  it when the check finds a flaw. `cost` shows what was sent and why.
- The scripts do all media work, segmentation, colour matching and calls to the models. Your part is editorial:
  where each video stops, what is on screen, what to remove, judging keyframes, and confirming review findings on
  stills. Never write scripts that call Lumos, Winky or FFmpeg yourself.
- Files you write (work order, edits) are JSON; prompt texts inside them are Chinese. Reply in the user's language.

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
bottom warning text (the user's standing request); keep HUD numbers, icons and buttons. When the user asks to see
the result next to the original (对比, 并排, 左右对比), `compare` after the last `assemble` writes
`<name>_写实_对比.mp4` beside the delivery (original left, restyled right; `--layout top` stacks them); never build it
with FFmpeg yourself. When the user asks to redo a delivered video and compare versions (原版-旧版-新版), start a new
job (`init`) so the old delivery stays, and after its last `assemble` run `compare --only S01 --with <old delivery>
--labels 原版 旧写实版 新写实版`. Then work to the delivered
batch without asking; an open question never stops production: take the default below and report it at the end.

| Situation | Default |
| --- | --- |
| End card start unclear | the last frame before the transition into it (wipe, flash, blur, logo zoom); judge from `<id>-end.jpg` |
| A logo that moves between corners | one `remove` item naming the logo and its text, e.g. `游戏logo「WHITEOUT SURVIVAL」` |
| A logo or warning line shows only part of the time | a timed item `{"what": "...", "start": s, "end": s}`: naming text that is not on screen makes Seedance draw garbled text there |
| Objects that could be mistaken for each other (whole fish vs fish steaks, cash vs food) | name each as itself in `materials` (`整条的金枪鱼`, `绿色的纸币堆`) and leave out materials that are not on screen |
| An object comes out in another colour than in the original (a yellow cable drawn red, a crane flickering) | name its colour in `materials` (`黄色的缆绳`), timed when the original changes it (`蓝色的金属吊机` until the upgrade, `红色的金属吊机` after) |
| An object changes colour during a shot (an upgrade turns a blue crane red) | two timed `materials` items split at the change (`蓝色的机械抓斗吊臂` until 39, `红色的机械抓斗吊臂` from 39), and look at a preview after the change. A segment spanning the change can still keep the old colour: then `redo` it with a `--prompt` naming only the part that changes and the moment in seconds into the segment (see the next rows) |
| Objects that appear later in the ad (trucks, a new machine) | timed `materials` items: an untimed one gets drawn into the opening seconds too |
| A coin counter, timer or other screen-fixed HUD with changing numbers | a `hud` box around it (from the still-overlay boxes in the catalog): it is copied back from the original frame by frame, so the digits are always right |
| Screen-fixed, flat text or UI (captions, labels, banners, buttons) comes out garbled or changed | an `overlays` item for each (a `box` or `poly` hugging it): the models leave it out and the program burns the original's pixels (or the drawn `text`) back; `colors` for letters over a moving scene. UI standing in the scene (floor price tags, bubbles) and text with complex motion stay with the models |
| The user finds the result not realistic enough, still cartoonish in places | the preset already rebuilds every object as its real counterpart (shape, proportions and surface; layout, positions, counts and colours stay). Name each prominent object in `materials` by its real-world look (`枝叶层次丰富、挂着积雪的真实云杉树`, `被积雪半覆盖的不规则雪堆和岩石`, `排队的穿浅蓝色棉服的真人顾客`), the scenery too: Seedance keeps the realism of its first frame on what it shows, but draws what pans into view later (snow mounds, trees, crowds) nearly as the cartoon source unless the text describes its real look |
| The user gives a reference picture or video for the look | `style_ref` in the edits: `{"path": <video>, "at": <seconds>, "box": [x0, y0, x1, y1]}` (or an image path); pick a frame with the textures wanted (ground, water, wood, snow) and few objects that could be copied, crop away its logos and warning lines. The anchor keyframe learns its texture and realism (never its content); the rest of the video follows the anchor. Check the anchor for objects copied from the reference. The job keeps the frame as `keyframes/<id>-styleref.png`: if the user moves the reference file away, point `style_ref` at that copy (the anchor stays current) |
| Two sources are the same ad in other sizes | style the second after the first (`style_from`), so both look alike |
| A keyframe changes the layout, adds objects, keeps a logo or looks noisy | adjust `materials`/`extra`, then `keyframes --redo <key>` (at most twice, then keep the best and note it) |
| Looking at keyframes | besides layout and colours, note how each recurring character looks there (the protagonist, customers): write that look into `materials` (`身穿深蓝色派克大衣、灰色毛领兜帽、深色长裤的写实成年男子主角`) before rendering. A generic `身穿蓝色棉服的真人主角` let the protagonist slide back to the game's light-blue chibi within the first segment, and every later segment kept it |
| `identity` issue or lines under a preview or a held segment (a subject changed look) | look at the close-up sheet (`-identity-...-subjects.jpg`, REF columns first): confirmed -> `redo` the segment where the change starts (its successors in the shot follow) with the subject's keyframe look in `materials` and a `--prompt` that keeps it; a preview -> `preview --at T --redo` |
| Looking at previews | judge their realism too (they are the look each moment should have), and compare the colour of every prominent object with the original, in every preview, the late ones of each segment too: a whole yellowfin that came out pale in one preview was drawn silver-grey for the last 20 s of the video. Fixing it in the edits before rendering costs a few images; a redo costs 6-9 minutes and every later segment of the shot |
| A preview shows a wrong object, colour or text | the edits are wrong for that moment (an untimed item, a missing colour, a confusable name): fix them, `plan`, `preview` again; previews cost a few images, a wrong prompt costs every segment |
| After a `redo --prompt`, before rendering | `preview --only S01:K02` (or `--at <the flaw's time>`): previews carry the segment's corrective sentences, so a sentence that recolours the wrong object shows on a still for the price of an image instead of a render. `run` makes them by itself and stops to let you look |
| The video turns cartoonish later in a segment or along a shot (realism 3 or lower, toy trucks, doll people, flat snow), while the previews of those moments look real | `midframes` in the edits: preview times inside each fading segment (or `"auto"`), sent to Seedance as extra reference pictures; check those previews, then `redo` the segment. See "Realism that fades" in [review](references/review.md) |
| `lint` suggests a change | it is Gemini's reading of the video: check it on the sheets (it once claimed a logo vanished at 22 s that stayed to 34 s); take what the sheets confirm |
| A segment is held by the segment check | look at its stills (`review/gate/<id>-<Kxx>-p1.jpg`) and its realism line: confirmed -> `redo` it (with `--prompt`, or `midframes` when its realism fades); a false alarm or a flaw you let through -> `accept --segment <key> --reason <why>` (later checks and the review are told, so it does not hold the next segments again); then `run` again |
| Screen-fixed text or UI comes out garbled after rendering | add an `overlays` (or `hud`) item covering it with a `box`/`poly` and no `colors`, `plan`, `assemble`: the segments are reused, nothing is rendered again |
| Gemini reports a flaw the original shows too | dismiss it (note it) |
| A confirmed flaw | `redo` that segment with a reason and, when it helps, a short corrective `--prompt` (inside a shot this also renders the later segments of that shot again). This is the default |
| Writing a corrective `--prompt` | name only the part that changes and what stays, e.g. `只有吊臂的金属立柱和横臂换成红色涂装；金枪鱼保持蓝白色鱼身，绝不变红`: a bare colour change (`吊臂由蓝色变为红色`) turned the tuna it held red too |
| Redoing a video in a new job with the same segments | carry the old job's corrective sentences over with `redo --prompt` before the first render, but reread each against every object of its segment: `被抓起的金枪鱼保持蓝白色` (meant for the blue tuna) turned the golden yellowfin the claw lifts later silver |
| A corrective sentence made things worse | `redo --segment <key> --reason R --replace-prompt --prompt <new sentence>`: corrective sentences otherwise accumulate over attempts |
| A confirmed flaw that is short (a few seconds) and local, inside a long segment, with later segments of the shot already done | consider `patch --segment <key> --at <start> <end> --reason R [--prompt P]`: only 4-8 s render again and the rest of the shot stays. Not for flaws spread over the segment, a wrong look or colour of the whole segment, fast camera motion or a cut at the patch's edges, or a flaw within 0.75 s of the segment's end. After `assemble`, both patch joins must be clean or slight; a visible join -> `patch --drop` and `redo` the segment |
| A visible seam jump (seam sheet, measured or Gemini) | `redo` the segment after the seam; if it stays, `split_at` a still moment elsewhere |
| A seam measured `visible`, but the seam sheet shows the same scene and the Gemini close-up calls it consistent | check the original at the seam (`frames`): an effect bursting there (flying bills, a flash) is counted as a jump. Dismiss it with that reason and report it; no redo |
| `segment check failed (...); continuing unchecked` | the next segment renders anyway; after assembly look at that segment's frames yourself (`frames`) |
| A segment fails on Lumos twice | shorter prompt (`extra` off), or split it differently with `split_at`; then render again |
| Issues remain after `review_rounds` (3) rounds | deliver; list each remaining issue with its time |

Stop to ask only when nothing can continue: footage missing or unreadable, no key, or a Lumos QR code to scan (send
it and stop; see Credentials). Say what you tried and the minimum you need.

## Workflow

Commands and file formats: [workflow](references/workflow.md). Review loop: [review](references/review.md).

1. `init --work-order -` (JSON on stdin) creates `<output_root>/视频换风格_YYYYMMDD_HHMMSS/temp`, the job dir.
2. `analyze` finds cuts, the end-card candidate and still overlays; `lint` then has Gemini list what each source
   shows (objects with colours and times, logos and warning lines, screen-fixed text, HUD, end card) and drafts
   `config/edits.draft.json`. Read `catalog/catalog.md` and `catalog/lint.md`, look at every `catalog/<id>.jpg`,
   `catalog/<id>-end.jpg` and `catalog/<id>-lint.jpg`.
3. Write the edits file (start from the draft; keep only what the sheets confirm): per source the kept range (`keep`, ending before the end card), the real-world
   `materials` of what is on screen, what to `remove` (only logos and warning lines you actually saw on that source's
   sheets, timed when they come and go; HUD stays), `hud` boxes for screen-fixed counters, `overlays` for screen-fixed flat text and UI the models garble, and an `anchor` time when
   the automatic one would be a poor frame. `lint --edits <file>` checks the edits against the video (untimed or
   absent items, colours, missing logos); `plan --edits <file>` cuts every kept range into as few segments as
   Seedance allows (at most 30 s each, at camera cuts where possible) and writes `reports/plan.md`.
4. `run` makes the keyframes (one anchor per source, the first frame of every shot) and previews (the same prompt on
   frames across every segment), then stops. Look at every `sheets/<id>-keyframes.jpg` and `sheets/<id>-preview.jpg`:
   same layout and objects as the original, the right colours, realistic materials, no logo or warning text, no
   strong noise or fragmented look. Fix the edits or redo keyframes before rendering: a whole shot follows its keyframe
   and every segment uses the same texts as the previews.
5. `run` again renders every segment with Seedance 2.5 on Lumos at the job resolution (480p by default while
   developing), chaining the segments of each shot, and checks each segment that has a successor before the
   successor starts. As soon as all segments of a source are done it assembles and reviews that source while the
   rest renders:
   - `assemble` retimes each segment to its source duration, matches colours, switches between segments at their
     best-matching frame, splices in patches, copies the `hud` back from the original, burns in the `overlays`, joins
     everything with the source audio and writes `<name>_写实.mp4` into the delivery folder. It measures every seam
     (`clean`, `slight`, `visible`) and draws `sheets/<id>-seams.jpg`: look at it.
   - `review` has Gemini compare the video with its original (穿帮, bugs, 拼接色差, leftover logos/text, and whether it
     looks real enough: a realism timeline, `not_realistic` issues; and whether every recurring character and
     object keeps its keyframe look to the end: `identity` issues on close-ups), look at every seam close up, adds the measured
     seam jumps, draws the stills of every issue (`review/issues/<id>-r<n>-p<k>.jpg`) and lets Gemini triage them (a
     hint, not a verdict).
   (`keyframes`, `preview`, `render`, `assemble`, `review` also run one by one.)
6. Held segments first: look at their stills, `redo` or `accept`. Then confirm every review issue on its stills (more
   moments: `frames --source <id> --at <t>`). Judge the realism yourself as well, on those stills and on `frames`
   every ~10 s across the whole video, its end too: a part that looks like a game is a flaw even when Gemini rates
   it well (fix: `midframes`, see the table). `redo` the confirmed segments (or `patch` a short local flaw, see the
   table), and `run` again: only what changed renders, unchanged videos are not reviewed again. Up to
   `review_rounds` rounds. A video with a visible seam is not done.
7. If the user asked for a comparison, `compare` (after the last `assemble`).
8. Final summary, in your reply: videos delivered and the folder; per video the kept range, what was removed, how
   real each part looks (the realism timeline and your own judgement), review
   rounds and verdict, seam measurements; issues dismissed (and why) or still open (with times); redos, patches,
   accepted holds and keyframe redos; and the `cost` lines (seconds sent to Seedance by cause).

## Rules

1. **Honest work.** Judge only from sheets, stills, reports and command output you read. Report how real the
   delivery looks, part by part, not only that the review passed. Never claim a review passed,
   that you watched a video, or that a fix worked without the stills or a new review round to show it.
2. **Same video, real materials, one look.** Composition, motion, timing, characters, counts, positions, colours and
   HUD stay as in the original; the materials change and each object's appearance is rebuilt as its real-world
   counterpart (shape details, proportions, surface), plus the removals. The restyled scene never changes at a seam. Never ask
   for added objects, text or effects.
3. **No logos, warning lines or end cards** in deliveries unless the user asks to keep them.
4. **Confirm before redoing.** Gemini can be wrong: look at the stills of every issue (and of every held segment)
   before `redo`, `patch` or `accept`; redo only confirmed segments, never the whole video for a local flaw.
5. **Delivery folder.** It holds only the finished `.mp4` files and `temp/`. Everything else (edits drafts, stills,
   logs) lives in `temp/`; revisions reuse the same folder.
