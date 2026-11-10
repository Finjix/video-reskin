---
name: video-reskin
description: Batch-restyle 2.5D (top-down) game videos, by default into realistic materials with the style unchanged ("写实版材质，风格不变"), into 1080p deliverables without game logos, warning lines or end cards: a Gemini survey of each whole video, one GPT Image scene map per location and three-view sheets of the subjects that come into view, Seedance 2.5 video editing on Lumos in segments of up to 30 s, each continuing one chained to the previous result's last frame, seams placed in fast camera moves and joined with plain cuts (a very light dissolve only where a cut would show), reassembly on the source clock with the original audio, and a review with at most two redos per segment.
---

# Video reskin

## Runtime

- Windows 10/11 x64 with a local shell. `<skill_root>` is the folder that holds this file.
- Every command: `& "<skill_root>/scripts/video_reskin.cmd" <command> [--job-dir <job_dir>] ...`. Python, FFmpeg and
  libraries download into `<skill_root>/runtime-cache` on first use; never install or build an environment yourself.
  Results go to stdout, progress to stderr.
- Read the output, not the exit code (some shells report every failure as 1). `error:` lists what the input broke:
  fix it and run the same command again. `credential error:` names the key or login to provide. `runtime error:` /
  `service error:` name a network, disk or Lumos problem: fix the cause and run the same command again. Every command
  can be re-run and redoes only what changed; `render` also resumes generations still running on Lumos.
- `render` takes minutes per segment (Seedance renders on Lumos, 4 at a time) and prints a heartbeat every 2 minutes. Run
  it with a long timeout (an hour or more for a batch) and wait; never poll or edit `state/`.
- The scripts do all media work, segmentation, retiming, colour matching, seam masking and calls to the models. Your
  part is editorial: where each video stops, what is on screen, what to remove, checking the survey and the
  reference pictures, and confirming review findings on stills. Never write scripts that call Lumos, Winky or FFmpeg
  yourself.
- Files you write (work order, edits) are JSON; texts inside them are Chinese. Reply in the user's language.

## Credentials (first)

Run `auth`. It shows the stored Winky key and whether the Lumos login works.
- No Winky key: ask the user for theirs (Lumos → Winky → API Keys → 创建密钥), then `auth --key <key>`. The key is
  stored encrypted for this Windows user; never write it into job files, reports or replies.
- Not logged in to Lumos (or `credential error: ... run login`, also mid-batch): start `login --no-open --timeout
  1800` in the background, and as soon as it prints `qr: <path>` send that image to the user (as a file, or show
  the path) and ask them to scan it with the Feishu app. Feishu replaces the code every minute or two: every new
  `qr:` line is a new image, send it at once. Do not sit idle while waiting: carry on with work that needs no
  Lumos (init, analyze, survey, edits). `login` ends with `lumos: logged in as ...`. `login --check` only checks.
- The Winky key is only for Gemini (survey, review); pictures (GPT Image) and videos (Seedance 2.5) run on the Lumos
  login.

## Talking to the user

