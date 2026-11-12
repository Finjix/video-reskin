---
name: video-reskin
description: Batch-restyle 2.5D (top-down) game videos, by default into realistic materials with the style unchanged ("写实版材质，风格不变"), into 1080p deliverables without game logos, warning lines or end cards: a Gemini survey of each whole video, one GPT Image scene map per location and three-view sheets of the subjects that come into view, Seedance 2.5 video editing on Lumos in segments of up to 30 s, each segment that continues a shot chained to the previous result and rendered only after that result is checked and approved, seams placed in fast camera moves and joined with plain cuts (a very light dissolve only where a cut would show), non-game stretches (live action) kept as is, reassembly on the source clock with the original audio, and a gate per segment: Gemini checks each segment, its seam to the one before (on a preview of the segments joined so far) and its handoff frame, the agent confirms the findings and looks for anything else, the next segment renders only after approval, at most five redos per segment.
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
- The scripts do all media work, segmentation, retiming, colour matching, seams and calls to the models. Your
  part is editorial: where each video stops, what is on screen, what to remove, checking the survey and the
  reference pictures, and confirming Gemini's findings on their sheets. Never write scripts that call Lumos, Winky or FFmpeg
  yourself.
- Files you write (work order, edits) are JSON. Every text that reaches a model prompt (`materials`, `remove`,
  `extra`, `scenes`/`subjects` names, `redo --prompt`) is English, like the prompts themselves. Reply in the user's
  language.

## Credentials (first)

Run `auth`. It shows the stored Winky key and whether the Lumos login works.
- No Winky key: ask the user for theirs (Lumos → Winky → API Keys → 创建密钥), then `auth --key <key>`. The key is
  stored encrypted for this Windows user; never write it into job files, reports or replies.
- Not logged in to Lumos (or `credential error: ... run login`, also mid-batch): start `login --no-open --timeout
  1800` in the background, and as soon as it prints `qr: <path>` send that image to the user (as a file, or show
  the path) and ask them to scan it with the Feishu app. Feishu replaces the code every minute or two: every new
  `qr:` line is a new image, send it at once. Do not sit idle while waiting: carry on with work that needs no
  Lumos (init, analyze, survey, edits). `login` ends with `lumos: logged in as ...`. `login --check` only checks.
- The Winky key is only for Gemini (survey, checks); pictures (GPT Image) and videos (Seedance 2.5) run on the Lumos
  login.

## Talking to the user

