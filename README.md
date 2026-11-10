# video-reskin

版本 `0.0.1`。批量把视频转换画风的标准 Agent Skill（技能），支持 Windows 10/11 x64 的本地执行环境。默认风格是“写实版材质，风格不变”，成片为 2K（短边 1440），会去掉游戏 logo、底部警示语和片尾尾板，保留原声。

## 目录与分发

技能放在 `skill/video-reskin/`：里面是 `SKILL.md` 与 `references/`、`scripts/`、`assets/`、`requirements/`。直接压缩 `skill/video-reskin/` 文件夹即可打包分发（首次运行后在里面下载的 `runtime-cache/` 不需要带上），解压到任意技能目录（例如 `~/.claude/skills/video-reskin/`）即可使用。仓库根目录只放说明文档和 `dev/`（测试与验证工具）。`dev/` 只放测试和验证工具。

## 工作流

AI 阅读 `SKILL.md` 后通过统一入口操作：

```powershell
& "skill/video-reskin/scripts/video_reskin.cmd" --help
& "skill/video-reskin/scripts/video_reskin.cmd" auth --key <Winky API key>
& "skill/video-reskin/scripts/video_reskin.cmd" login --no-open
```

`init` → `analyze` → `survey` → `plan` → `refs` → `render` → `assemble` → `review`（→ `frames` / `redo` → 重新 `render`、`assemble`、`review`）。

适用对象是 2.5D 俯视角的游戏视频：镜头在同一个场景里移动，角色走到不同地方，中途会冒出新的角色、物品和道具。为了降低成本，所有片段共用同一套参考图，片段之间不再串联，也不再为了接缝反复重抽。

1. **分析**（`analyze`）：识别镜头切换，逐帧估算镜头在画面上的移动速度和方向（相位相关），找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。
2. **看全片**（`survey`）：Gemini 完整看一遍原片，返回两类信息。一是场景：每个地点给出 3–8 个时刻，这些帧拼起来能覆盖镜头走过的整个场景范围。二是主体：中途出现的角色、物品、道具，以及一直在场、会走动的主要角色，最多 8 个，每个给出出场区间、一个清楚的时刻和框。视频里读出的框经常偏一个身位，所以脚本会把那一帧截出来，再让 Gemini 在静帧上重新框一次。截图和裁切汇总在 `catalog/<id>-survey.jpg`，AI 检查后在 edits 里写保留区间、材质、要去除的元素，需要时修正 `scenes` / `subjects`。
3. **分段**（`plan`）：MiniMax H3 每次最多 15 秒，时长必须是整秒（4–15）。保留区间按最长 15 秒切段，优先在硬切处断开。镜头内部的断点放在可选范围最后 7 秒里镜头移动最快、或者画面本身有跳变（闪光、爆点、甩镜）的时刻。镜头内的接缝两侧各多渲染 0.3 秒作为重叠，每段再补齐到整秒。每段记录它用到的场景图和主体三视图。
4. **参考图**（`refs`）：都用 GPT Image 2.6（`gpt-image-2.6-sunburst`，Lumos 图生图）。每个场景生成一张完整场景大图，规格 4K、质量 high：把 survey 选出的几帧和风格参考图 `assets/styles/realistic.jpg` 一起交给模型，拼成整个场景的写实俯视全景，不带人物、UI 和文字，宽高比按镜头走过的范围估算。每个主体生成一张三视图（正、侧、背），规格 1K、质量 medium：输入是裁切图、原帧和场景大图，写实程度与场景图一致。汇总在 `sheets/<id>-refs.png` 给 AI 检查，不合格的用 `refs --redo` 单独重做。
5. **视频编辑**（`render`）：在 Lumos 上逐段调用 MiniMax H3（`MiniMax-H3`），2K 渲染，每次输入一段原片。参考图包括该段涉及的场景大图，以及段内出场主体的三视图，每段最多 2 张，本段新出现的优先，只生成会被某段用到的三视图。三视图带得越多，H3 越容易去“特写”主体：实测带 4 张三视图时，K01 四次里有三次自己推成特写，只带场景图时机位正常。只靠提示词时 H3 几乎不改画风（实测仍是卡通），写实效果主要来自这些参考图。H3 不是逐帧锁定的编辑器：它会保留故事和构图，但节奏由自己决定，偶尔还会自己运镜（实测一次重做在 4 秒后推成了切鱼特写）。所以提示词第一句就要求逐帧重绘、机位和取景不变、不推近、不切镜。结果下载后，脚本用边缘图和粗布局两项指标与原片比对，两项都接近随机配对时判为偏离原片，自动重渲一次（这次只带场景图），不计入回炉次数。同一轮两次都偏离时，保留较接近原片的一次，审片时列为高优先级问题。各段互不依赖，默认 4 段同时渲染，重做时只重渲那一段。
6. **拼接**（`assemble`）：
   - **对时**：H3 的节奏不均匀，实测 6 秒的片段返回 6.58 秒，前 2.5 秒同步，之后慢了半秒，整体拉伸会错位。逐帧对时（边缘图动态时间规整）会在 H3 走快走慢的地方反复冻帧、跳帧，看起来卡顿、忽慢忽快（GOF3D10176 实测 495 个运动帧里冻了 187 个）。现在每段只用一个恒定速度：按画面内容找出该段在接缝处对应原片的时刻，两头钉住，中间按 H3 自己的节奏逐帧播放；需要补的少量重复帧或跳帧放在画面静止的帧上，相隔至少 4 帧，不用插帧（ffmpeg 补帧在这些片段上会连续输出同一帧）。
   - **调色**：镜头内的接缝先用最小二乘做颜色匹配。
   - **接缝**：以不加效果为主（用户要求）。在两段的重叠里找跳变最小的一帧切换：如果该帧的跳变不超过正常帧间变化的 1.15 倍、最差区域不超过 1.2 倍、两版画面相差不超过 30，就直接切；否则只做 6 帧（0.25 秒）的非常轻微的叠化，不加模糊等其他效果。硬切处直接拼接。实测射冰取鱼的两处接缝：22.17 秒直接切，11.58 秒两版画面差异大（明暗、人群画法不同，长凳上的鱼排消失），用了轻微叠化。
   - **测量与成片**：脚本照样测量两段在切换处的差异，明显不一致时标为可疑接缝，交给 AI 看图确认。最后铺上原片对应区间的音频，输出 2K（短边 1440）H.264。
