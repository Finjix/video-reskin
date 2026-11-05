# video-reskin

版本 `0.0.1`（见 `plugin/plugin.json`）。批量把视频转换画风的 OpenAI 插件，主要面向 ChatGPT / Codex 的本地执行环境，支持 Windows 10/11 x64。默认风格是“写实版材质，风格不变”，成片固定为 720p、24fps，会去掉游戏 logo、底部警示语和片尾尾板，保留原声。工作流以“画面稳定、接缝无跳变、成本低”为先，写实程度适度即可。

## 工作流

AI 阅读 `plugin/skills/video-reskin/SKILL.md` 后通过统一入口操作：

```powershell
& "plugin/scripts/video_reskin.cmd" --help
& "plugin/scripts/video_reskin.cmd" auth --key <Winky API key>
& "plugin/scripts/video_reskin.cmd" login
```

`init` → `analyze` → `lint` → `plan` → `run`（关键帧，停下来看图）→ `run`（逐段检查的渲染；每个源渲完马上拼接、审片）→ 确认问题后 `redo` / `accept` → 再 `run`。各步骤也可以单独执行（`keyframes`、`render`、`assemble`、`review`；`preview` 只在需要时预览个别时刻），`cost` 查看花费；用户要原片对比时，`compare` 生成左原片、右成片的 `<名称>_写实_对比.mp4`；重做一版后可以用 `compare --with <旧成片> --labels 原版 旧写实版 新写实版` 生成三栏对比。

1. **分析**（`analyze`）：识别镜头切换，找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。AI 看图后写 edits 文件，内容包括保留区间、画面里的真实材质、要去除的元素、需要保留原样的 HUD 框、由程序烧录的文字/UI（`overlays`）和锚点帧。
2. **分段**（`plan`）：Seedance 2.5 能一次编辑 4–30 秒，30 秒内画质保持稳定，所以分段尽量少、尽量长，优先在硬切处断开。超长镜头会平均切分，断点选在画面最静止的位置，后一段多渲染 1 秒作为重叠区。
3. **关键帧**（`keyframes`）：写实预设只把材质、纹理和光影换成真实世界的样子，物体和角色的造型、比例、布局、位置、数量、动作和固有颜色都不变（Q版人物仍是Q版造型，只是衣服、皮肤和头发变成真实材质）。用户给了参考图或参考视频时，edits 里的 `style_ref`（视频某一帧，可裁掉 logo 和警示语）作为锚点的第二张图，只学习它的质感，不复制内容。参考飞书案例，先用 `gpt-image-2.5-sunburst` 把锚点帧“转化为写实版材质，风格不变”，再以锚点为参考，生成每个镜头首段的首帧。图片统一走 Lumos 图生图接口（用 Lumos 登录态，不用 Winky key），质量 `high`、分辨率 1K（720p 成片用不到更高）。
4. **视频编辑**（`render`）：在 Lumos 上逐段调用 Seedance 2.5（`doubao-seedance-2-5-260628`），模式是 `video_edit`，分辨率固定 720p，送去的片段也是 720p。同一镜头内的各段串成一条链：首段以关键帧作参考；后面每段的输入片段开头 1 秒直接是上一段 AI 结果的最后 1 秒（按原片时钟拉伸回去），参考图是其中第一帧，所以 Seedance 是接着上一段已经画好的画面往下画，而不是只看一张图重新“想象”一遍。1080p 那批只给参考图，13 个镜头内接缝里有 8 个按现在的标准测出明显跳变（GOF3D10374 在 33.6 秒处原木、水面和纹理都变了）。带 `box` 的 logo 和警示语在送去的片段里由程序按字母抹掉，视频提示词不点名 logo，只说“画面右上角有被抹平的模糊色块”，让 Seedance 在那里画出连贯的场景（1080p 那批没有这句话，S01、S09 的每个角落都留下了模糊块）；角落仍然糊时改用 `"wipe": "none"`：logo 留在片段里，由提示词让 Seedance 自己删。不同镜头、不同视频之间并行，可以断点续跑。
5. **拼接**（`assemble`）：
   - Seedance 返回 24fps 视频，时长比原片段略短，每段按原时长精确拉伸。
   - 同一镜头内的接缝先做最小二乘调色，再在共享的那 1 秒里找两版最相近的一帧，用 2 帧切换，不做淡入淡出（两版不同时，长一点的过渡看起来就是渐变）。每个接缝都测两处：切换点两版的差异，以及后一段自己的画面开始处的逐帧变化；结果分为 clean、slight、visible 三档，标准比以前严（差异超过画面自身运动 2.2 倍也算明显），并生成接缝对照图。
   - 每个去 logo 的角落都会测一次：被抹掉的字母区域里剩下的细节和周围场景相比的比例，低于 0.4 判为 `smeared`（模糊涂抹块），低于 0.6 判为 `soft`。1080p 那批糊掉的角落测得 0.1–0.38，干净的约 1.05。
   - HUD 框内“镜头移动时始终静止”的像素（图标、底框和其中的数字）逐帧从原片贴回，数字始终正确。
   - 屏幕固定的平面 2D 文字和 UI（字幕、标签、横幅、按钮）用 `overlays` 由程序烧录：模型在关键帧和视频里不画这些元素，拼接时再逐帧从原片贴回（可按矩形、椭圆、多边形或颜色抠出），或者由程序直接绘制文字，保证内容准确。场景里的 UI（地面价格牌、角色头顶气泡等透视元素）和复杂动效文字仍交给模型：贴图跟不上镜头运动。
   - 最后铺上原片对应区间的音频，输出 720p、24fps 的 H.264。