Confirm once, at the start, in one message, and only what you cannot work out: the footage (files or folders), and
the style if it is not the default realistic material look. By default every video loses its end card, game logos and
bottom warning text (the user's standing request); keep HUD numbers, icons and buttons. Then work to the delivered
batch without asking; an open question never stops production: take the default below and report it at the end.

| Situation | Default |
| --- | --- |
| End card start unclear | the last frame before the transition into it (wipe, flash, blur, logo zoom); judge from the survey's end-card strip (`catalog/<id>-end.jpg` when it leaves a doubt) |
| Live action or other non-game footage (a presenter, filmed hands on a phone, a non-game cartoon) at the start or in between | a `passthrough` range from its first frame to the first gameplay frame after it (the survey's non-game strip shows both); a bit of gameplay shorter than 4 s next to it joins the range; a small picture-in-picture over gameplay is not one |
| A logo that moves between corners | one timed `remove` item per corner (the survey's removal strip shows the move, and its measured still-box span differs from Gemini's), e.g. `the game logo "WHITEOUT SURVIVAL" in the top-left corner`, 0-15 s |
| A logo or warning line shows only part of the time | a timed item `{"what": "...", "start": s, "end": s}`: naming text that is not on screen makes the model draw garbled text there |
| Objects that could be mistaken for each other (whole fish vs fish steaks, cash vs food) | name each as itself in `materials` (`whole tuna`, `stacks of green banknotes`) and leave out materials that are not on screen |
| A survey view is blurred or covered, or a subject crop misses the subject | correct `scenes` / `subjects` for that source in the edits (copy them from `catalog/survey/<id>.json`, change the time or box) |
| An object that comes into view stays cartoon in a result (cash stacks, food, goods) | it had no sheet in that segment (`reports/plan.md` lists each segment's sheets): add it to `subjects` if the survey missed it, or drop a subject that matters less there, then `plan` again |
| A subject that does not matter (a background crowd, a one-frame effect) | drop it from `subjects`: every sheet costs a picture and takes a reference slot |
| Two sources are the same ad in other sizes | `style_from` the first: the second's scene map takes its look from the first's |
| A scene map shows things that only come later (piles, built or lit objects): segments draw them from the start | move that scene's `views` to early moments, before the place fills up, then `refs --redo <key>` |
| A scene map invents places, keeps cartoon parts, characters, UI or text; a sheet changes the subject | adjust `materials`/`extra` or the views/box, then `refs --redo <key>` (at most five times, then keep the best and note it) |
| Gemini reports a flaw the original shows too | dismiss it (note it) |
| A confirmed flaw | `redo` that segment with a reason (at most five redos per segment). The first two redos of a flaw go without a new sentence or with a minimal `--prompt` about the look; once two have not fixed it, inject a targeted `--prompt` that names exactly how it must look or move (the things by name, as the original shows them: `whole tuna`, not `fish`), and change the pictures too where they cause it (`sheets`, `motion`, `split_at`). A sentence about where objects are can make the video model move its camera: on the next redo drop it with `--replace` |
| A segment fails on Lumos twice | shorter prompt (`extra` off), or split it differently with `split_at`; then render again |
| A segment still flawed after five redos | ask the user when the flaw would carry into the rest of the shot (it would be copied on); otherwise deliver and list each remaining issue with its time |

Stop to ask only when nothing can continue: footage missing or unreadable, no key, or the user does not scan the QR
code. Say what you tried and the minimum you need.

## Workflow

Commands and file formats: [workflow](references/workflow.md). Gate and checks: [review](references/review.md).

1. `init --work-order -` (JSON on stdin) creates `<output_root>/视频换风格_YYYYMMDD_HHMMSS/temp`, the job dir.
2. `analyze` finds cuts, the camera's speed, the end-card candidate and still overlays (measured; read
   `catalog/catalog.md`, the contact sheets are there for a doubt the survey leaves).
3. `survey` has Gemini watch each whole source once (6 frames/s): the moments that together show the whole location
   (views), the characters, objects and props that come into view (subjects, each re-boxed on its still), where the
   closing card starts, every logo, watermark, caption and warning line over the picture (to remove, each timed),
   the HUD (kept), and the stretches that are not gameplay (live action, real people, non-game animation: they go
   to `passthrough`). Gemini then checks the view frames and crops as stills (blurred, covered, late, wrong or cut-off
   subject). The command prints all of it as text with the `suggested` edits (`keep` and timed `remove`). Look at
   the end-card strip, each non-game strip, the one marked still per removal and each flagged picture; not the
   whole sheets.
4. Write the edits file: per source the kept range (`keep`, ending before the end card), the non-game stretches
   inside it (`passthrough`: this workflow restyles gameplay only; those stretches are cut out, never rendered, and
   the original footage goes back in at assembly), the real-world `materials`
   of what is on screen, what to `remove` (the suggested items you confirmed on their stills, timed when they come
   and go; HUD stays), and corrected `scenes`/`subjects` where a flag holds. `plan --edits <file>` cuts every
   kept range into segments of at most 30 s: at camera cuts where possible, otherwise where the camera moves
   fastest; writes `reports/plan.md` with each segment's references (the scene map and up to two sheets; only
   sheets some segment takes are made).
5. `refs` makes one scene map per scene (GPT Image 2.6, 4K, quality high: the views joined into one overview of the
   whole location, realistic, no characters, UI or text) and one three-view sheet per subject (1K, quality medium).
   Gemini checks each new picture against what it was made from (the map against its views: layout, cartoon parts,
   invented places, things that only come later, characters/UI/text; a sheet against its crop: the same subject,
   parts and colours kept, nothing added). Look at each flagged picture (`refs/check/<key>.png`, the inputs beside
   the picture), confirm or dismiss it, and redo the confirmed ones before rendering: every segment copies them.
6. `render` edits every segment with Seedance 2.5 (`video_edit`) on Lumos at 1080p: the source clip plus the scene
   map and at most two sheets of subjects on screen (those that fill the segment most, not counting ones the
   previous result already drew from a sheet). A subject without a sheet keeps the game's look. A segment that
   continues a shot also gets, as Image 1, the previous segment's result at the moment it begins (cut from that
   result's tail), so it is submitted only after you approved the segment before it: each `render` run renders
   the first segment of every shot (shots and sources side by side) and then, per shot, the next segment after an
   approved one, and stops there (`waiting`). A result that no longer shows the source's camera and layout (more
   likely with more sheets) is rejected and rendered once more with the scene map only (not counted as a redo).
