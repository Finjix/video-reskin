# Validation record

The evidence behind the skill's current rules and thresholds. Each finding names the run it comes from.

## Runs

| Date | Footage | Setup | Outcome |
| --- | --- | --- | --- |
| 2026-11-03 | `.素材`: GOF3D10232 射冰取鱼 1080x1920, GOF3D10374 锤木过桥卖炭 1920x1080, GOF3D10057 甩枪获鱼 1080x1920 | Seedance 2.5 `video_edit` 1080p | 2 of 3 passed; open: whole tuna drawn as salmon steaks, a stack of thin logs drawn as one trunk |
| 2026-11-10/11 | GOFCN64128 抢鱼之熊 | Seedance 2.5, per-segment checks | belt items stood still in every result (see below) |

## Findings behind current rules

**Seedance timing.** Results come back 24 fps and slightly shorter than the input with the motion compressed
evenly (5.00 s → 4.71 s; 2026-11-03). Following the source frame by frame froze many moving frames and
`minterpolate` returned repeated frames (GOF3D10176, 2026-11-10), hence one plain duration normalization per clip,
no motion warping.

**Chaining inside a shot.** Segments of one shot rendered independently differed too much at the joins
(GOF3D10176, 2026-11-10). A segment that continues a shot starts from the previous result's last frame, so the
segments of one shot render one after the other.

**Prompt wording** (Seedance, 2026-11-03). Naming text that is not on screen made the model draw garbled text there;
materials present only late in the video appeared from the opening; `salmon` in a material list turned whole tuna and
cash stacks into salmon steaks. Hence no material lists in video prompts: the look comes from the reference pictures.

**Uploads.** A 20 MB 4K PNG timed out when Lumos fetched it as a reference (2026-11-09), so pictures over 8 MB are
uploaded as JPEG.

**Review.** Gemini (`gemini-3.8-flash`) found every defect later confirmed on stills except one garbled caption it
described as a ground change (2026-11-03). On a small crf-30 proxy it passed a visible 0.5 s dissolve and cartoon
trees, hence high-quality comparison clips up to the 256,000,000-byte inline limit (a 60 s 2160x1920 side-by-side
clip of about 200 MB was accepted, 2026-11-06). Gemini samples an inline video at `videoMetadata.fps`, not the
clip's frame rate. Every frame (24/s) cost about 256 tokens per frame at media resolution HIGH (2026-11-11);
6 frames/s for every clip is the user's ceiling, about 1,540 tokens per second of video.

**Per-segment gate, no whole-video review** (user rule, 2026-11-11). On GOFCN64128 every flaw the whole-video rounds
found had already appeared in a segment check, and each round cost a 207 MB, 86-340k-token clip. Approval therefore
needs a passed check of the current result, and there is no whole-film review. Without the gate one run chained a
whole shot from a flawed redo and re-rendered two clean segments (2026-11-10).

**Known limitations.** Items riding a running conveyor stood still in every Seedance result, with or without motion
sentences in the prompt, and no check catches it (GOFCN64128 K03, 2026-11-11). Fast-changing HUD counters are redrawn
by the video model and may show other digits for a few frames.

## Offline checks

`dev/test_bootstrap.py` (cold start in a Unicode temp path, JSON stdin, offline reuse) and `dev/run_tests.py` pass.
The tests mock all Lumos/Winky calls and use real FFmpeg media for chaining, padding, transitions and source-clock
assembly. No paid model calls are made; visual quality and live service acceptance are not verified by them.