7. **质检**（`review`）：Gemini 只看一次完整的并排对比，原片和成片按原尺寸并排、近无损编码，检查穿帮、bug、拼接色差、残留的 logo/文字和尾板，不再单独看接缝。主 agent 自己看两组图：整片每 3 秒一张原片/成片对比图，每个接缝一张 5 帧对比图（−1、−0.5、0、+0.5、+1 秒）。问题在静帧上确认属实后，用 `redo` 打回对应片段，每个片段最多回炉两次，之后照常交付并列出遗留问题。纠正句只写观感。写“某物在哪里、何时出现”的句子可能让 H3 自己改运镜，可以用 `--replace` 去掉之前加过的纠正句。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

## 账号

- **Lumos 登录**：`login` 会用隐藏的 Edge（或 Chrome）打开 Lumos 登录页，把飞书二维码保存成图片。每出一张新码就打印一行 `qr: <路径>`，AI 把图片发给用户扫码。飞书一两分钟换一次码，换了就发新的。页面刚打开时的第一张码会马上被替换，所以只有连续两秒不变的码才会打印。用户扫码后，技能只保存 refresh token，用它换取访问令牌。
- **Winky API key**：通过 `auth --key` 保存，只用于 Gemini（看全片、审片）。
- **存储方式**：两者都用 Windows DPAPI 加密，存在 `%APPDATA%\video-reskin\credentials.json`，只有当前 Windows 用户能解密。也可以用环境变量 `VIDEO_RESKIN_WINKY_KEY` 临时指定 key。

## 接口来源

| 用途 | 接口 |
| --- | --- |
| 登录 | `POST /marauders-map/lumos-auth/refresh`（携带 Cookie `lumos_refresh_token`）→ `lumos_access_token`，请求头为 `Authorization: LumosBearer …` |
| 上传 | `POST /legil/server/input-media-url` → PUT 到 OSS 签名地址 → `POST /legil/server/upload-callback`（`file_id`）→ `POST /legil/server/output-media-url` |
| 视频编辑 | `POST /legil/server/generations/video-to-video`，参数 `model: MiniMax-H3`、`reference_videos`（1 段，2–15 秒，≤50 MB）、`reference_images`（≤9 张，≤30 MB）、`prompt`（≤7000 字符）、`resolution: 2K`（或 `768P`）、`duration`（4–15 整秒）、`aspect_ratio: adaptive`、`sample_count: 1`；轮询 `GET /legil/server/generations/{id}`。返回 24fps、带音轨、时长略长于输入 |
| 图生图（参考图） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.6-sunburst`、`resolution`（`1K`/`2K`/`4K`，4K 需指定宽高比）、`quality`（`low`/`medium`/`high`/`xhigh`/`max`/`auto`），最多 16 张参考图 |
| 看全片、审片 | `POST https://lumos.diandian.info/winky/gemini/v1/models/gemini-3.8-flash:generateContent`（`inlineData` 视频或图片 + `videoMetadata.fps` + `responseSchema`） |

Lumos 的接口是从网页的实际请求中整理出来的，网页改版后可能需要跟着调整。

## 按需依赖与缓存

| 使用功能 | 自动准备 |
| --- | --- |
| 基础命令 | 私有 Python 3.13.15（uv 0.13.4） |
| 分析、看全片、参考图、渲染、拼接、审片 | NumPy 2.5.3、Pillow 12.3.0、FFmpeg/FFprobe 9.0.2 |
| 扫码登录 | Playwright 1.64.1（使用系统自带的 Edge，不下载浏览器） |

依赖版本和哈希都已锁定。缓存默认放在技能目录内的 `runtime-cache/`，可以用 `VIDEO_RESKIN_CACHE_DIR` 改到其他位置；设置 `VIDEO_RESKIN_OFFLINE=1` 可禁止联网。退出码：`0` 成功；`2` 输入、凭据或规则问题；`3` 运行时或服务故障。

## 开发与验证

- `python dev/run_tests.py`：不需要网络的回归测试。
- `python dev/test_bootstrap.py`：在中文路径下验证冷启动和离线复用。
- `python dev/smoke/e2e.py`：用 `.素材` 里的视频跑一遍真实的端到端流程，需要 Lumos 登录和 key。
- `python dev/check_skill.py`：检查 `skill/video-reskin/` 是否是可直接分发的技能（`SKILL.md` 头部、引用文件、文件大小与类型）。
