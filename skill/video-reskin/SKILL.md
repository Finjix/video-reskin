---
name: video-reskin
description: Restyle game videos into realistic materials using a whole-video Gemini survey, incremental full-frame references and sequential Seedance editing, with full-segment review and restoration of non-game footage, end cards and original audio.
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
5. `refs`: prepare only the next unapproved segment of each source. Gemini selects full frames covering its scenes, characters, objects and ground, especially new props. The first converted frame uses the bundled style image; later conversions use generated references selected by Gemini. References are 2K/medium. Actual realistic coverage is checked before rendering. No panoramas or turnarounds.
6. `render`: submit a segment cut from the local 24 fps working copy, never the original source file, resuming its recorded generation ID after interruption. For continuous shots, Gemini compares the preceding generated last frame with the next source first frame and decides ONLY whether they belong to the same continuous shot. Ignore material realism and rendering quality in this decision; use the tail if continuous. If unsuitable, use a converted first frame of the next segment instead. This reference-selection decision must never reject the approved predecessor, trigger a video redo, or consume a quality retry. Save either verdict for traceability. Hard cuts and non-game gaps always start with a converted source frame. Add the continuation prompt only when the tail is actually used; preserve the source timeline either way. Each source proceeds sequentially.
7. `check`: review every complete generated segment against the source at 6 frames/s. There is no separate seam comparison, transition audit or manual seam decision. Apply relaxed UI/HUD/text review: tolerate mild blur, ghosting or jelly-like deformation during changes if the static appearance is normal and overall placement is reasonably stable; do not demand frame-by-frame text/number matching. Only obvious drift, severe distortion, persistent static defects or major missing/added elements warrant UI/HUD/text issues or retries. Keep usual standards for gameplay objects, characters, materials and motion. For each reported segment or reference-image issue, the main agent must inspect its comparison sheet and scan the entire picture; correct confirmed defects or request re-review of false positives. Tail continuity checks remain Gemini-only.
8. `approve --segment S01:K01` after reviewing the current passed check. Or `redo --segment S01:K01 --reason "<English reason>"`, then render/check again. All video quality failures share four retries per segment after its first attempt (five versions total). If none passes, the main agent compares all five, inspects reported issue sheets and selects the best available result with select-best --segment S01:K02 --generation-id <id> --reason "<comparison findings and remaining defects>". Continue subsequent segments and assembly with that selected result; retain failed verdicts and disclose remaining defects. No hidden quality rerenders or accumulated corrective prompts.
9. Repeat refs/render/check/approve. `assemble` requires every segment approved, restores all non-game footage and end cards on their source clock and muxes original audio. Delivery is 1080p, 24 fps. No whole-film model review is added.

Gameplay-to-gameplay joins use a fixed 3-frame transition (0.125 s at 24 fps). Any boundary into or out of original/non-game footage or end cards is a hard cut. Blend the previous last frame into the next gameplay clip's first 3 frames without shortening the timeline or changing the audio clock. No seam or post-transition model review is performed.

## Recovery and delivery

Commands accept `--job-dir <temp>`; refs/render/check/approve/redo accept `--segment S01:K02`.
`status` shows per-segment state. A service failure preserves generation IDs; fix the cause and rerun.
`refs --redo --segment S01:K02` explicitly replaces that segment's references and archives previous images.
New references do not rerender approved videos. Explicitly redoing a predecessor invalidates the following segment check, preserving its existing video for rechecking.
A segment shorter than two seconds is padded with its boundary frame and trimmed back at assembly.

All model prompts and dynamic context are English, centrally maintained in `assets/remake-prompts.json`.
Do not add material lists, overlay instructions, negatives or historical correction sentences. Original logos, warnings and HUD are compared against the source, not named in generation prompts.

Final reply in the user's language: delivery folder, segment/check/retry outcomes and any unresolved issues.
Only finished MP4s and temp/ belong in the delivery folder.
Read [workflow](references/workflow.md) for commands, [review](references/review.md) for review semantics.
