# Commands and data

Entry: `scripts/video_reskin.cmd`. Job creation:

```json
{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery"}
```

`init --work-order order.json` prints job_dir. Subsequent commands require `--job-dir <job_dir>`.

Sequence: analyze → survey → plan → refs → render → check (full segment) → approve, then repeat refs/render/check/approve for the next segment, finally assemble.
Default refs/render/check choose the first unapproved segment of each selected source.
Use `--only S01` to select sources and `--segment S01:K02` for a segment.
approve and redo should specify a segment explicitly. redo needs an English reason.
auth/login/runtime take their own account/runtime options.

Job data:
- job.json: source identity, output root, fixed 24 fps timeline.
- catalog/: normalized media information.
- survey/: non-game intervals, end-card boundary, semantic cut candidates and English content inventory.
- plans/: half-open source-frame ranges, passthrough ranges, continuity and fixed transition policy.
- references/: full-frame images with original logos and all text unchanged, actual coverage IDs, generated-image dependencies, segment reference lists and predecessor generation identity.
- image-tasks/: persisted image generation IDs for resuming.
- state/: video generation ID, retry count/history and version-bound approval.
- checks/: segment verdicts, issue times and current-generation token.
- review/: Gemini comparison clips, independent native-size PNG frames for segment issues (frames entries with at/original/result), and failed reference-image reports pointing directly to original/result image files (images entries). No agent comparison sheets are generated. Tail checks keep Gemini verdicts only and require no agent review.
- raw/: original model outputs; ai/: outputs normalized to segment duration.
- work/: temporary previews and local normalized media.

An explicitly redone predecessor preserves existing later outputs but invalidates their dependent approval.
Approved results stay pinned when references change.
Every quality retry is explicit and counted; two retries after the initial generation is the limit (three versions total). At exhaustion, compare the three outputs and select the best with select-best --segment S01:K02 --generation-id <id> --reason "<comparison findings and known defects>". Continue the workflow; retain the failed check and label acceptance as best_available.
References can be explicitly replaced with refs --redo; previous generated images are archived. Replace noncompliant reference images before rendering. Unchanged logos/text is a reference-image generation and reference-image review requirement only; preserve source-existing blur, overlap and occlusion. Video generation and segment review keep their own prompts and relaxed acceptance rules.

Prompts are solely in assets/prompts.json.

Gameplay joins use a fixed 4-frame transition (1/6 second at 24 fps), spanning the outgoing last two frames and incoming first two frames. Outgoing weights are 3/4, 1/2, 1/2, 1/4; incoming weights are 1/4, 1/2, 1/2, 3/4. For the outgoing frames, overlay the frozen incoming first frame; for the incoming frames, overlay the frozen outgoing last frame. The incoming third frame is fully incoming. Boundaries into or out of original/non-game footage and end cards are hard cuts. Preserve total frame count and audio alignment. There is no seam or post-transition review.

Tail suitability checks ONLY whether the previous generated last frame and next source first frame depict the same continuous shot, ignoring material realism and rendering quality. It is reference selection only: save the Gemini verdict, use the approved predecessor's exact tail when suitable, otherwise convert the next segment's first frame. Never reject or redo the predecessor, revoke its approval, or consume retries because of this decision. Source continuity stays on the original clock; the continuation prompt is used only with an actual tail reference.

Media inputs: Gemini survey uses the full-size, silent 24 fps working copy (H.264 CRF 16, slow, yuv420p). Full-segment comparison videos use CRF 16, slow, yuv420p, retaining each panel at working-copy dimensions, 24 fps with 6 fps Gemini sampling and the inline-size bitrate cap. Gemini reference-image review, tail continuity and reference selection send the original image files without resizing or JPEG conversion, including candidate references. Main-agent issue review opens independent native-size frames and original reference files; no reduced or stitched comparison images. Gemini reports issue times/descriptions; local FFmpeg extracts the frames.
