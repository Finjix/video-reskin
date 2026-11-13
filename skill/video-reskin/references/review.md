# Remake review

Gemini compares each complete generated segment with the source at 6 frames/s on the same clock. No independent seam or transition review is performed.

UI, HUD and text use a relaxed standard in both Gemini review and main-agent verification. Mild blur, ghosting or jelly-like deformation during changes or movement is acceptable when they look normal when static and their overall placement remains reasonably stable. Do not require pixel-perfect shapes or frame-by-frame text and number matching during changes. Report only obvious drifting, severe distortion, persistent abnormal appearance when static, or major missing or added elements. Tolerated imperfections must not fail a segment or trigger a quality retry. Gameplay objects, characters, materials and motion retain their usual review standards.

Gameplay-to-gameplay joins receive a fixed 3-frame transition (0.125 seconds at 24 fps). Boundaries into or out of original/non-game footage and end cards are hard cuts. Assembly blends from the previous last frame into the next clip without removing timeline frames; no post-transition review is added.

For every segment or reference-image issue, the main agent must inspect its comparison sheet, verify the reported problem, and scan the entire picture for other defects. Confirmed defects require correction. False positives require a fresh model review before normal approval. A normal approval requires a passed current segment check.

After the initial generation, allow at most four quality retries (five versions total). If none passes, compare all five, inspect reported evidence, and use select-best with the selected generation ID, comparison findings and remaining defects. Keep failed verdicts and label acceptance best_available; continue subsequent segments and assembly. Service failures are recorded separately, not additional quality retries.

Tail suitability compares the previous generated last frame with the next source first frame ONLY for same-shot continuity, never material realism or rendering quality. It is not a video quality verdict. An unsuitable tail selects a converted first frame for the next segment; it cannot invalidate an approved segment or trigger/count a video quality retry. Both suitable and unsuitable decisions are saved. No agent issue-sheet review is required for this reference-selection decision.
