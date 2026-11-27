---
name: video-reskin
description: Use Seedance video editing to restyle gameplay in realistic or dark styles, or replace one or more prominent elements together. Design distinct replacement assets across output variants and preserve the original timing, end card and audio.
---

# Video reskin

Use the bundled Windows launcher `scripts/video_reskin.cmd`; it prepares the private runtime. Run `auth` to check the Lumos login. If needed, run `login --no-open --timeout 1800` and show each new QR image for a Feishu scan.

For 换元素 or any object substitution, follow the [element workflow](references/elements.md). Seedance must generate the replacement itself. Rework defects mainly by regenerating with Seedance. Use local texture overlays, tracking or per-frame masks only for localized defects that regeneration cannot fix, as the element workflow describes. The steps below cover full-frame style changes.

## Realistic and dark styles

1. Run `init --work-order <json>` with the source paths, optional `output_root` and the requested style. Use `realistic` for 写实 or realistic materials and `dark` for 暗黑 or a dark fantasy night in the game's stylized 3D look. Ask when the requested style is unclear.
2. Run `analyze` and `survey`. Read the source sheets, mark non-game intervals and the end card, and note clear appearances of scenes, characters, props and ground. Submit the findings with `plan --input` using the [command guide](references/workflow.md).
3. Run `refs` for the next unapproved segment. Choose the starting image and the fewest full frames covering the segment, prioritizing newly appearing content. Follow the [reference steps](references/workflow.md#references) and [image standards](references/review.md#reference-images).
4. Run `render`, then `check`. Open every sheet of synchronized source and result frames. Apply the [segment standards](references/review.md#video-segments) and record the decision with `approve`, `redo` or `select-best`.
5. Repeat for each segment, then run `assemble` to place gameplay, original footage, end cards and audio on the source clock.

Use `status` to resume. Commands reuse submitted generation IDs. If the service reports a failed generation, rerun the command to resubmit it.

Place work files in the job's `temp` folder. Report the delivery location, review and retry outcomes, any local post-production fixes with their frame ranges and methods, and any remaining defects in the user's language.
