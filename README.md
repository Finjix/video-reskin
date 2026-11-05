# video-reskin

版本 `0.0.1`（见 `plugin/plugin.json`）。批量把视频转换画风的 OpenAI 插件，主要面向 ChatGPT / Codex 的本地执行环境，支持 Windows 10/11 x64。默认风格是“写实版材质，风格不变”，成片固定为 1080p、24fps，会去掉片尾尾板，游戏 logo 和底部警示语按原片原样保留（拼接时从原片逐帧贴回），保留原声。工作流以“画面稳定、接缝无跳变、成本低”为先，写实程度适度即可。

## 工作流

AI 阅读 `plugin/skills/video-reskin/SKILL.md` 后通过统一入口操作。所有发给模型的提示词（GPT Image、Seedance、Gemini）统一用英文：模板、程序拼出的句子，以及 edits 里的材质、去除项、`extra` 和 `redo --prompt` 的纠正语句；报告和给用户的说明仍用中文。

```powershell
& "plugin/scripts/video_reskin.cmd" --help
& "plugin/scripts/video_reskin.cmd" auth --key <Winky API key>
& "plugin/scripts/video_reskin.cmd" login
```

`init` → `analyze` → `lint` → `plan` → `run`（关键帧，停下来看图）→ `run`（逐段检查的渲染；每个源渲完马上拼接、审片）→ 确认问题后 `redo` / `accept` → 再 `run`。各步骤也可以单独执行（`keyframes`、`render`、`assemble`、`review`；`preview` 只在需要时预览个别时刻），`cost` 查看花费；用户要原片对比时，`compare` 生成左原片、右成片的 `<名称>_写实_对比.mp4`；重做一版后可以用 `compare --with <旧成片> --labels 原版 旧写实版 新写实版` 生成三栏对比。

