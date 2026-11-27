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
For the same reason the banknote guidance is added only to element edits, not to style prompts.

**Uploads.** A 20 MB 4K PNG timed out when Lumos fetched it as a reference (2026-11-09), so pictures over 8 MB are
uploaded as JPEG.

**Per-segment approval.** When a run chained a shot from a flawed redo, it had to re-render two clean segments
(2026-11-10). Approve each current result before continuing the shot.

**Dark style** (user's example deliveries, 2026-11-18: GOFCN69238 and GOFCN69240 小兵数字门, GOFCN69241
大炮出兵数字门, all 1080x1920). Only the environment changes: road, railings, terrain, sky and distant buildings
become a gothic night world with firelight, skull banners and spiked parapets, keeping the layout. Soldiers, barrels,
number gates, cannons, effects, the logo, the disclaimer caption and the end card stay as they were, only relit.
The same gameplay appears in two themes (a frozen abyss with blue fire in 69238, lava and a blood moon in 69240;
69241 is frozen), so the prompt names only the style, like the realistic one. The samples stay saturated: deep
blue/purple shadows, bright fire, characters as bright as before. A "dark gothic game art" image prompt applied to
every element (GOFCN69114 射冰版, 2026-11-18) gave near-black, grey frames instead: the snow ground turned to dark
stone and fence posts to rune pillars. Hence no "gothic". The look the user then chose (`assets/styles/dark.jpg`, 2026-11-18) is a snowy deep-blue
night with pale snow, glowing cracked ice, amber lanterns, weathered dark wood and skull banners. Tests on
GOF3D10232 射冰取鱼 frames at 0 s and 7.5 s: the prompt alone drew the banners, lanterns and dark wood but added a
moon whenever it said "moonlit" and left the ice dull; the example picture alone matched ice, snow and blue but
dropped banners and lanterns; the example picture plus the descriptive prompt matched on both frames. Hence the
picture is the first conversion's look and the prompt names the traits. Snow and ice are left out of the prompt
because not every game is set in snow: without them a log-sawing conveyor frame (`D:/temporary/v.mp4`) became a dark
night with lanterns and skull banners and gained no snow, while the 射冰取鱼 frame at 7.5 s kept its pale snow and
glowing ice from the picture. The banners were later changed from skulls to pale ice-blue snowflakes, in
both the prompt and the example picture. Not yet checked through a full video.

**Known limitations.** Items riding a running conveyor stood still in every Seedance result, with or without motion
sentences in the prompt (GOFCN64128 K03, 2026-11-11). Fast-changing HUD counters are redrawn
by the video model and may show other digits for a few frames.

**Encoding.** Working copies are at most 1080 on the short side and intermediate encodes use CRF 16. Delivery uses
faststart MP4, H.264 High, 8-bit yuv420p, CRF 16, preset medium and 24 fps.
Source audio is encoded as 48 kHz stereo AAC-LC at 128 kbps when present.

## Offline checks

`dev/test_bootstrap.py` (cold start in a Unicode temp path, JSON stdin, offline reuse) and `dev/run_tests.py` pass.
The tests mock Lumos calls and use real FFmpeg media for sampling, chaining, padding, transitions and source-clock
assembly.
