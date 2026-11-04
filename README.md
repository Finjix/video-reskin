# video-reskin

版本 `0.0.1`（见 `plugin/plugin.json`）。批量把视频转换画风的 OpenAI 插件，主要面向 ChatGPT / Codex 的本地执行环境，支持 Windows 10/11 x64。默认风格是“写实版材质，风格不变”，成片为 1080p，会去掉游戏 logo、底部警示语和片尾尾板，保留原声。

## 工作流

AI 阅读 `plugin/skills/video-reskin/SKILL.md` 后通过统一入口操作：

```powershell
& "plugin/scripts/video_reskin.cmd" --help
& "plugin/scripts/video_reskin.cmd" auth --key <Winky API key>
& "plugin/scripts/video_reskin.cmd" login
```

`init` → `analyze` → `lint` → `plan` → `run`（关键帧和预览图，停下来看图）→ `run`（逐段检查的渲染；每个源渲完马上拼接、审片）→ 确认问题后 `redo` / `patch` / `accept` → 再 `run`。各步骤也可以单独执行（`keyframes`、`preview`、`render`、`assemble`、`review`），`cost` 查看花费；用户要原片对比时，`compare` 生成左原片、右成片的 `<名称>_写实_对比.mp4`；重做一版后可以用 `compare --with <旧成片> --labels 原版 旧写实版 新写实版` 生成三栏对比。

1. **分析**（`analyze`）：识别镜头切换，找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。AI 看图后写 edits 文件，内容包括保留区间、画面里的真实材质、要去除的元素、需要保留原样的 HUD 框、由程序烧录的文字/UI（`overlays`）和锚点帧。
2. **分段**（`plan`）：Seedance 2.5 能一次编辑 4–30 秒，30 秒内画质保持稳定，所以分段尽量少、尽量长，优先在硬切处断开。超长镜头会平均切分，断点选在画面最静止的位置，并多渲染 0.5 秒作为重叠区。
3. **关键帧**（`keyframes`）：写实预设允许大幅重构物件外观：每个物体按真实世界里的对应物重新设计造型细节、比例和表面（卡通小树变成真实云杉，圆滚滚的雪包变成不规则的雪堆和岩石，Q版人物变成真人），画面布局、位置、数量、动作和固有颜色不变。用户给了参考图或参考视频时，edits 里的 `style_ref`（视频某一帧，可裁掉 logo 和警示语）作为锚点的第二张图，只学习它的质感和写实程度，不复制内容。参考飞书案例，先用 `gpt-image-2.5-sunburst` 把锚点帧“转化为写实版材质，风格不变”，再以锚点为参考，生成每个镜头首段的首帧（“基于首帧的风格……禁止强烈的噪点以及破碎感，并且参考首帧的地面”）。图片统一走 Lumos 图生图接口（用 Lumos 登录态，不用 Winky key）。
4. **视频编辑**（`render`）：在 Lumos 上逐段调用 Seedance 2.5（`doubao-seedance-2-5-260628`），模式是 `video_edit`，分辨率 1080p。同一镜头内的各段串成一条链：首段以关键帧作参考，后面每段都以上一段 AI 结果在本段起点那一帧作参考，场景就能衔接上，不会每段重新“想象”一遍。不同镜头、不同视频之间并行，可以断点续跑。
5. **拼接**（`assemble`）：
   - Seedance 返回 24fps 视频，时长比原片段略短，每段按原时长精确拉伸。
   - 同一镜头内的接缝先做最小二乘调色，再在重叠区里找两版最相近的一帧，用 4 帧过渡切换，不做长时间的淡入淡出。每个接缝都会测量跳变程度，结果分为 clean、slight、visible 三档，并生成接缝对照图。
   - HUD 框内“镜头移动时始终静止”的像素（图标、底框和其中的数字）逐帧从原片贴回，数字始终正确。
   - 屏幕固定的平面 2D 文字和 UI（字幕、标签、横幅、按钮）用 `overlays` 由程序烧录：模型在关键帧和视频里不画这些元素，拼接时再逐帧从原片贴回（可按矩形、椭圆、多边形或颜色抠出），或者由程序直接绘制文字，保证内容准确。场景里的 UI（地面价格牌、角色头顶气泡等透视元素）和复杂动效文字仍交给模型：贴图跟不上镜头运动。
   - 最后铺上原片对应区间的音频，输出 1080p H.264。
6. **质检**（`review`）：
   - 把原片和成片并排同步交给 `gemini-3.8-flash` 全片审查：穿帮、bug、新增文字、拼接色差、残留 logo/文字、尾板。
   - 再对每个接缝前后 3 秒以 10fps 单独近看。
   - 程序测得的接缝跳变也会作为问题加进报告。
   - 逐段检查和审片都判断画风是否足够写实：Gemini 给出随时间的写实度（5 分像真实相机实拍，3 分是主要物体仍有游戏感），3 分及以下的区间作为 `not_realistic` 问题加进报告，逐段检查会因此拦住这一段；agent 还要自己在抽帧上判断写实度。
   - 一致性检查（`identity`）：Gemini 先在原片帧上找出反复出现的主体（主角必选，另外最多 3 个：顾客、车辆、机器），程序从风格关键帧和全片各时刻截出它们的近景，再让 Gemini 逐对比较（参考 | 该时刻 | 原片小图），按体型（写实/风格化/Q版）、渲染（实拍/游戏CG/卡通）和颜色打分。主角从写实成年人慢慢退回 Q 版这种渐变，逐时刻对照原片的审片看不出来，这里能抓到。逐段检查对每段、预览对每张预览也做同样的检查。
   - 写实度在一段后部或整条链上逐渐退回卡通（Seedance 只参考首帧，后进入画面的物体和后面的段越来越像原片）时，edits 里的 `midframes` 把该段中间时刻的预览图作为额外参考图交给 Seedance，并注明是本段第几秒。
   - 每个问题由主 agent 用 `frames` 抽帧确认，属实的片段用 `redo` 打回重做；同一镜头里后面的段会跟着重做。最多 3 轮，有明显接缝的视频不算完成。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