1. **分析**（`analyze`）：识别镜头切换，找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。AI 看图后写 edits 文件，内容包括保留区间、画面里的真实材质、要去除的元素（只在用户要求时才写）、需要保留原样的屏幕 UI（`ui`：计数器、按钮、字幕、横幅、游戏 logo、警示语，逐条写明位置、引号里的原文、颜色和数字变化，由模型照着画）和锚点帧。插件只转化游戏玩法画面：素材里混进来的真人/实拍片段（GOFCN67484 开头车里的真人和熔岩、GOFCN5456 开头的雪地公路、GOFCN7427 开头的雪林，GOFCN60010/63256 开头贴在木头上的真人脸）写进 `live`，由 `lint` 帮忙找出；这些区间单独成段，成片里直接用原片画面，不做关键帧、不送 Seedance、审片也不算问题。
2. **分段**（`plan`）：Seedance 2.5 能一次编辑 4–30 秒，30 秒内画质保持稳定，所以分段尽量少、尽量长，优先在硬切处断开。超长镜头会平均切分，断点选在画面最静止的位置，后一段多渲染 1 秒作为重叠区。
3. **关键帧**（`keyframes`）：写实预设只把材质、纹理和光影换成真实世界的样子，物体和角色的造型、比例、布局、位置、数量、动作和固有颜色都不变（Q版人物仍是Q版造型，只是衣服、皮肤和头发变成真实材质）。用户给了参考图或参考视频时，edits 里的 `style_ref`（视频某一帧，可裁掉 logo 和警示语）作为锚点的第二张图，只学习它的质感，不复制内容。参考飞书案例，先用 `gpt-image-2.5-sunburst` 把锚点帧“转化为写实版材质，风格不变”，再以锚点为参考，生成每个镜头首段的首帧。图片统一走 Lumos 图生图接口（用 Lumos 登录态，不用 Winky key），质量 `high`、分辨率 2K。
4. **视频编辑**（`render`）：在 Lumos 上逐段调用 Seedance 2.5（`doubao-seedance-2-5-260628`），模式是 `video_edit`，分辨率固定 1080p，送去的片段也是 1080p。同一镜头内的各段串成一条链：首段以关键帧作参考；后面每段的输入片段依次是：这个镜头开头 0.5 秒的原片（参考图是这个镜头的关键帧，Seedance 从“第一帧 → 参考图”这一对里读出要做的改动）、上一段 AI 结果的最后 1 秒（按原片时钟拉伸回去），再是本段自己的原片；拼接时去掉开头那 0.5 秒，前一段一直播到这 1 秒结束，再直接切到本段自己的画面，颜色按切点两侧的画面匹配（Seedance 会给复制的那 1 秒重新调色，在里面切换会多出一段颜色不同的画面）。只把上一段结果放在最前面、参考图也用它时，Seedance 读出的改动是“什么都不改”，GOF3D10012 在 26.4 秒处树又变回卡通、河水变浅，logo 也没删。1080p 那批只给参考图，13 个镜头内接缝里有 8 个按现在的标准测出明显跳变（GOF3D10374 在 33.6 秒处原木、水面和纹理都变了）。**现在默认不去 logo 和警示语**：lint 把它们起草成 `ui` 条目，提示词让模型按描述原样画出，审片逐字核对。以下是用户要求去 logo 时的经验：logo 留在送去的片段里，由提示词让 Seedance 删除：GOF3D10012 在 720p 下所有角落都删干净了，没有模糊块。链式的后一段开头那 1 秒来自上一段结果（已经没有 logo），程序会把原片的 logo 像素贴回这 1 秒，让 logo 从第一帧起就在，否则 Seedance 会把中途“出现”的 logo 当成新内容保留。程序先抹掉字母再送（`"wipe": "before"`）的做法，Seedance 每次都留下模糊块（还有一次自己重新画出了游戏 logo），不再用于 logo。画面最底边的警示语无论点名还是抹掉都会保留，干净的去法只有把原片副本的底边裁掉（约 6% 放大），需要先征得用户同意。不同镜头、不同视频之间并行，可以断点续跑。
5. **拼接**（`assemble`）：
   - Seedance 返回 24fps 视频，时长比原片段略短，每段按原时长精确拉伸。
   - 同一镜头内的接缝先做最小二乘调色，再在共享的那 1 秒里找两版最相近的一帧，用 2 帧切换，不做淡入淡出（两版不同时，长一点的过渡看起来就是渐变）。每个接缝都测两处：切换点两版的差异，以及后一段自己的画面开始处的逐帧变化；结果分为 clean、slight、visible 三档，标准比以前严（差异超过画面自身运动 2.2 倍也算明显），并生成接缝对照图。
   - （用户要求去 logo 时）每个去 logo 的角落都会测一次：被抹掉的字母区域里剩下的细节和周围场景相比的比例，低于 0.4 判为 `smeared`（模糊涂抹块），低于 0.6 判为 `soft`。1080p 那批糊掉的角落测得 0.1–0.38，干净的约 1.05。
   - HUD、屏幕固定的文字和 UI、游戏 logo、警示语都由模型绘制，拼接时不再从原片贴回或由程序烧录（`hud`、`overlays` 字段已取消，写了会报错）。edits 的 `ui` 条目逐条写进关键帧和视频提示词（只在该元素出现的时间内），并说明它是平面 2D 层、不做材质和光影、逐字照抄、只画一份、逐帧稳定、数字只按原片变化。
   - 最后铺上原片对应区间的音频，输出 1080p、24fps 的 H.264。
6. **质检**（`review`）：
   - 把原片和成片并排同步交给 `gemini-3.8-flash` 全片审查：穿帮、bug、新增文字、拼接色差、残留的去除项、新增文字、尾板（logo 和警示语默认保留，不算问题）。
   - 再对每个接缝以 10fps 单独近看（覆盖切换点和后一段自己的画面开始处）。另外两道接缝检查：程序对整条视频逐帧算“观感变化”（扣除原片算出的镜头位移和原片自身的变化后，饱和度和颜色的变化），接缝处的变化和这条视频平时的变化比，明显超出就判为跳变；再把接缝前后相邻两帧按原尺寸并排（下面附原片同两刻）交给 Gemini 逐区域比对，medium 以上都算 high。GOF3D10012 26.4 秒的接缝（河水饱和度、鱼的颜色、顶部边缘的树），灰度测量和视频近看都漏掉了，这两道都抓到了。接缝对照图也改为原尺寸的前后两帧加一张变化热图。程序测得的模糊去 logo 角落也作为问题加进报告。
   - 写实度只作为时间线报告（5 分像真实相机实拍），2 分及以下才算 medium 问题；不再为写实度返工。
   - 一致性检查（`identity`）只在审片时做一次：反复出现的主体（主角、顾客、车辆、机器）和风格关键帧逐对比较。
   - 保留元素检查：带 `box` 的 `ui` 元素（logo、警示语、计数器、字幕、按钮）必须和原片一致。审片为每个元素出一张对照图（`review/<id>-kept-r<n>-<k>.jpg`，8 个时刻，上原片下成片），Gemini 逐字比对，乱码、模糊、重影、移位/变色/缺字一律 high。修复要重渲：关键帧上就错了，改准 `ui` 描述后 `plan`、`redo --keyframe`；只在视频里错，用 `redo --prompt` 点名该元素和原文重做该段。两次仍不对就报为未解决，不再烧录。
   - 去 logo 区域检查（`gemini-wipe`，只在有带 `box` 的去除项时）：每个带 `box` 的去除项按时间抽 8 个时刻，原片和成片的该角落上下对照，Gemini 找残留字母、复制进来的物体和糊斑。
   - 只有观众一眼能看到的问题才返工：明显接缝、模糊或残留的 logo、乱码或新增文字、变形/消失/多出来的物体、主要物体颜色错。medium 和 low 问题写进总结。最多 2 轮。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

