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
- `render` takes 5-10 minutes per segment (Seedance renders 1080p on Lumos). Segments of one shot render one after
  another (each starts from the previous result), separate shots and sources 4 at a time; it prints a heartbeat every
  2 minutes. Run it with a long timeout (an hour or more for a batch) and wait; never poll or edit `state/`.
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
bottom warning text (the user's standing request); keep HUD numbers, icons and buttons. Then work to the delivered
batch without asking; an open question never stops production: take the default below and report it at the end.

| Situation | Default |
| --- | --- |
| End card start unclear | the last frame before the transition into it (wipe, flash, blur, logo zoom); judge from `<id>-end.jpg` |
| A logo that moves between corners | one `remove` item naming the logo and its text, e.g. `游戏logo「WHITEOUT SURVIVAL」` |
| A logo or warning line shows only part of the time | a timed item `{"what": "...", "start": s, "end": s}`: naming text that is not on screen makes Seedance draw garbled text there |
| Objects that could be mistaken for each other (whole fish vs fish steaks, cash vs food) | name each as itself in `materials` (`整条的金枪鱼`, `绿色的纸币堆`) and leave out materials that are not on screen |
| An object comes out in another colour than in the original (a yellow cable drawn red, a crane flickering) | name its colour in `materials` (`黄色的缆绳`), timed when the original changes it (`蓝色的金属吊机` until the upgrade, `红色的金属吊机` after) |
| Objects that appear later in the ad (trucks, a new machine) | timed `materials` items: an untimed one gets drawn into the opening seconds too |
| A coin counter, timer or other screen-fixed HUD with changing numbers | a `hud` box around it (from the still-overlay boxes in the catalog): it is copied back from the original frame by frame, so the digits are always right |
| Screen-fixed, flat text or UI (captions, labels, banners, buttons) comes out garbled or changed | an `overlays` item for each (a `box` or `poly` hugging it): the models leave it out and the program burns the original's pixels (or the drawn `text`) back; `colors` for letters over a moving scene. UI standing in the scene (floor price tags, bubbles) and text with complex motion stay with the models |
| Two sources are the same ad in other sizes | style the second after the first (`style_from`), so both look alike |
| A keyframe changes the layout, adds objects, keeps a logo or looks noisy | adjust `materials`/`extra`, then `keyframes --redo <key>` (at most twice, then keep the best and note it) |
| Gemini reports a flaw the original shows too | dismiss it (note it) |
| A confirmed flaw | `redo` that segment with a reason and, when it helps, a short corrective `--prompt` (inside a shot this also renders the later segments of that shot again) |
| A visible seam jump (seam sheet, measured or Gemini) | `redo` the segment after the seam; if it stays, `split_at` a still moment elsewhere |
| A segment fails on Lumos twice | shorter prompt (`extra` off), or split it differently with `split_at`; then render again |
| Issues remain after `review_rounds` (3) rounds | deliver; list each remaining issue with its time |

Stop to ask only when nothing can continue: footage missing or unreadable, no key, or a Lumos QR code to scan (send
it and stop; see Credentials). Say what you tried and the minimum you need.

## Workflow

Commands and file formats: [workflow](references/workflow.md). Review loop: [review](references/review.md).

1. `init --work-order -` (JSON on stdin) creates `<output_root>/视频换风格_YYYYMMDD_HHMMSS/temp`, the job dir.
2. `analyze` finds cuts, the end-card candidate and still overlays. Read `catalog/catalog.md` and look at every
   `catalog/<id>.jpg` and `catalog/<id>-end.jpg`.
3. Write the edits file: per source the kept range (`keep`, ending before the end card), the real-world
   `materials` of what is on screen, what to `remove` (only logos and warning lines you actually saw on that source's
   sheets, timed when they come and go; HUD stays), `hud` boxes for screen-fixed counters, `overlays` for screen-fixed flat text and UI the models garble, and an `anchor` time when
   the automatic one would be a poor frame. `plan --edits <file>` cuts every kept range into as few segments as
   Seedance allows (at most 30 s each, at camera cuts where possible) and writes `reports/plan.md`.
4. `keyframes` restyles one anchor frame per source and the first frame of every shot's first segment after it (later
   segments of a shot start from the previous result instead). Look at every `sheets/<id>-keyframes.jpg`: same
   layout and objects as the original, realistic materials, no logo or warning text, no strong noise or fragmented
   look. Redo bad ones before rendering: a whole shot follows its keyframe.
5. `render` edits every segment with Seedance 2.5 on Lumos at the job resolution (480p by default while developing), chaining the segments of each shot.
6. `assemble` retimes each segment to its source duration, matches colours, switches between segments at their
   best-matching frame, copies the `hud` back from the original, burns in the `overlays`, joins everything with the source audio and writes
   `<name>_写实.mp4` into the delivery folder. It measures every seam (`clean`, `slight`, `visible`) and draws
   `sheets/<id>-seams.jpg` (the frames before and after each switch): look at it.
7. `review` has Gemini compare each video with its original (穿帮, bugs, 拼接色差, leftover logos/text), then look at
   every seam close up, and adds the measured seam jumps. Confirm every reported issue on stills
   (`frames --source <id> --at <t>`), `redo` the confirmed segments, then (`keyframes` if a keyframe was redone,)
   `render`, `assemble`, `review` again, up to `review_rounds` rounds. A video with a visible seam is not done.
8. Final summary, in your reply: videos delivered and the folder; per video the kept range, what was removed, review
   rounds and verdict, seam measurements; issues dismissed (and why) or still open (with times); redos and keyframe
   redos.

## Rules

1. **Honest work.** Judge only from sheets, stills, reports and command output you read. Never claim a review passed,
   that you watched a video, or that a fix worked without the stills or a new review round to show it.
2. **Same video, new materials, one look.** Composition, motion, timing, characters, counts and HUD stay as in the
   original; only the materials change, plus the removals. The restyled scene never changes at a seam. Never ask
   for added objects, text or effects.
3. **No logos, warning lines or end cards** in deliveries unless the user asks to keep them.
4. **Confirm before redoing.** Gemini can be wrong: look at the stills of every issue before `redo`; redo only
   confirmed segments, never the whole video for a local flaw.
5. **Delivery folder.** It holds only the finished `.mp4` files and `temp/`. Everything else (edits drafts, stills,
   logs) lives in `temp/`; revisions reuse the same folder.
