# video-reskin

将游戏画面转换为写实材质的 Windows Agent Skill。先看完整视频，再逐段补齐完整帧参考图、生成、审片；输出 1080p / 24 fps，最终放回非游戏画面、尾板和原声。

## 工作流程

1. 本地生成 24 fps 工作副本，Gemini 以每秒 6 帧分析全片。
2. 标记非游戏区间、尾板及内容清单，按不超过 30 秒、少切原则选切点。
3. Gemini 逐段选出覆盖场景、人物、物品、地面的完整帧，重点补齐新道具。首张只用源帧转换，之后参考已有生成图；参考图 2K/medium。
4. 同一镜头的后续段以上段生成尾帧为起点，前提是 Gemini 判断尾帧与下段源首帧为连续画面；否则以及硬切后转换下段首帧。这一判断只用于选参考，不影响上段。
5. Seedance 生成后，Gemini 对照原片整段审核，主 agent 核实报告后批准。
6. 每段最多 2 次质量重试；三版都不合格时用 select-best 选最好的一版并记录已知问题。
7. 游戏片段之间做 4 帧过渡，与非游戏画面、尾板之间硬接，原声对齐。

审核规则见 [review.md](skill/video-reskin/references/review.md)：整段按正常播放观感验收，UI/HUD/文字宽松；参考图严格要求 Logo 和文字保持原样。

## 使用

技能入口：`skill/video-reskin/scripts/video_reskin.cmd`。先 auth / login，再 init --work-order order.json。
工单示例：`{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery"}`。

其余命令加 `--job-dir <任务 temp 目录>`：
`analyze → survey → plan → refs → render → check → approve --segment S01:K01`，重复逐段循环后 assemble。
`status` 查进度；`redo --segment S01:K02 --reason "Visible material jump"` 回炉视频；
`refs --redo --segment S01:K02` 重做本段图片。
服务中断后复用已记录的任务 ID。新参考图不自动重生成已批准片段；前段重做后，后段保留结果并重查。

命令、任务目录、过渡权重和媒体参数见 [workflow.md](skill/video-reskin/references/workflow.md)。全部提示词在 [prompts.json](skill/video-reskin/assets/prompts.json)。

凭据由 Windows DPAPI 加密保存。已有工作副本和分析、参考图、审核缓存不会自动重建；完整采用新配置请新建任务。

## 离线验证与分发

用技能托管 Python 运行 `dev/run_tests.py` 和 `dev/check_skill.py`。
测试包含模拟审核、重试、依赖恢复及真实 FFmpeg 合成媒体拼接；不消耗模型额度，不代表实际生成画质已验收。
打包 `skill/video-reskin/`，排除 runtime-cache/ 即可分发。