## 省钱和提速

1080p 那批 9 条视频（约 500 秒）生了 427 张图（393 张是预览图），提交了 87 次 Seedance、共 1952 秒（首次 547 秒，其余是返工、改计划、链式连带、改 logo 框和补丁）。现在的做法：

- **固定 1080p、24fps**：Seedance 按 1080p 渲染，成片 1080p、24fps（就是 Seedance 的帧率）；GPT Image 质量 `high`（不用 `max`）、分辨率 2K。
- **不再自动出预览图**：`run` 只做关键帧（每个源一个锚点加每个镜头的首帧）。`preview --only S01 --at T` 只在关键帧看不到的时刻有疑问时用，每次最多 4 张。
- **去掉 midframes 和局部补丁**：预览图不再作为参考图交给 Seedance（它把预览图的颜色错误带进视频）；`patch` 已删除（每个补丁多两个接缝）。
- **写实度适度**：只换材质、不改造型，不追求“更真实”，不为写实度返工；身份一致性检查只在审片时做一次。
- **逐段检查只拦明显问题**：同一镜头里后一段从前一段的结果接着画，后面的问题打回会连带重渲整条链。所以每段渲完、下一段开始前，先测接缝和去 logo 角落、再让 Gemini 对照原片看这一段；只有明显接缝、模糊角落或 high 问题才停住这条链。
- **横竖版只写一份 edits**：竖版不是横版的裁切（镜头拉远、重新构图，两版各有对方看不到的部分），Seedance 两版都得渲。竖版写 `"like": "<横版 ID>"`，只填自己的去除框和 `ui` 条目（位置不同），其余（保留区间、材质、`extra`、锚点时间、断点、画风）跟横版走；时间跨度相同的段还会带上横版的 `redo --prompt` 纠正语句。`run` 会等横版成片审完、没有待重做的段再渲竖版，横版上踩过的坑不再在竖版上花一遍钱（`--only` 可以立刻渲）。内容相同（md5 一样）的文件只做一次。
- **先定好 edits 再渲染**：改 logo 框、保留区间或断点会重渲相关段（那批为此花了 436 秒）。
- **审片最多 2 轮**，只返工观众一眼能看到的问题。
- **已接受的问题不再拦**：`accept` 放行的检查结果和原因会告诉后面的逐段检查和审片。
- **不重复付费**：提交只发一次，回复丢失时先按本次上传的 `media_id` 查历史；同一任务不允许两个渲染同时跑。
- **纠正提示词**：`redo --prompt` 的纠正语句会随重做累加；只点名要变的部件和要保持的东西，写错了用 `--replace-prompt` 替换。
- **断电恢复**：JSON 状态先落盘再改名；`work/` 里读不出的缓存会自动重建；已提交的生成在重跑 `run` 时接着取。
- **记账**：每次付费调用写入 `state/ledger.jsonl`（首次、redo、上游连带、自动重试、改计划……），`cost` 汇总。
- **流水线**：最长的链先渲；每个源的段一齐就拼接、审片，不等整批。

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
| 图生图（关键帧） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.5-sunburst`、`quality: high`、`resolution: 2K` |
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
