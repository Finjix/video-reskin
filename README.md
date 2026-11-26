# video-reskin

将游戏画面转换为写实材质（realistic）或暗黑风格（dark，保持原游戏 3D 画风的阴森暗夜：深蓝暗部、暖色灯笼、旧木和骷髅旗）的 Windows Agent Skill。先看完整视频，再逐段补齐完整帧参考图、生成、审片；输出 1080p / 24 fps，最终放回非游戏画面、尾板和原声。

## 换元素

每个成片可以替换一个或多个元素。选在手机上原速播放也能看清的物体，让替换效果在手机上清晰可辨。例如冻木可以沿用原木模型，在表面包一层明显的厚冰。竹束则可以通过凸起的竹节和空心截面改变造型。每种替代造型只分给一条成片，同版可同时替换车斗和运送的货物。钞票属于受保护物件，所有版本保留原样。冻木、冻肉等可作为选项，多版同时保留非冰冻方案。

三个换皮模式都由 Seedance 编辑原片，并共用换风格的切片逻辑。动作和遮挡写进片段内提示，每段首发后最多再试两次。换元素默认只复用已有结果，新增提交需在工单写明整批额度。换元素工单使用 `style: "elements"`，在 `variants` 里登记交付名、各目标的可见区间、分段提示和参考图。`render` 上传原视频片段，保存每段的模型名、生成任务 ID 和原始返回文件校验值。`check` 生成原帧率对照视频，检查锤击、散落、背运、转弯和遮挡全过程，通过后拼入原时间轴与原声。示例见[换元素工单与命令](skill/video-reskin/references/elements.md)。

## 工作流程

以下流程和使用示例适用于写实、暗黑两种换画风任务。

1. 本地生成 24 fps 工作副本，短边超过 1080 时缩到 1080，中间编码采用 CRF 16。Gemini 以每秒 6 帧分析全片。
2. 标记非游戏区间、尾板及内容清单，按不超过 30 秒、少切原则选切点；需要多段时每段不短于 4 秒，非游戏区间之间不足 0.5 秒的游戏画面保留原片。
3. Gemini 逐段选出覆盖场景、人物、物品、地面的完整帧，重点补齐新道具。首张只用源帧转换（风格有示例图时，如 dark 的 `assets/styles/dark.jpg`，一并作为风格参考），之后参考已有生成图；参考图 2K/medium。
4. 同一镜头的后续段以上段生成尾帧为起点，前提是 Gemini 判断尾帧与下段源首帧为连续画面；否则以及硬切后转换下段首帧。这一判断只用于选参考，不影响上段。
5. Seedance 生成后，Gemini 对照原片整段审核，主 agent 核实报告后批准。
6. 每段最多 2 次质量重试；三版都不合格时用 select-best 选最好的一版并记录已知问题。生成服务失败会记录并重新提交，不计重试。
7. 游戏片段之间做 4 帧过渡，与非游戏画面、尾板之间硬接，原声对齐。

成片为启用 faststart 的 MP4，视频采用 H.264 High、8-bit yuv420p、CRF 16、medium 预设，短边 1080、24 fps。源视频有音轨时，原声转码为 AAC-LC、48 kHz、双声道、128 kbps。

审核规则见 [review.md](skill/video-reskin/references/review.md)：整段按正常播放观感验收，UI/HUD/文字宽松；参考图严格要求 Logo 和文字保持原样。

## 使用

技能入口：`skill/video-reskin/scripts/video_reskin.cmd`。先 auth / login，再 init --work-order order.json。
工单示例：`{"sources":["D:/footage/source.mp4"],"output_root":"D:/delivery","style":"dark"}`。style 必填，由 agent 按用户请求判断：“转化为写实”等为 realistic，“转化为暗黑”等为 dark，都没说或都说了就先问用户。成片命名为 `<源文件名>_S01_<style>.mp4`。

其余命令加 `--job-dir <任务 temp 目录>`：
`analyze → survey → plan → refs → render → check → approve --segment S01:K01`，重复逐段循环后 assemble。
`status` 查进度；`redo --segment S01:K02 --reason "Visible material jump"` 回炉视频；
`refs --redo --segment S01:K02` 重做本段图片。
服务中断后复用已记录的任务 ID，生成失败则重新提交。新参考图不自动重生成已批准片段；前段变化后，后段需重新 refs 和 render。

命令、任务目录、过渡权重和媒体参数见 [workflow.md](skill/video-reskin/references/workflow.md)。全部提示词在 [prompts.json](skill/video-reskin/assets/prompts.json)，各风格的转换图、视频提示词和目标描述在其 styles 下；参考图检查和整段审核都按任务风格的目标描述判断。

凭据由 Windows DPAPI 加密保存。已有工作副本和分析、参考图、审核缓存不会自动重建；完整采用新配置请新建任务。

## 离线验证与分发

用技能托管 Python 运行 `dev/run_tests.py` 和 `dev/check_skill.py`。
测试包含模拟审核、重试、依赖恢复及真实 FFmpeg 合成媒体拼接；不消耗模型额度，不代表实际生成画质已验收。
打包 `skill/video-reskin/`，排除 runtime-cache/ 即可分发。
