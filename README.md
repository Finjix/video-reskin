# video-reskin — remake

将游戏画面转换为写实材质的 Windows Agent Skill。新版先看完整视频，再逐段补齐完整帧参考图、生成、审片；输出 1080p / 24 fps，最终放回非游戏画面、尾板和原声。

## 工作流程

1. 本地生成完整 24 fps 工作副本，Gemini 以每秒 6 帧分析全片。
2. 标记非游戏区间、尾板及内容清单，按不超过 30 秒、少切原则选择运动开始或镜头变化附近的切点。
3. Gemini 逐段选择能覆盖场景、人物、物品、地面的完整帧，重点补齐新道具。第一张用技能风格图；以后仅使用生成图作为参考，2K/medium。
4. 连续镜头由 Gemini 对比上段生成尾帧与下一段源首帧，只判断是否为同一镜头的连续画面；是则使用尾帧作参考，否则转换下一段首帧。此处不判断材质或生成质量，不打回上段、不消耗重试次数。场景硬切或非游戏间隔后始终转换首帧。
5. Seedance 输入本地 24 fps 工作副本切出的片段，生成首段后由 Gemini 完整审核，主 agent 校对后批准。
6. 后续段先拼接预览，Gemini 检查接缝前后各 1 秒、报告问题并提供对照图，然后停下等待主 agent 判断：差异大则重做后一段，差异小则选择直切或细微过渡，再完整审段。接缝、片段和参考图审核中，主 agent 必须查看 Gemini 报告问题的对应对照图，核对该问题，并通看整张图是否还有其他问题。尾帧审核仅由 Gemini 判断，主 agent 不参与。确认有问题再修正，误报则说明原因并请求 Gemini 重审。
7. 每段首次生成后共用最多 5 次质量重试，耗尽停住该视频。通过后才开始下一段。
8. 默认直切，由主 agent 看问题图后决定是否添加最多 6 帧的细微过渡，应用后不再重审接缝。最后按源时间线放回非游戏画面、尾板和原声。

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
| 接缝审核 | Compare the join with the source, one second before and after. Report visible jumps or drifts in style, scene, characters, objects or motion that the source does not have. |
| 整段审核 | Compare the result with the source. Report missed realistic materials, content changes, motion changes or artifacts. Ignore issues already present in the source. |

连续镜头的视频提示仅额外加：`Start from Image 1.`

## 离线验证与分发

用技能托管 Python 运行 `dev/run_tests.py` 和 `dev/check_skill.py`。
测试包含模拟审核、重试、依赖恢复及真实 FFmpeg 合成媒体拼接；不消耗模型额度，不代表实际生成画质已验收。
打包 `skill/video-reskin/`，排除 runtime-cache/ 即可分发。账号和运行时实现、锁定依赖不变。

后续段接缝审核后，主 agent 使用 `seam --segment S01:K02 --transition-frames 0 --reason "Clean join after visual inspection"` 记录决定（0 为直切，1–6 为过渡），再执行 check 完整审段；明显跳变或人物、物品、运动变化使用 redo。Gemini 不再返回过渡建议。
