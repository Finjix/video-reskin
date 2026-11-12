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
8. 所有接缝统一使用约 0.2 秒过渡：24 fps 下取 5 帧（0.2083 秒），包括原片和尾板边界。保持总帧数及原声音轨时间不变，不再进行过渡审核。

没有全景图、三视图、材质长清单、累积纠正提示或最后额外的全片模型审片。

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
| 选帧与选参考 | Choose the fewest clear frames covering this segment's scenes, characters, objects and ground, especially newly appearing items. Select existing generated references for content already seen. |
| 图片转换 | Convert Image 1 to realistic materials, matching the references' style, color, brightness, shadows and texture. |
| 视频生成 | Edit the video: replace its materials to match the reference images. |
| 整段审核 | Compare the result with the source. Report missed realistic materials, content changes, motion changes or artifacts. Ignore issues already present in the source. |

连续镜头的视频提示仅额外加：`Start from Image 1.`

## 离线验证与分发

用技能托管 Python 运行 `dev/run_tests.py` 和 `dev/check_skill.py`。
测试包含模拟审核、重试、依赖恢复及真实 FFmpeg 合成媒体拼接；不消耗模型额度，不代表实际生成画质已验收。
打包 `skill/video-reskin/`，排除 runtime-cache/ 即可分发。账号和运行时实现、锁定依赖不变。