6. **质检**（`review`）：
   - 把原片和成片并排同步交给 `gemini-3.8-flash` 全片审查：穿帮、bug、新增文字、拼接色差、残留 logo/文字、尾板。
   - 再对每个接缝前后 3 秒以 10fps 单独近看；程序测得的接缝跳变和模糊的去 logo 角落也作为问题加进报告。
   - 写实度只作为时间线报告（5 分像真实相机实拍），2 分及以下才算 medium 问题；不再为写实度返工。
   - 一致性检查（`identity`）只在审片时做一次：反复出现的主体（主角、顾客、车辆、机器）和风格关键帧逐对比较。
   - 去 logo 区域检查（`gemini-wipe`）：每个带 `box` 的去除项按时间抽 8 个时刻，原片和成片的该角落上下对照，Gemini 找残留字母、复制进来的物体和糊斑。
   - 只有观众一眼能看到的问题才返工：明显接缝、模糊或残留的 logo、乱码或新增文字、变形/消失/多出来的物体、主要物体颜色错。medium 和 low 问题写进总结。最多 2 轮。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

## 省钱和提速

1080p 那批 9 条视频（约 500 秒）生了 427 张图（393 张是预览图），提交了 87 次 Seedance、共 1952 秒（首次 547 秒，其余是返工、改计划、链式连带、改 logo 框和补丁）。现在的做法：

- **固定 720p、24fps**：Seedance 按 720p 渲染，成片 720p、24fps（就是 Seedance 的帧率）；GPT Image 质量从 `max` 改为 `high`、分辨率 1K。
- **不再自动出预览图**：`run` 只做关键帧（每个源一个锚点加每个镜头的首帧）。`preview --only S01 --at T` 只在关键帧看不到的时刻有疑问时用，每次最多 4 张。
- **去掉 midframes 和局部补丁**：预览图不再作为参考图交给 Seedance（它把预览图的颜色错误带进视频）；`patch` 已删除（每个补丁多两个接缝）。
- **写实度适度**：只换材质、不改造型，不追求“更真实”，不为写实度返工；身份一致性检查只在审片时做一次。
- **逐段检查只拦明显问题**：同一镜头里后一段从前一段的结果接着画，后面的问题打回会连带重渲整条链。所以每段渲完、下一段开始前，先测接缝和去 logo 角落、再让 Gemini 对照原片看这一段；只有明显接缝、模糊角落或 high 问题才停住这条链。
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
| 视频编辑 | `POST /legil/server/generations/video-to-video`，参数 `generation_mode: video_edit`、`resolution: 720p`、`model: doubao-seedance-2-5-260628`、`reference_videos`、`reference_images`；轮询 `GET /legil/server/generations/{id}` |
| 图生图（关键帧） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.5-sunburst`、`quality: high`、`resolution: 1K` |
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
