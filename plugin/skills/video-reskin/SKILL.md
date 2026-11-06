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
- `render` takes minutes per segment (Seedance renders 1080p on Lumos, 4 at a time) and prints a heartbeat every
  2 minutes. Run it with a long timeout (an hour or more for a batch) and wait; never poll or edit `state/`.
- The scripts do all media work, segmentation, colour matching and calls to the models. Your part is editorial:
  where each video stops, what is on screen, what to remove, judging keyframes, and confirming review findings on
  stills. Never write scripts that call Lumos, Winky or FFmpeg yourself.
- Files you write (work order, edits) are JSON; prompt texts inside them are Chinese. Reply in the user's language.

## Credentials (first)

Run `auth`. It shows the stored Winky key and whether the Lumos login works.
- No Winky key: ask the user for theirs (Lumos → Winky → API Keys → 创建密钥), then `auth --key <key>`. The key is
  stored encrypted for this Windows user; never write it into job files, reports or replies.
- Not logged in to Lumos (or `credential error: ... run login`, also mid-batch): tell the user that a Feishu QR code is
  about to open on their screen and that they should scan it with the Feishu app, then run `login`. It waits up to
  5 minutes and replaces an expired code by itself; it prints the image path (`qr: ...`) in case no window opened, so
  you can show that image. `login --check` only checks.
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
| Objects that appear later in the ad (trucks, a new machine) | timed `materials` items: an untimed one gets drawn into the opening seconds too |
| Two sources are the same ad in other sizes | style the second after the first (`style_from`), so both look alike |
| A keyframe changes the layout, adds objects, keeps a logo or looks noisy | adjust `materials`/`extra`, then `keyframes --redo <key>` (at most twice, then keep the best and note it) |
| Gemini reports a flaw the original shows too | dismiss it (note it) |
| A confirmed flaw | `redo` that segment with a reason and, when it helps, a short corrective `--prompt` |
| Detail fades late in a long segment | keep the segments long (never lower `max_segment_seconds`: 5 segments of 15 s showed more and clearer jumps than 2 of 30 s); report it |
| A segment fails on Lumos twice | shorter prompt (`extra` off), or split it differently with `split_at`; then render again |
| Issues remain after `review_rounds` (3) rounds | deliver; list each remaining issue with its time |

Stop to ask only when nothing can continue: footage missing or unreadable, no key, or the user does not scan the QR
code. Say what you tried and the minimum you need.

## Workflow

Commands and file formats: [workflow](references/workflow.md). Review loop: [review](references/review.md).

1. `init --work-order -` (JSON on stdin) creates `<output_root>/视频换风格_YYYYMMDD_HHMMSS/temp`, the job dir.
2. `analyze` finds cuts, the end-card candidate and still overlays. Read `catalog/catalog.md` and look at every
   `catalog/<id>.jpg` and `catalog/<id>-end.jpg`.
3. Write the edits file: per source the kept range (`keep`, ending before the end card), the real-world
   `materials` of what is on screen, what to `remove` (only logos and warning lines you actually saw on that source's
   sheets, timed when they come and go; HUD stays), and an `anchor` time when the automatic one would be a poor frame. `plan --edits <file>` cuts every kept range into
   segments of at most 12 s (at camera cuts where possible) and writes `reports/plan.md`.
4. `keyframes` restyles one anchor frame per source and, after it, the first frame of every segment (a segment that continues a
   shot starts from the previous result's frame; its keyframe only sets the material detail). Objects that come
   into view for the first time in a later segment (timed `materials`) get a reveal frame (`S01:K02-N1`). Look at every
   `sheets/<id>-keyframes.png`: same layout and objects as the original, realistic materials, no logo or warning text,
   no strong noise or fragmented look. Redo bad ones before rendering: every segment copies its keyframe.
5. `render` edits every segment with Seedance 2.5 at 1080p on Lumos. The segments of one source run one after
   another: each later one gets the previous result's frame at its start, its own keyframe for material detail,
   its reveal frames and up to 3 earlier results' frames of the places it shows again (at most 6 pictures).
6. `assemble` retimes each segment to its source duration, matches colours across seams inside a shot, switches at
   the best-matching overlap frame with a 3-frame blend and measures each such seam (`visible` ones become high
   review issues), joins everything with the source audio and writes `<name>_写实.mp4` into the delivery folder.
7. `review` has Gemini watch each whole video beside its original at full size (穿帮, bugs, 拼接色差, leftover
   logos/text) and every seam up close, and writes full-size lossless stills for you: the whole video every 2 s and
   every seam frame by frame. Look at all of them yourself, confirm every reported issue on stills
   (`frames --source <id> --at <t>`), `redo` the confirmed segments, then (`keyframes` if
   a keyframe was redone,) `render`, `assemble`, `review` again, up to `review_rounds` rounds.
8. Final summary, in your reply: videos delivered and the folder; per video the kept range, what was removed, review
   rounds and verdict; issues dismissed (and why) or still open (with times); fallbacks taken (image backend, redos,
   keyframe redos).

## Rules

1. **Honest work.** Judge only from sheets, stills, reports and command output you read. Never claim a review passed,
   that you watched a video, or that a fix worked without the stills or a new review round to show it.
2. **Same video, new materials.** Composition, motion, timing, characters, counts and HUD stay as in the original;
   only the materials change, plus the removals. Never ask for added objects, text or effects.
3. **No logos, warning lines or end cards** in deliveries unless the user asks to keep them.
4. **Confirm before redoing.** Gemini can be wrong: look at the stills of every issue before `redo`; redo only
   confirmed segments, never the whole video for a local flaw.
5. **Delivery folder.** It holds only the finished `.mp4` files and `temp/`. Everything else (edits drafts, stills,
   logs) lives in `temp/`; revisions reuse the same folder.
