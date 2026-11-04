# Review loop

`review` sends Gemini (`gemini-3.8-flash`) one clip with the original and the restyled video side by side (left/right
for vertical videos, top/bottom for horizontal ones), frame-synchronised, with the seam times and the removal list,
then a 3-second close-up of every seam (patch joins included) at 10 fps with one question: does the restyled scene
change there? It adds the seams `assemble` measured as `slight` (low: rarely visible in playback) or `visible` (high).
It draws the stills of every issue and asks Gemini once more, on those stills, whether each is visible and whether the
original shows the same: the `triage` hint. Gemini also rates the realism of the restyle over time (`realism`,
5 = looks like real camera footage, 4 = real with small game-like traces, 3 = mixed: prominent objects still look like
a game, 2 = mostly a game/CG look, 1 = cartoon) and lists the objects that still look game-like; a range rated 3
becomes a medium `not_realistic` issue, 1-2 a high one (source `gemini-realism`). The report and `run` print the
timeline (`realism: 0.0-22.9 s 4/5; 45.6-65.7 s 2/5 (皮卡车, 顾客)`). Findings of segment checks the editor accepted
go along to every later check and to the review, which do not report them again.

The identity check keeps every recurring character and object at one look from the first second to the last (a slow
drift passes the video review: in the GOF3D10057 run the protagonist, a realistic adult in a navy parka with a grey
fur hood on the keyframes, slid back within the first segment into the game's light-blue chibi, and the later
segments kept that). Gemini finds the recurring subjects on the original frames (the protagonist always, up to three
more: customers, a vehicle, a machine), the program cuts close-ups of each from the styled keyframes (REF) and from
moments across the video, and Gemini judges each pair (reference | moment | original small) on fixed attributes:
build (realistic, stylized, chibi), rendering (photo_real, game_cg, cartoon) and colours. A step in build or rendering
or other colours is a difference; three moments in a row or a step of two is high. The issues (`identity`, source
`gemini-identity`) name the subject, its reference look and what it turned into; the sheets
`review/<id>-identity-r<n>-subjects.jpg` (close-ups, REF first) and `-frames.jpg` (whole frames) show it. The segment
check runs the same on each segment against its shot's keyframe and the anchor, and `preview` (and `run`) on the
previews (`sheets/<id>-preview-identity-subjects.jpg`), since each preview is restyled on its own and a midframe that
draws another person carries that person into the video. Issues come in nine categories:

| Category | Means |
| --- | --- |
| `continuity` | 穿帮: something differs from the original (cartoon material left, an object changes shape, colour or count, things appear or vanish, motion no longer matches) |
| `artifact` | bug: morphing, melting, flicker, jitter, frozen frames, garbled HUD text or numbers, strong noise or a fragmented look |
| `added_text` | captions, warning lines, watermarks or garbled characters the original does not have (Seedance draws them when a prompt names text that is not on screen) |
| `seam_color` | 拼接色差 / scene change at a seam: colour, brightness, texture, terrain, cable colours or realism jump; source `measured` (seam measurement or colour residual), `gemini-seam` (close-up) or `gemini` |
| `leftover_overlay` | a logo, watermark or warning line still visible |
| `end_card` | closing-card content still visible |
| `style_drift` | the look changes between parts of the video |
| `identity` | a recurring character or object looks different in another part of the video (clothes, colours, build, face, species, material, realism), compared with the styled keyframes; a gradual drift counts |
| `not_realistic` | an object or area still looks like a game or cartoon, not real footage: rounded blob shapes, toy vehicles and machines, doll or chibi people, plastic or clay surfaces, flat fills, outlines, candy colours |

Each issue lists the segments it falls in (`S01:K03`). Gemini is a second pair of eyes, not the judge.

## Confirm on stills

For every issue look at its stills in `review/issues/<id>-r<n>-p<k>.jpg` (more moments: `frames --source <id> --at
<start> [<end>]`): the upper row is the original, the lower row the result, at T-0.3 s, T and T+0.3 s. The triage hint
is a second opinion only. Decide:

- **Confirmed**: the bottom row shows the flaw and the top row does not. Note what exactly is wrong.
- **Dismissed**: the original shows the same thing (game physics, clipping, motion blur), the flaw is not visible on
  the stills, or it is the intended material change. Note why.
- For a seam issue look at the seam time itself and 0.3 s on either side; a step in colour or texture between the
  middle and one side is a confirmed seam issue.
- **Judge realism yourself on every sheet**, also where Gemini rates it 4-5: the restyle must look filmed by a real
  camera. Rounded snow blobs, cone trees, toy-like trucks, doll-like people, plastic-looking fish, flat white snow
  or candy-bright colours in the lower row are confirmed `not_realistic` flaws. Compare with the preview of the same
  moment (`sheets/<id>-preview.jpg`): when the preview is real and the video is not, the segment drifted back to
  the source's look (see "Realism that fades" below).

## Redo

`redo --segment S01:K03 --reason "<what is wrong>" [--prompt "<one corrective Chinese sentence>"] [--keyframe]`

