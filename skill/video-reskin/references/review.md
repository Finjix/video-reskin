# Review

## Segments

Gemini compares each complete generated segment with the source at 6 frames/s on the same clock. Seams and transitions are not reviewed separately.

Judge the impression at normal playback speed, not paused or magnified frames. Subtle defects, brief minor clipping and small shape or texture inconsistencies that viewers are unlikely to notice in motion are tolerated. Report missed materials, content or motion changes and artifacts only when they are readily noticeable or change the main content or action.

UI, HUD and text are relaxed: mild blur, ghosting or jelly-like deformation while they change is fine if they look normal when static and stay in place. Report only obvious drift, severe distortion, persistent static defects or major missing or added elements.

Tolerated imperfections are not issues: a segment with nothing else wrong passes with an empty issues list. Gemini and the main agent apply the same standard.

## Reference images

Converted reference images are strict: every source logo and all text (numbers, brand marks, warnings, HUD labels) stays unchanged in content, lettering, font, color, shape, size, placement and appearance, even when material coverage is complete. Blur, occlusion and overlap already in the source stay as they are; hidden lettering is not reconstructed. Replace noncompliant images with `refs --redo` before rendering. This rule applies only to reference images, never to video prompts, tail checks or segment review.

## Main-agent verification

For every reported issue, open the native-size source/result frames (segments) or the original and generated files (reference images) listed in the report and scan each whole picture. Judge segment issues by their effect in motion, replaying the comparison clip when stills are not enough. Correct defects above the threshold; for false positives, including tolerated defects reported as failures, request a fresh review. Normal approval requires a passed current check.

## Retries

Quality retries happen only through an explicit `redo`, and corrective prompts never accumulate. After the initial generation, at most two retries (three versions). If none passes, compare all three, inspect the reported evidence and run `select-best --segment S01:K02 --generation-id <id> --reason "<comparison findings and remaining defects>"`. The failed checks are kept, acceptance is labelled best_available and the workflow continues. Service failures are recorded separately and do not count as retries.

## Tail continuity

When a segment continues the previous shot, Gemini compares the predecessor's last generated frame with the next source first frame and decides only whether they are the same continuous shot, ignoring materials and rendering quality. A suitable tail is used as the start frame with the continuation prompt; otherwise the next segment's first frame is converted. Both verdicts are saved. This is reference selection, not a quality verdict: it never rejects the predecessor, revokes approval or counts as a retry, and needs no main-agent review.