7. `check` is the gate of every finished segment not approved yet. Gemini (6 frames/s, clips near-lossless up to
   the 256,000,000-byte inline limit; the watching is its job) watches the segment beside its source clip and looks
   at the frame the next segment will start from; then the script joins every segment up to this one (a preview)
   and Gemini watches the seam to the segment before it, 1 s either side beside the original, with the frame this
   segment was told to start from beside its own first frame. The check fails on a flaw in the segment, at the seam
   or in the handoff frame. In `review/check/<id>-<Kxx>-a<n>/` you get one three-frame sheet per issue; where Gemini
   found nothing at the seam or the handoff frame, you also get the seam's five-frame sheet (every 0.25 s from -0.5
   to +0.5 s) and the full-size handoff frame to review yourself (rule 7). `approve --segment <key>` takes a passed
   check; a failed one only with `--dismiss "<why each issue is wrong>"` after you saw every sheet. `redo` a
   confirmed broken one (a seam flaw: the later segment), then `render` again; the next segment of a shot renders
   only after the one before it is approved, so a flaw never travels on. An approved segment keeps its result when
   the segment before it is redone later; check it again then (its seam changed).
8. `assemble` puts every result on the source clock (and the original footage at each `passthrough` range) at one constant speed per clip, its seams pinned to the
   source's moments (the few holds or skips this needs go where the picture stands still), matches colours
   across seams inside a shot, switches at each such seam at the least visible frame of the overlap with a plain
   cut, or with a very light 6-frame dissolve where a cut would show (no other effects at seams: the user's rule),
   joins everything with the source audio and writes `<name>_realistic.mp4` (the style's label) into the delivery
   folder. There is no review of the whole video: every segment and every seam passed its check. Read the
   command's `problems` (measured): one there goes back to the segment it names.
9. Final summary, in your reply: videos delivered and the folder; per video the kept range, what was removed, the
   segments and seams with their checks (attempts, verdicts); issues dismissed (and why) or still open (with
   times); fallbacks taken (survey corrections, picture redos, segment redos).

## Rules

1. **Honest work.** Judge only from sheets, stills, reports and command output you read. Never claim a check passed,
   that you watched a video, or that a fix worked without the sheets or a new check to show it.
2. **Same video, new materials.** Composition, motion, timing, characters, counts and HUD stay as in the original;
   only the materials change, plus the removals. Never ask for added objects, text or effects.
3. **No logos, warning lines or end cards** in deliveries unless the user asks to keep them.
4. **Confirm before redoing.** Gemini can be wrong: look at the sheet of every issue before `redo`; redo only
   confirmed segments, never the whole video for a local flaw; never more than five times per segment.
5. **Approve before continuing.** Never approve a segment whose `check` you have not gone through, nor one whose
   seam or handoff frame shows a flaw (leftover text or logo, a wrong character or material, a jump at the join): the
   next segment of the shot is drawn from that frame and keeps the flaw. Redo it instead, and if its redos are
   spent, ask the user, saying the flaw will carry into the rest of the shot.
6. **Delivery folder.** It holds only the finished `.mp4` files and `temp/`. Everything else (edits drafts, stills,
   logs) lives in `temp/`; revisions reuse the same folder.
7. **Gemini first, then you; never only the report.** Gemini watches and reports; you confirm. Where it reported an
   issue, judge that issue on its own sheet (not on the seam sheet or handoff frame). Where it found nothing at a
   seam or a handoff frame, review that sheet or frame yourself. On every picture you look at (an issue sheet, a
   survey or refs flag, a removal strip, a seam sheet, a handoff frame), confirm or dismiss what it was made for and
   also look over the whole picture for anything else wrong (cartoon material left, text or logos, a changed or
   missing character, object or crowd, colours, the ground); what you find counts like a reported issue.
