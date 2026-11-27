# Review

## Video segments

Open each sheet returned by `check` and scan the complete segment in time order. Each pair shows the source and the result at the same moment, sampled six times per second. Use `check --at <seconds>` for an independent full-size image from each video. When stills leave motion unclear, play the reported source range and the result at normal speed.

Judge style against the task's target and quality by what a viewer sees during normal playback. Flag visible style gaps, unexpected changes in content or movement, and noticeable artifacts. Tolerate subtle texture differences, brief minor clipping and small shape inconsistencies. Treat defects present in the source as part of the baseline.

For UI, HUD and text, tolerate mild blur, ghosting and deformation during changes if placement stays stable and the elements look normal when static. Flag obvious drift, severe distortion, persistent defects or major missing or added elements.

Run `approve --segment S01:K01` once the current result passes inspection. For a noticeable defect, run `redo --segment S01:K01 --reason "<English description and time range>"`, then render and inspect again. Each segment allows two quality retries. If all three versions fall short, inspect earlier versions with `check --segment S01:K01 --generation-id <id>`. Use `select-best --segment S01:K01 --generation-id <id> --reason "<comparison and remaining defects>"` to record the choice and proceed.

## Reference images

Open the source and converted image files reported by `refs` at full size. Record inventory items visibly rendered in the target style. Each logo and piece of text, including numbers, warnings and HUD labels, must keep its original content, lettering, font, color, shape, size and position. Preserve any existing blur, occlusion or overlap. Accept a conforming result with `refs --accept`. Replace a defective image with `refs --redo` before rendering.
