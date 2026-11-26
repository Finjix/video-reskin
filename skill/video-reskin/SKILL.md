---
name: video-reskin
description: Use Seedance video editing to restyle gameplay in realistic or dark styles, or replace one or more prominent elements together. Design distinct replacement assets across output variants and preserve the original timing, end card and audio.
---

# Video reskin

For 换元素 or an object substitution, follow the [element workflow](references/elements.md). Survey the source once, then select replacements with a distinct silhouette and modeled structure at phone playback size. A variant may change several elements together. Give each replacement design to only one output in the batch. Banknotes are protected in every state and must retain their original appearance and motion. For a frozen variant such as timber or meat, keep the original model and encase it in a thick layer of ice. Show the original shape beneath the ice and make the ice thickness visible at phone playback size. Use this treatment in both the reference images and the Seedance edit prompt. Include a non-frozen alternative in multi-version batches. Use the same source survey and segment planner as realistic and dark edits. Keep action-specific instructions and references inside those segments. Count all submissions against the work-order limit and allow at most two retries per segment. Inspect and reuse previous takes before requesting another. Use the original video as the Seedance edit input for every generated interval. Inspect the returned video through the full actions before delivery.

The following sections apply to the realistic and dark styles.

Use Windows and the bundled entry point:
`& "<skill_root>/scripts/video_reskin.cmd" <command> ...`.
The private runtime downloads automatically. Never install another environment or write ad-hoc model/media scripts.

## Credentials

Run `auth` to check the stored Winky key and Lumos login. Missing key: ask the user, then `auth --key <key>`.
Missing login: run `login --no-open --timeout 1800`, show each new `qr:` image promptly and ask for a Feishu scan.
Do independent preparation while waiting. Never put credentials into jobs, reports or replies.

## Production

1. `init --work-order <json>`: sources, optional output_root and the required style, chosen from the user's request:
   - `realistic` for requests such as 转化为写实, 写实风格 or realistic: realistic materials.
   - `dark` for requests such as 转化为暗黑, 暗黑风格 or dark: an eerie dark night in the game's own stylized 3D look.
   Ask the user when the request names neither or both. Delivery is 1080p, 24 fps.
2. `analyze`: build a silent 24 fps working copy at source size, scaled down to 1080 on the short side when larger. Originals stay the audio and delivery source.
3. `survey`: Gemini watches the whole working copy once, marking non-game footage, end cards, candidate cuts and a content inventory.
4. `plan`: the fewest segments of at most 30 seconds and at least 4 seconds when a stretch needs several, cut near semantic boundaries; gameplay slivers under half a second between non-game parts stay original. Non-game parts and end cards are restored later, not generated.
5. `refs`: for the next unapproved segment of each source, Gemini picks full frames covering its scenes, characters, objects and ground, especially new props. The first frame is converted from its source alone, plus the style's example picture `assets/styles/<style>.jpg` when there is one (dark has one); later ones also use earlier generated references. Each converted image is reviewed for coverage in the job's style and unchanged logos/text before rendering.
6. `render`: submit the segment cut from the working copy. A segment continuing the previous shot starts from the predecessor's last frame when Gemini judges it the same shot; otherwise, and after hard cuts or non-game gaps, it starts from a converted first frame. Sources proceed one segment at a time.
7. `check`: Gemini reviews the complete segment against the source under [review](references/review.md). For every reported issue, open the listed frames or images and verify it there.
8. `approve --segment S01:K01` after a passed current check, or `redo --segment S01:K01 --reason "<English reason>"` (required), then render/check again. After two failed retries, compare the three versions and `select-best` (see [review](references/review.md)).
9. Repeat refs/render/check/approve, then run `assemble` to restore non-game footage and end cards on the source clock and encode the delivery. Export MP4 with H.264 High, 8-bit yuv420p, CRF 16, preset medium, a 1080-pixel short side, and 24 fps. Encode original audio, when present, as 48 kHz stereo AAC-LC at 128 kbps. There is no whole-film model review.

## Recovery and delivery

Commands accept `--job-dir <temp>`; refs/render/check/approve/redo accept `--segment S01:K02`.
`status` shows per-segment state. A service failure preserves generation IDs; fix the cause and rerun.
`refs --redo --segment S01:K02` replaces that segment's references and archives the previous images.
New references do not rerender approved videos. Once a predecessor changes, the following segment needs refs and render again: an unapproved result made from other references or after another predecessor is rendered anew and its quality retries start again. A failed generation is recorded under service-failures/ and the next run submits it again; it is not a retry. `refs --redo` is refused while another segment uses that segment's pictures.

All model prompts are English and live in `assets/prompts.json`; each style's image and video prompts and its target description are under `styles`. Do not add material lists, overlay instructions or accumulated correction sentences.

Final reply in the user's language: delivery folder, segment/check/retry outcomes and any unresolved issues.
Only finished MP4s and temp/ belong in the delivery folder.
Read [workflow](references/workflow.md) for commands and job data, [review](references/review.md) for review rules.
