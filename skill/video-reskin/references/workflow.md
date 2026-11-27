# Commands and data

For Seedance edits that replace one or more objects per output, use the [element work order and commands](elements.md). The following commands describe full-frame style changes.

Use `scripts/video_reskin.cmd init --work-order order.json` with a work order such as this one.

```json
{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery","style":"dark"}
```

Use the printed `job_dir` as `--job-dir` for subsequent commands. With `init --job-dir`, name the folder `temp` inside the intended delivery folder. The delivery is `<source name>_S01_<style>.mp4`.

Use `--only S01` to select a source or `--segment S01:K02` to select a segment. `refs`, `render` and `check` default to the first unapproved segment of each selected source. Specify `--segment` for `approve`, `redo` and `select-best`.

## Survey and plan

Run `analyze` to create the silent 24 fps working copy. Open the sheets from `survey` in order. Their labels follow the source clock. Use `survey --only S01 --at 3.25` for a full-size source frame. Favor cuts at motion starts, camera changes and hard cuts. Record each distinct scene, character, prop and ground surface that needs coverage with a stable ID and the time of its first clear view.

Save the findings as JSON and run `plan --only S01 --input survey.json`. Times are in source seconds. Use the source duration as `end_card_start` when there is no end card. Cut kinds are `motion`, `camera` and `hard_cut`.

```json
{
  "non_game": [{"start": 0, "end": 1.5}],
  "end_card_start": 34.5,
  "cuts": [{"time": 27.5, "kind": "camera"}],
  "inventory": [{"id": "cart", "name": "Supply cart", "kind": "prop", "start": 4, "end": 22, "first_clear": 4.5}]
}
```

The plan uses the fewest segments of at most 30 seconds. When a continuous gameplay stretch needs several segments, each is at least 4 seconds. A shorter standalone stretch is padded with its last frame and trimmed after generation. Gameplay slivers under half a second beside non-game stretches stay original.

## References

Run `refs --segment S01:K02` to get the source range, relevant inventory, accepted images and any continuity images. When the command returns continuity images, compare the previous result's last frame with the current source first frame. Set `use_tail` to true when the scene and camera show consecutive views of the same shot. With `use_tail` set to false, the source first frame is converted automatically.

Choose additional frames on the source clock. List accepted reference IDs for content already covered. Submit with `refs --segment S01:K02 --input refs.json`.

```json
{"use_tail":true,"frames":[29.5],"references":["S01-K01-f108"]}
```

Open both files under `images` whenever the command returns a reference for inspection. After inspecting them, pass the returned `generation_id` to `refs --segment S01:K02 --accept <generation_id> --covers <id> <id>`, using the IDs visibly covered. Omit `--covers` when the image covers none of the listed items. This records acceptance and prepares the next reference. Add frames or accepted IDs with `--input` if coverage is incomplete. Use `refs --redo --segment S01:K02` to replace the segment's conversions, provided other segments do not use them.

The first conversion uses its source frame and `assets/styles/<style>.jpg` when available. Later conversions reuse accepted images that cover the relevant content. Prefer a smaller set of clear references. The renderer accepts up to 30 images.

## Segment review and delivery

When `refs` returns the reference list, run `render` and `check` for that segment. Check times start at zero for the segment. For example, `check --segment S01:K02 --at 1.5` extracts the source and result at 1.5 seconds into that segment. Inspect and record the result as described in [Review](review.md).

A new predecessor requires updated references and a new render for a continuing segment. Approved renders remain pinned to their generations when references change. Generation service failures are recorded separately from quality retries.

Gameplay joins blend four frames across the boundary. On the last two outgoing frames, the frozen incoming first frame contributes one quarter and one half. On the first two incoming frames, the frozen outgoing last frame contributes one half and one quarter. Original footage and end cards join with hard cuts. The source clock keeps picture and audio aligned.

The working copy uses H.264 CRF 16, slow, yuv420p and even source dimensions, with the short side capped at 1080 pixels. Delivery uses faststart MP4 with H.264 High, 8-bit yuv420p, CRF 16, medium, a 1080-pixel short side and 24 fps. Original audio, when present, is encoded as 48 kHz stereo AAC-LC at 128 kbps.

## Job files

- `job.json` and `catalog/` store the work order and normalized media information.
- `survey/` and `plans/` store source findings, half-open frame ranges and continuity.
- `references/` stores full-frame images, selections and accepted coverage. `reference-history/` stores replaced images.
- `image-tasks/` and `state/` store generation IDs, dependencies, retry history and approvals. `service-failures/` records failed service requests.
- `review/` contains survey and comparison sheets, full-size inspection frames and selections after retries. `checks/` links each current inspection to its generation.
- `segments/`, `raw/`, `ai/` and `work/` hold the upload clips, original renders, normalized renders and working media.