## 省钱和提速

- **逐段检查**：同一镜头里后一段从前一段的结果接着画，后面的问题打回会连带重渲整条链。所以每段渲完、下一段开始前，先测接缝、再让 Gemini 对照原片看这一段；有问题就停住这条链（其他链照跑），agent 看图后 `redo` 或 `accept`。
- **返工前先看预览**：预览图带上该段的纠正语句（并告诉模型这是本段第几秒），`redo --prompt` 之后 `run` 先出该段的预览停下来看，纠正语句写错（例如把金色的黄鳍金枪鱼也说成蓝白色）只花几张图，不花一次渲染；预览 6 张并发。
- **已接受的问题不再拦**：`accept` 放行的检查结果和原因会告诉后面的逐段检查和审片，同一个已接受的小问题不会每段都卡一次。
- **渲染前抓提示词错误**：`lint` 让 Gemini 列出画面里的物体（颜色、时间）、logo 和警示语、屏幕文字、HUD，核对 edits 并生成草稿；`preview` 用视频提示词在每段里多出几张 GPT Image 预览图（只看，不当参考）。
- **局部补丁**：一段里一两秒的局部问题，可以只重渲 4–8 秒（从该段自己的画面接上），进出两个接缝都测量；整段重做仍是默认。
- **不重复付费**：提交只发一次，回复丢失时先按本次上传的 `media_id` 查历史；同一任务不允许两个渲染同时跑。
- **纠正提示词**：`redo --prompt` 的纠正语句会随重做累加；只点名要变的部件和要保持的东西（颜色词会串到旁边的物体上），写错了用 `--replace-prompt` 替换。
- **断电恢复**：JSON 状态先落盘再改名；`work/` 里读不出的缓存会自动重建；已提交的生成在重跑 `run` 时接着取，不重复付费。
- **记账**：每次付费调用写入 `state/ledger.jsonl`（首次、redo、上游连带、自动重试、补丁……），`cost` 汇总。
- **流水线**：最长的链先渲；每个源的段一齐就拼接、审片，不等整批；审片自动出每个问题的抽帧图和 Gemini 预判。

## 账号

- **Lumos 登录**：`login` 会用隐藏的 Edge（或 Chrome）打开 Lumos 登录页，把飞书二维码保存成图片并弹出。用户扫码后，插件只保存 refresh token，用它换取访问令牌。
- **Winky API key**：通过 `auth --key` 保存，只用于 Gemini 审片。
- **存储方式**：两者都用 Windows DPAPI 加密，存在 `%APPDATA%\video-reskin\credentials.json`，只有当前 Windows 用户能解密。也可以用环境变量 `VIDEO_RESKIN_WINKY_KEY` 临时指定 key。

## 接口来源

| 用途 | 接口 |
| --- | --- |
| 登录 | `POST /marauders-map/lumos-auth/refresh`（携带 Cookie `lumos_refresh_token`）→ `lumos_access_token`，请求头为 `Authorization: LumosBearer …` |
| 上传 | `POST /legil/server/input-media-url` → PUT 到 OSS 签名地址 → `POST /legil/server/upload-callback`（`file_id`）→ `POST /legil/server/output-media-url` |
| 视频编辑 | `POST /legil/server/generations/video-to-video`，参数 `generation_mode: video_edit`、`resolution: 1080p`、`model: doubao-seedance-2-5-260628`、`reference_videos`、`reference_images`；轮询 `GET /legil/server/generations/{id}` |
| 图生图（关键帧） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.5-sunburst` |
| 审片 | `POST https://lumos.diandian.info/winky/gemini/v1/models/gemini-3.8-flash:generateContent`（`inlineData` 视频 + `videoMetadata.fps` + `responseSchema`） |

Lumos 的接口是从网页的实际请求中整理出来的，网页改版后可能需要跟着调整。

## 按需依赖与缓存

| 使用功能 | 自动准备 |
| --- | --- |
| 基础命令 | 私有 Python 3.13.15（uv 0.13.4） |
| 分析、关键帧、渲染、拼接、审片 | NumPy 2.5.3、Pillow 12.3.0、FFmpeg/FFprobe 9.0.2 |
| 扫码登录 | Playwright 1.64.1（使用系统自带的 Edge，不下载浏览器） |

依赖版本和哈希都已锁定。缓存默认放在插件目录内的 `runtime-cache/`，可以用 `VIDEO_RESKIN_CACHE_DIR` 改到其他位置；设置 `VIDEO_RESKIN_OFFLINE=1` 可禁止联网。退出码：`0` 成功；`2` 输入、凭据或规则问题；`3` 运行时或服务故障。

## 开发与验证

- `python dev/run_tests.py`：不需要网络的回归测试。
- `python dev/test_bootstrap.py`：在中文路径下验证冷启动和离线复用。
- `python dev/smoke/e2e.py`：用 `.素材` 里的视频跑一遍真实的端到端流程，需要 Lumos 登录和 key。
- `python dev/build_plugin.py`：检查包结构并生成 `dist/video-reskin-0.0.1.zip`。
- `python dev/sync_codex_plugin.py`：把 `plugin/` 同步到本机 Codex 已安装的插件副本。
