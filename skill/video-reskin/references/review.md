# Remake review

Gemini samples every video at 6 frames/s. Reviews compare ORIGINAL with RESULT on the same clock.

- First segment: full segment review.
- Subsequent segments: joined-preview seam first, one second before and after (clamped at source ends). Every seam check stops before full segment review for an explicit agent decision.
- Original scene cuts and non-game boundaries are judged relative to the original.
- Gemini reports defects and issue sheets only. The main agent inspects them, including other defects in each picture, and chooses cut (0 frames), subtle transition (1-6 frames for minor differences), or redo of the later segment for major/content/motion changes. Record acceptance with `seam --segment S01:K02 --transition-frames N --reason "<visual findings>"`, then run check for full segment review; the seam is not reviewed again. Original cuts/non-game boundaries only allow 0 frames.
- Every Gemini-reported issue gets visual evidence. Seam and segment issues get three-frame original/result sheets, including failed seams that stop before full review. Failed reference-image checks get still original/result comparisons and saved reports. Tail checks are Gemini-only: save the verdict, with no agent-facing sheet or agent verification.
- For seam, segment and reference-image reviews (excluding Gemini-only tail checks), the main agent must view each flagged comparison, verify the reported issue, and also inspect the entire picture for other problems (materials, characters, objects, ground, colours, overlays and artifacts). Newly discovered defects count equally. Confirmed defects require correction; record dismissed seam false positives in the agent decision; for other false positives request a fresh Gemini check before approval. Do not approve or redo solely from the text report.
- A pass cannot contain unresolved issues. Approval requires a passed check bound to the current generation, predecessor and transition.
- redo invalidates approval and creates one new attempt. Maximum four quality retries after the initial attempt (five versions total). If none passes, the main agent compares all five and uses select-best to accept the best available version with its remaining defects documented. This is explicit best_available acceptance, not a passed review; preserve failed verdicts and continue later segments and delivery.
- A changed predecessor requires the successor seam to be checked again, but does not automatically regenerate the successor.
- Final assembly uses the transition parameters explicitly chosen by the main agent; no post-transition review is required.

The review schema carries English verdict/summary, clip-relative issue start/end/description only; transition_frames belongs to the main agent decision, not Gemini output. User-facing summaries may be Chinese.

Tail suitability compares the previous generated last frame with the next source first frame ONLY for same-shot continuity, never material realism or rendering quality. It is not a video quality verdict. An unsuitable tail selects a converted first frame for the next segment; it cannot invalidate an approved segment or trigger/count a video quality retry. Both suitable and unsuitable decisions are saved. No agent issue-sheet review is required for this reference-selection decision.
