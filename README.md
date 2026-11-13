# video-reskin — remake

将游戏画面转换为写实材质的 Windows Agent Skill。新版先看完整视频，再逐段补齐完整帧参考图、生成、审片；输出 1080p / 24 fps，最终放回非游戏画面、尾板和原声。

## 工作流程

1. 本地生成完整 24 fps 工作副本，Gemini 以每秒 6 帧分析全片。
2. 标记非游戏区间、尾板及内容清单，按不超过 30 秒、少切原则选择运动开始或镜头变化附近的切点。
3. Gemini 逐段选择能覆盖场景、人物、物品、地面的完整帧，重点补齐新道具。第一张用技能风格图；以后仅使用生成图作为参考，2K/medium。
4. 连续镜头由 Gemini 对比上段生成尾帧与下一段源首帧，只判断是否为同一镜头的连续画面；是则使用尾帧作参考，否则转换下一段首帧。此处不判断材质或生成质量，不打回上段、不消耗重试次数。场景硬切或非游戏间隔后始终转换首帧。
5. Seedance 输入本地 24 fps 工作副本切出的片段，生成首段后由 Gemini 完整审核，主 agent 校对后批准。
6. 每段直接与原片完整对照审核，不再单独审核接缝或过渡。主 agent 必须检查片段和参考图的问题图，核实报告并通看整图；尾帧只判断镜头连续性。
7. 每段首次生成后共用最多 4 次质量重试，共五个版本。若均不合格，比较五版后用 select-best 选择最好的一版，保留审核失败记录和已知问题，再继续后续片段及合成。
8. 游戏片段之间使用 4 帧过渡（24 fps 下为 1/6 秒），覆盖接缝前后各两帧。上一段占比依次为 3/4、1/2、1/2、1/4，下一段为 1/4、1/2、1/2、3/4；前两帧叠加静止的下一段首帧，后两帧叠加静止的上一段尾帧，下一段第三帧恢复完全显示。非游戏画面、原片直通区间及落板边界均硬接。总帧数和原声音轨不变，不再进行过渡审核。

没有全景图、三视图、材质长清单、累积纠正提示或最后额外的全片模型审片。

整体审核以正常速度播放时的观感为准：允许少量细微穿帮、短暂轻微穿模、局部形状或纹理小偏差等动态观看不易察觉的问题，不以暂停、放大或逐帧找瑕疵作为验收标准。主要拦截正常观看容易发现的材质漏改、明显内容变化、动作错误及生成瑕疵，或实质改变主体内容和动作的问题。容许范围内的小问题不列为待修复项、不打回、不消耗重试；只有这些小问题时应通过。主 agent 问题图复核也按同一动态观感标准，必要时回看对应对照视频判断。

UI、HUD 和文字采用宽松审核：变化或运动中的轻微模糊、重影、果冻感可以接受，只要静态看起来正常、整体位置不明显漂移；不要求变化过程中逐帧文字、数字或形状完全一致。这类轻微问题不打回、不触发质量重试。只有明显漂移、严重变形、静态持续异常或主要元素增减才报问题。Gemini 和主 agent 复核使用同一标准。

仅参考图生成与审核要求 Logo 和所有文字保持源图原样，包括内容、字形、字体、颜色、形状、尺寸、位置及原有外观。不得写实化、浮雕化、改色、变形、替换或增删。源帧已有的模糊、遮挡和重叠需保留，不移动元素、不补造被挡文字。此要求不注入视频生成提示词，不改变视频、尾帧及整段审核规则。

## 使用

技能入口：`skill/video-reskin/scripts/video_reskin.cmd`。先 auth / login，再 init --work-order order.json。
工单示例：`{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery"}`。

其余命令加 `--job-dir <任务 temp 目录>`：
`analyze → survey → plan → refs → render → check → approve --segment S01:K01`，重复逐段循环后 assemble。
`status` 查进度；`redo --segment S01:K02 --reason "Visible material jump"` 回炉视频；
`refs --redo --segment S01:K02` 重做本段图片。
服务中断后复用已记录的任务 ID。新参考图不自动重生成已批准片段；前段重做后，后段保留结果并重查接缝。

新任务 schema 2；旧任务在旧分支继续。运行环境、Lumos 登录与 Winky 凭据沿用现有实现，凭据仍由 Windows DPAPI 加密保存。

## 全部默认提示词

全部集中在 [remake-prompts.json](skill/video-reskin/assets/remake-prompts.json)，动态上下文使用英文。

| 用途 | 提示词 |
| --- | --- |
| 全片分析 | Watch the full video. Mark gameplay, non-game footage and end cards. Choose as few segments as possible, up to 30 seconds each, with cuts near movement starts or camera changes. List scenes, characters, objects and their first clear appearances. |
| 选帧与选参考 | Choose the fewest clear frames covering this segment's scenes, characters, objects and ground, especially newly appearing items. Select existing generated references for content already seen. When inspecting a converted image, compare every logo and all text with its source. Logos and text must remain unchanged in content, lettering, font, color, shape, size, placement and appearance, including numbers, brand marks, warnings and HUD labels. Return ok=false and describe the discrepancy if any are restyled, made realistic, embossed, distorted, replaced, added or removed, even when realistic material coverage is complete. Judge against the source image: preserve any blur, occlusion or overlap already present there; do not report those source-existing features as generated defects. Write findings in English ASCII without reproducing non-English source lettering. |
| 图片转换 | Convert Image 1 to realistic materials, matching the references' style, color, brightness, shadows and texture. Preserve every logo and all text from Image 1 unchanged, including numbers, brand marks, warnings and HUD labels. Keep their exact content, lettering, font, color, shape, size, placement and original appearance. Exclude them from material conversion, realistic restyling, relighting and embossing. Preserve source-existing blur, occlusion and overlap; do not reveal obscured text or reposition elements to improve readability. |
| 视频生成 | Edit the video: replace its materials to match the reference images. |
| 整段审核 | Compare the result with the source, judging the overall impression at normal playback speed. Allow a small number of subtle defects, brief minor clipping, small shape or texture inconsistencies and other imperfections that ordinary viewers are unlikely to notice in motion. Do not use paused frames, magnification or close frame-by-frame inspection as the acceptance standard. Report missed realistic materials, content changes, motion changes or artifacts only when readily noticeable during normal playback or when they materially change the main content or action. Ignore issues already present in the source. Tolerated minor defects must not be reported as unresolved issues, fail the segment or trigger a quality retry. Return pass with an empty issues list when only tolerated imperfections remain. Apply a relaxed standard to UI, HUD and text: accept mild blur, ghosting or jelly-like deformation while they change or move, provided they look normal when static and their overall placement remains reasonably stable. Do not require pixel-perfect shapes or frame-by-frame text and number matching during changes. Report UI, HUD or text only for obvious drifting, severe distortion, persistent abnormal appearance when static, or major missing or added elements. Do not fail the segment or request a retry for the tolerated UI, HUD or text imperfections. |

连续镜头的视频提示仅额外加：`Start from Image 1.`

## 离线验证与分发

用技能托管 Python 运行 `dev/run_tests.py` 和 `dev/check_skill.py`。
测试包含模拟审核、重试、依赖恢复及真实 FFmpeg 合成媒体拼接；不消耗模型额度，不代表实际生成画质已验收。
打包 `skill/video-reskin/`，排除 runtime-cache/ 即可分发。账号和运行时实现、锁定依赖不变。
