# Remake commands and data

Entry: `scripts/video_reskin.cmd`. Job creation:

```json
{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery"}
```

`init --work-order order.json` prints job_dir. Subsequent commands require `--job-dir <job_dir>`.

Sequence: analyze → survey → plan → refs → render → check (full segment) → approve, then repeat refs/render/check/approve for the next segment, finally assemble.
Default refs/render/check choose the first unapproved segment of each selected source.
Use `--only S01` to select sources and `--segment S01:K02` for a segment.
approve and redo should specify a segment explicitly. redo needs an English reason.
auth/login/runtime retain their existing account/runtime options.

Schema 2 data:
- job.json: source identity, output root, fixed 24 fps timeline.
- catalog/: normalized media information.
- survey/: non-game intervals, end-card boundary, semantic cut candidates and English content inventory.
- plans/: half-open source-frame ranges, passthrough ranges, continuity and fixed transition policy.
- references/: full-frame images, actual coverage IDs, generated-image dependencies, segment reference lists and predecessor generation identity.
- image-tasks/: persisted image generation IDs for resuming.
- state/: video generation ID, retry count/history and version-bound approval.
- checks/: segment verdicts, issue times and current-generation token.
- review/: Gemini comparison clips and agent-facing segment issue sheets; segment issue sheets, plus failed reference-image comparisons. Tail checks retain Gemini verdicts only and require no agent review.
- raw/: original model outputs; ai/: outputs normalized to segment duration.
- work/: temporary previews and local normalized media.

An explicitly redone predecessor preserves existing later outputs but invalidates their dependent approval.
Approved results remain pinned when references change.
Every quality retry is explicit and counted; four retries after the initial generation is the limit (five versions total). At exhaustion, compare the five outputs and select the best with select-best --segment S01:K02 --generation-id <id> --reason "<comparison findings and known defects>". Continue the workflow; retain the failed check and label acceptance as best_available.
References can be explicitly replaced with refs --redo; previous generated images are archived.
Old schema 1 jobs are rejected, not migrated.

Prompts are solely in assets/remake-prompts.json. The old internal modules remain as tested media/account helpers; their schema-1 orchestration is not used by the entry point.

Gameplay joins use a fixed 4-frame transition (1/6 second at 24 fps), spanning the outgoing last two frames and incoming first two frames. Outgoing weights are 3/4, 1/2, 1/2, 1/4; incoming weights are 1/4, 1/2, 1/2, 3/4. For the outgoing frames, overlay the frozen incoming first frame; for the incoming frames, overlay the frozen outgoing last frame. The incoming third frame is fully incoming. Boundaries into or out of original/non-game footage and end cards remain hard cuts. Preserve total frame count and audio alignment. No seam or post-transition review is added.

Tail suitability checks ONLY whether the previous generated last frame and next source first frame depict the same continuous shot, ignoring material realism and rendering quality. It is reference selection only: save the Gemini verdict, use the approved predecessor's exact tail when suitable, otherwise convert the next segment's first frame. Never reject or redo the predecessor, revoke its approval, or consume retries because of this decision. Source continuity remains on the original clock; the continuation prompt is used only with an actual tail reference.
