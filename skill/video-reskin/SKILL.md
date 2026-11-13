---
name: video-reskin
description: Restyle game videos into realistic materials using a whole-video Gemini survey, incremental full-frame references and sequential Seedance editing, with full-segment review and restoration of non-game footage, end cards and original audio.
---

# Video reskin

Use Windows and the bundled entry point:
`& "<skill_root>/scripts/video_reskin.cmd" <command> ...`.
The private runtime downloads automatically. Never install another environment or write ad-hoc model/media scripts.

## Credentials

Run `auth` to check the stored Winky key and Lumos login. Missing key: ask the user, then `auth --key <key>`.
Missing login: run `login --no-open --timeout 1800`, show each new `qr:` image promptly and ask for a Feishu scan.
Do independent preparation while waiting. Never put credentials into jobs, reports or replies.

## Production

1. `init --work-order <json>`: sources and optional output_root. Defaults: realistic materials, 1080p, 24 fps.
2. `analyze`: build a silent 24 fps working copy at source size. Originals stay the audio and delivery source.
3. `survey`: Gemini watches the whole working copy once, marking non-game footage, end cards, candidate cuts and a content inventory.
4. `plan`: the fewest segments of at most 30 seconds, cut near semantic boundaries. Non-game parts and end cards are restored later, not generated.
5. `refs`: for the next unapproved segment of each source, Gemini picks full frames covering its scenes, characters, objects and ground, especially new props. The first frame is converted from its source alone; later ones also use earlier generated references. Each converted image is reviewed for realistic coverage and unchanged logos/text before rendering.
6. `render`: submit the segment cut from the working copy. A segment continuing the previous shot starts from the predecessor's last frame when Gemini judges it the same shot; otherwise, and after hard cuts or non-game gaps, it starts from a converted first frame. Sources proceed one segment at a time.
7. `check`: Gemini reviews the complete segment against the source under [review](references/review.md). For every reported issue, open the listed frames or images and verify it there.
8. `approve --segment S01:K01` after a passed current check, or `redo --segment S01:K01 --reason "<English reason>"`, then render/check again. After two failed retries, compare the three versions and `select-best` (see [review](references/review.md)).
9. Repeat refs/render/check/approve, then `assemble`: every segment approved, non-game footage and end cards restored on the source clock, original audio muxed, 1080p 24 fps. There is no whole-film model review.

## Recovery and delivery

Commands accept `--job-dir <temp>`; refs/render/check/approve/redo accept `--segment S01:K02`.
`status` shows per-segment state. A service failure preserves generation IDs; fix the cause and rerun.
`refs --redo --segment S01:K02` replaces that segment's references and archives the previous images.
New references do not rerender approved videos. Redoing a predecessor invalidates the following segment's check but keeps its video for rechecking.

All model prompts are English and live in `assets/prompts.json`. Do not add material lists, overlay instructions or accumulated correction sentences.

Final reply in the user's language: delivery folder, segment/check/retry outcomes and any unresolved issues.
Only finished MP4s and temp/ belong in the delivery folder.
Read [workflow](references/workflow.md) for commands and job data, [review](references/review.md) for review rules.
