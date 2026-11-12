---
name: video-reskin
description: Restyle game videos into realistic materials using a whole-video Gemini survey, incremental full-frame references and sequential Seedance editing, with seam-first review and restoration of non-game footage, end cards and original audio.
---

# Video reskin — remake

Use Windows and the bundled entry point:
`& "<skill_root>/scripts/video_reskin.cmd" <command> ...`.
The private runtime downloads automatically. Never install another environment or write ad-hoc model/media scripts.

## Credentials

Run `auth` to check the stored Winky key and Lumos login. Missing key: ask the user, then `auth --key <key>`.
Missing login: run `login --no-open --timeout 1800`, show each new `qr:` image promptly and ask for a Feishu scan.
Do independent preparation while waiting. Never put credentials into jobs, reports or replies.

## Production

1. `init --work-order <json>`: sources and optional output_root. Defaults: realistic materials, 1080p, 24 fps. New jobs use schema 2; schema 1 jobs belong to the old branch.
2. `analyze`: create a complete local 24 fps working copy. Original files remain the audio and delivery source.
3. `survey`: Gemini watches the whole working video at 6 frames/s once, identifying non-game footage, end cards, candidate cuts and an unlimited content inventory.
4. `plan`: minimum segment count, at most 30 seconds each, favouring semantic cuts near the limit. Non-game parts and end cards are excluded from generation and restored later. No edits file is needed.
5. `refs`: prepare only the next unapproved segment of each source. Gemini selects full frames covering its scenes, characters, objects and ground, especially new props. The first converted frame uses the bundled style image; later conversions use generated references selected by Gemini. References are 4K/high. Actual realistic coverage is checked before rendering. No panoramas or turnarounds.
6. `render`: submit that segment, resuming its recorded generation ID after interruption. Only continuous shots use the preceding approved video's exact last frame as Image 1. Hard cuts and non-game gaps start with a converted source frame. Each source proceeds sequentially.
7. `check`: first segment gets a full comparison at 6 frames/s. Later segments first get a joined-preview seam comparison, one second on each side at 6 frames/s; failure skips full review. Passing seams unlock full segment review. The main agent reviews seam reports only, never seam media; ask Gemini again if its evidence is unclear. Inspect the comparison sheets of segment issues. Do not claim visual checks without evidence.
8. `approve --segment S01:K01` after reviewing the current passed check. Or `redo --segment S01:K01 --reason "<English reason>"`, then render/check again. All video quality failures share five retries per segment after its first attempt. At exhaustion stop that source and report; other sources continue. No hidden quality rerenders or accumulated corrective prompts.
9. Repeat refs/render/check/approve. `assemble` requires every segment approved, restores all non-game footage and end cards on their source clock and muxes original audio. Delivery is 1080p, 24 fps. No whole-film model review is added.

Default join: cut. Only a passing seam with Gemini's recommendation gets a transition of at most six frames, using the preceding tail over the incoming frames without removing time or fading through black. The final altered seam is checked again. Original hard cuts and non-game boundaries retain their original transitions.

## Recovery and delivery

Commands accept `--job-dir <temp>`; refs/render/check/approve/redo accept `--segment S01:K02`.
`status` shows per-segment state. A service failure preserves generation IDs; fix the cause and rerun.
`refs --redo --segment S01:K02` explicitly replaces that segment's references and archives previous images.
New references do not rerender approved videos. Explicitly redoing a predecessor invalidates the following seam check, preserving its existing video for rechecking.
A segment shorter than two seconds is padded with its boundary frame and trimmed back at assembly.

All model prompts and dynamic context are English, centrally maintained in `assets/remake-prompts.json`.
Do not add material lists, overlay instructions, negatives or historical correction sentences. Original logos, warnings and HUD are compared against the source, not named in generation prompts.

Final reply in the user's language: delivery folder, segment/check/retry outcomes and any unresolved issues.
Only finished MP4s and temp/ belong in the delivery folder.
Read [workflow](references/workflow.md) for commands, [review](references/review.md) for review semantics.