- `--prompt` is added to that segment's video prompt for every later attempt; keep it to one sentence about the
  confirmed flaw, e.g. `鱼的形状和数量与原视频保持一致，不要变形`, `去掉右上角残留的logo文字`,
  `整体颜色与前一段保持一致，雪地为白色`.
- Name only the part that changes and what must stay, with the moment in seconds into the segment (the segment's
  start is in `reports/plan.md`). Colour words leak onto whatever is nearby: `机械抓斗吊臂由蓝色变为红色` turned the
  tuna the arm was holding red; `本段约16秒处金色升级闪光过后，只有机械抓斗吊臂的金属立柱和横臂换成红色涂装；被抓起和河里的金枪鱼始终保持原本的蓝白色和银色鱼身，绝不变红。`
  fixed it.
- Corrective sentences accumulate over attempts. When one made things worse, `redo ... --replace-prompt --prompt
  "<new sentence>"` drops the earlier ones (never edit `config/redo.json` by hand).
- `--keyframe` when the keyframe already shows the flaw (wrong objects, logo kept, colour cast): run
  `keyframes --redo S01:K03` and check the sheet before rendering.
- A seam jump: look at `sheets/<id>-seams.jpg` and the stills at the seam time, then redo the segment after the
  seam (it starts again from the previous result's frame; the later segments of that shot follow). If the same seam
  jumps again, move it to a stiller moment with `split_at` (plan again: the shot's segments render anew).
- A seam measured `visible` while the seam sheet shows the same scene on both sides and the Gemini close-up calls it
  consistent: check whether the original itself changes abruptly there (an effect bursting, a stack of bills flying
  off, a flash). The measure counts that motion as a jump. Confirm it on `frames --source <id> --at <seam time>`;
  if so, dismiss it with that reason (a redo renders the same moment again); before rendering, a `split_at` a
  second away keeps the seam off such a moment.
- Whole-segment redo is the default. A short (a few seconds), local flaw inside a long segment whose later
  segments are done can be patched instead (`patch --segment S01:K03 --at <start> <end> --reason ... [--prompt ...]`):
  only 4-8 s render again and the rest of the shot stays. Not for flaws spread over the segment, a wrong colour or
  look of the whole segment, fast camera motion or a cut at the patch's edges. After `assemble` both patch joins
  must be clean or slight; a visible one -> `patch --drop S01:K03:P1`, then `redo` the segment.
- **A subject that changes look** (`identity`): confirm on the close-up sheet (REF columns first, red titles).
  It starts inside a segment and every later segment of the shot inherits it, so `redo` the segment where it starts,
  with the subject named by its keyframe look in `materials` (`身穿深蓝色派克大衣、灰色毛领兜帽、深色长裤的写实成年男子主角`)
  and a corrective `--prompt` (`主角从头到尾保持图片1里深蓝色派克大衣、灰色毛领的写实成年男子外观，不要变成浅蓝色Q版小人`), plus
  `midframes` across the segment when it drifts late. On the previews: `preview --at T --redo` for one that drew the
  subject differently.
- **Realism that fades** (the video turns cartoonish later in a segment or along a shot, while the previews of
  those moments are real): Seedance holds the realism of its reference picture, the first frame, on what that frame
  shows; what comes into view later, and every later segment of a chain (it starts from the drifted frame), slides
  back towards the source. Add `midframes` to the source's edits: the preview times inside the segment where the
  look fades or new objects arrive (at most 3 per segment, at least 2 s after its start; `"auto"` takes the regular
  preview moments of every segment). Those previews go along as extra reference pictures, each with a sentence
  naming its second in the segment. `plan`, look at the marked previews (`(Seedance reference)` on the sheet: they
  must be right, the video copies them), then `redo` the segment (adding midframes alone also renders it again).
  Name late objects by their real look in `materials` too.
- Garbled screen-fixed text or UI needs no render: an `overlays` item over it (`box`/`poly`, no `colors`), `plan`,
  `assemble`.
- Then `run` (only redone segments and patches render, with their checks; assembly and review follow per source).
  Each review round is numbered; after `review_rounds` rounds deliver and list what is still open.

## Held segments (segment check)

`run`/`render` checks every segment that has a successor in its shot before the successor starts: the seam to the
segment before it is measured, and Gemini compares the segment with its original (same categories; the HUD and
burned overlays are not in yet). A visible seam, a high issue or two medium ones hold the segment; the rest of the
shot waits, other shots go on. Confirm on `review/gate/<id>-<Kxx>-p<k>.jpg` like a review issue: `redo` (one segment
renders again instead of the whole shot) or `accept --segment <key> --reason <why>`. The check rates realism
too (`realism (5 = real footage): ...` under the held segment); a segment that drifts back to the game look in its
later seconds is held, since every later segment of the shot would start from it. An accepted finding (and its
reason) is passed to the later checks, so the same flaw continuing in the next segments does not hold them again.

- `segment check failed (...); continuing unchecked` (a Gemini error): the successor renders without the check; look
  at that segment's frames (`frames` after assembly) before you trust the review alone: an unchecked segment in the
  GOF3D10057 run kept a crane blue after its upgrade, and the shot's last segment had already rendered from it.
