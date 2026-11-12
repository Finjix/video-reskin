# Remake review

Gemini samples every video at 6 frames/s. Reviews compare ORIGINAL with RESULT on the same clock.

- First segment: full segment review.
- Subsequent segments: joined-preview seam first, one second before and after (clamped at source ends). A failed seam stops the check before a full segment call.
- Original scene cuts and non-game boundaries are judged relative to the original.
- Only a passing continuous seam may recommend a transition (0–6 frames). After applying it, Gemini checks the final seam again before full review.
- No seam images are presented to the main agent. Read verdict, summary, issue interval and source-relative evidence; request another Gemini check when unclear.
- Segment issues produce three-frame original/result sheets for the main agent to confirm. Original defects and intended material changes are not new defects.
- A pass cannot contain unresolved issues. Approval requires a passed check bound to the current generation, predecessor and transition.
- redo invalidates approval and creates one new attempt. Maximum five quality retries after the initial attempt; stop that video at exhaustion. There is no force-approve bypass.
- A changed predecessor requires the successor seam to be checked again, but does not automatically regenerate the successor.
- Final assembly uses the same transition parameters as the checked preview.

The review schema carries English verdict/summary, clip-relative issue start/end/description and a transition_frames recommendation. User-facing summaries may be Chinese.
