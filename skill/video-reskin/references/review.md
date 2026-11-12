# Remake review

Gemini samples every video at 6 frames/s. Reviews compare ORIGINAL with RESULT on the same clock.

- First segment: full segment review.
- Subsequent segments: joined-preview seam first, one second before and after (clamped at source ends). A failed seam stops the check before a full segment call.
- Original scene cuts and non-game boundaries are judged relative to the original.
- Only a passing continuous seam may recommend a transition (0–6 frames). After applying it, Gemini checks the final seam again before full review.
- Every Gemini-reported issue gets visual evidence. Seam and segment issues get three-frame original/result sheets, including failed seams that stop before full review. Failed reference-image and tail checks get still original/result comparisons and saved reports.
- For ALL reviews, the main agent must view each flagged comparison, verify the reported issue, and also inspect the entire picture for other problems (materials, characters, objects, ground, colours, overlays and artifacts). Newly discovered defects count equally. Confirmed defects require correction; dismiss original defects/intended material changes with an explanation and request a fresh Gemini check before approval. Do not approve or redo solely from the text report.
- A pass cannot contain unresolved issues. Approval requires a passed check bound to the current generation, predecessor and transition.
- redo invalidates approval and creates one new attempt. Maximum five quality retries after the initial attempt; stop that video at exhaustion. There is no force-approve bypass.
- A changed predecessor requires the successor seam to be checked again, but does not automatically regenerate the successor.
- Final assembly uses the same transition parameters as the checked preview.

The review schema carries English verdict/summary, clip-relative issue start/end/description and a transition_frames recommendation. User-facing summaries may be Chinese.
