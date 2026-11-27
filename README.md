# video-reskin

Windows 游戏视频换皮技能，支持换写实画风（`realistic`）、换暗黑画风（`dark`）和换元素（`elements`）。三个模式都由 Seedance 编辑原片，共用同一个切片规划器，保留原声和尾板。

## 换画风

1. 生成 24 fps 工作副本，用 `survey` 查看原片采样，标记非游戏画面、尾板和场景、人物、道具等内容。用 `plan` 规划片段，每段最长 30 秒。
2. 用 `refs` 选帧并检查转换后的参考图。同一镜头的后续段可用上段生成尾帧起手。
3. Seedance 生成后，用 `check` 生成原片与生成片并排的对照页，按正常播放观感审核，再批准或重做。每段最多重做两次，三版都不合格时选出最好的一版并记录遗留问题。
4. 各段放行后用 `assemble` 合成。游戏片段之间做 4 帧过渡，非游戏画面和尾板按原时间轴拼接。

## 换元素

每条成片替换一个或多个醒目元素，各版本使用互不重复的替代造型，钞票保持原样。`plan --input` 登记各版的目标、可见区间、分段提示和参考图；`refs` 用 Lumos 图生图生成替代物参考图；`render` 把原视频片段交给 Seedance 编辑；`check` 生成原帧率对照视频；`approve` 后 `assemble` 拼回原时间轴与原声。多个版本的生成任务会同时提交。详见[换元素工单与命令](skill/video-reskin/references/elements.md)。

## 使用

通过 `skill/video-reskin/scripts/video_reskin.cmd` 运行命令。用 `auth` 查看 Lumos 登录状态，必要时用 `login` 扫码。

```json
{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery","style":"dark"}
```

`init --work-order order.json` 读取上面这样的工单，后续命令使用打印出的 `--job-dir`。`status` 可查看进度。

[命令和数据格式](skill/video-reskin/references/workflow.md) · [审核标准](skill/video-reskin/references/review.md) · [风格与生成提示词](skill/video-reskin/assets/prompts.json)

## 离线验证与分发

用技能托管的 Python 运行 `dev/run_tests.py` 和 `dev/check_skill.py`。测试模拟 Lumos 调用，用本地 FFmpeg 验证采样、重试、恢复及拼接流程。分发时打包 `skill/video-reskin/`，可排除 `runtime-cache/`。