Confirm once, at the start, in one message, and only what you cannot work out: the footage (files or folders), and
the style if it is not the default realistic material look. By default every video loses its end card, game logos and
bottom warning text (the user's standing request); keep HUD numbers, icons and buttons. Then work to the delivered
batch without asking; an open question never stops production: take the default below and report it at the end.

| Situation | Default |
| --- | --- |
| End card start unclear | the last frame before the transition into it (wipe, flash, blur, logo zoom); judge from `<id>-end.jpg` |
| A logo that moves between corners | one `remove` item naming the logo and its text, e.g. `游戏logo「WHITEOUT SURVIVAL」` |
| A logo or warning line shows only part of the time | a timed item `{"what": "...", "start": s, "end": s}`: naming text that is not on screen makes the model draw garbled text there |
| Objects that could be mistaken for each other (whole fish vs fish steaks, cash vs food) | name each as itself in `materials` (`整条的金枪鱼`, `绿色的纸币堆`) and leave out materials that are not on screen |
| A survey view is blurred or covered, or a subject crop misses the subject | correct `scenes` / `subjects` for that source in the edits (copy them from `catalog/survey/<id>.json`, change the time or box) |
| A subject that does not matter (a background crowd, a one-frame effect) | drop it from `subjects`: every sheet costs a picture and takes a reference slot |
| Two sources are the same ad in other sizes | `style_from` the first: the second's scene map takes its look from the first's |
| A scene map invents places, keeps cartoon parts, characters, UI or text; a sheet changes the subject | adjust `materials`/`extra` or the views/box, then `refs --redo <key>` (at most twice, then keep the best and note it) |
| Gemini reports a flaw the original shows too | dismiss it (note it) |
| A confirmed flaw | `redo` that segment with a reason and, when it helps, a short corrective `--prompt` about the look (at most two redos per segment); a sentence about where objects are can make the video model move its camera: on the next redo drop it with `--replace` |
| A segment fails on Lumos twice | shorter prompt (`extra` off), or split it differently with `split_at`; then render again |
| A segment still flawed after two redos | deliver; list each remaining issue with its time |

Stop to ask only when nothing can continue: footage missing or unreadable, no key, or the user does not scan the QR
code. Say what you tried and the minimum you need.

## Workflow

Commands and file formats: [workflow](references/workflow.md). Review loop: [review](references/review.md).

1. `init --work-order -` (JSON on stdin) creates `<output_root>/视频换风格_YYYYMMDD_HHMMSS/temp`, the job dir.
2. `analyze` finds cuts, the camera's speed, the end-card candidate and still overlays. Read `catalog/catalog.md` and
   look at every `catalog/<id>.jpg` and `catalog/<id>-end.jpg`.
3. `survey` has Gemini watch each whole source once: the moments that together show the whole location (views) and
   the characters, objects and props that come into view (subjects), each re-boxed on its still. Look at every
   `catalog/<id>-survey.jpg`.
4. Write the edits file: per source the kept range (`keep`, ending before the end card), the real-world `materials`
   of what is on screen, what to `remove` (only logos and warning lines you actually saw, timed when they come and
   go; HUD stays), and corrected `scenes`/`subjects` when the survey picked badly. `plan --edits <file>` cuts every
   kept range into segments of at most 30 s: at camera cuts where possible, otherwise where the camera moves
   fastest; writes `reports/plan.md` with each segment's references (the scene map and up to two sheets; only
   sheets some segment takes are made).
5. `refs` makes one scene map per scene (GPT Image 2.6, 4K, quality high: the views joined into one overview of the
   whole location, realistic, no characters, UI or text) and one three-view sheet per subject (1K, quality medium).
   Look at every `sheets/<id>-refs.png`: the map keeps the layout of the views, realistic materials, no cartoon
   parts, no invented places; each sheet keeps its subject's shape, colours and parts. Redo bad ones before
   rendering: every segment copies them.
6. `render` edits every segment with Seedance 2.5 (`video_edit`) on Lumos at 1080p: the source clip plus the scene
   map and at most two sheets of subjects on screen (those first seen in the segment first). A segment that
   continues a shot also gets, as Image 1, the previous segment's result at the moment it begins (cut from that
   result's tail), so the segments of one shot render one after the other and a redo re-renders the segments after
   it in that shot; separate shots and sources render side by side. A result that no longer shows the source's camera and layout (more likely
   with more sheets) is rejected and rendered once more by itself with the scene map only (not counted as a redo).
7. `assemble` puts every result on the source clock at one constant speed per clip, its seams pinned to the
   source's moments (the few holds or skips this needs go where the picture stands still), matches colours
   across seams inside a shot, switches at each such seam at the least visible frame of the overlap with a plain
   cut, or with a very light 6-frame dissolve where a cut would show (no other effects at seams: the user's rule),
   joins everything with the source audio and writes `<name>_写实.mp4` into the delivery folder.
8. `review` has Gemini watch each whole video beside its original, and writes stills for you: the whole video every
   3 s and one five-frame sheet per seam (-1, -0.5, 0, +0.5, +1 s). Look at all of them yourself, confirm every
   issue on stills (`frames --source <id> --at <t>`), `redo` the confirmed segments (each at most twice), then
   (`refs` if a picture was redone,) `render`, `assemble`, `review` again.
9. Final summary, in your reply: videos delivered and the folder; per video the kept range, what was removed, the
   segments and seams, review rounds and verdict; issues dismissed (and why) or still open (with times); fallbacks
   taken (survey corrections, picture redos, segment redos).

## Rules

1. **Honest work.** Judge only from sheets, stills, reports and command output you read. Never claim a review passed,
   that you watched a video, or that a fix worked without the stills or a new review round to show it.
2. **Same video, new materials.** Composition, motion, timing, characters, counts and HUD stay as in the original;
   only the materials change, plus the removals. Never ask for added objects, text or effects.
3. **No logos, warning lines or end cards** in deliveries unless the user asks to keep them.
4. **Confirm before redoing.** Gemini can be wrong: look at the stills of every issue before `redo`; redo only
   confirmed segments, never the whole video for a local flaw; never more than twice per segment.
5. **Delivery folder.** It holds only the finished `.mp4` files and `temp/`. Everything else (edits drafts, stills,
   logs) lives in `temp/`; revisions reuse the same folder.
