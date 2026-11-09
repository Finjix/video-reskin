# video-reskin

版本 `0.0.1`。批量把视频转换画风的标准 Agent Skill（技能），支持 Windows 10/11 x64 的本地执行环境。默认风格是“写实版材质，风格不变”，成片为 1080p，会去掉游戏 logo、底部警示语和片尾尾板，保留原声。

## 目录与分发

仓库根目录就是技能目录：`SKILL.md` 与 `references/`、`scripts/`、`assets/`、`requirements/` 并列。把根目录压缩成 ZIP 即可分发（`.git/`、`dev/` 和首次运行后下载的 `runtime-cache/` 不需要带上），解压到任意技能目录（例如 `~/.claude/skills/video-reskin/`）即可使用。`dev/` 只放测试和验证工具。

## 工作流

AI 阅读 `SKILL.md` 后通过统一入口操作：

```powershell
& "scripts/video_reskin.cmd" --help
& "scripts/video_reskin.cmd" auth --key <Winky API key>
& "scripts/video_reskin.cmd" login
```

`init` → `analyze` → `plan` → `keyframes` → `render` → `assemble` → `review`（→ `frames` / `redo` → 重新 `render`、`assemble`、`review`）。

1. **分析**（`analyze`）：识别镜头切换，找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。AI 看图后写 edits 文件，内容包括保留区间、画面里的真实材质、要去除的元素和锚点帧。
2. **分段**（`plan`）：把保留区间切成最长 30 秒的片段（Seedance 上限），优先在硬切处断开。镜头内部按能放下的最少段数平均切分（接缝越多跳变越多，不要为了细节缩短分段），每个断点选在平均位置附近画面最静止的时刻，后一段多渲染 0.5 秒作重叠。
3. **关键帧**（`keyframes`）：参考飞书案例，先用 `gpt-image-2.6-sunburst` 把锚点帧转成写实材质，风格不变，纹理突出。转锚点帧时附上风格参考图 `assets/styles/realistic.jpg`，只取它的观感（材质、光线、明暗和色调），提示词不再单独强调暗沉。之后以锚点为参考，把镜头开头和硬切后每段的首帧转换成同一风格；镜头内部的后续段也生成关键帧，但只当“材质细节”参考（见下）。后段里第一次出现、前段都没有的物体（带时间的 `materials` 条目），会按它出现的时刻各生成一张写实化的“揭示帧”。所有提示词都是英文（`assets/prompts.json`）。图片统一走 Lumos 图生图接口（用 Lumos 登录态，不用 Winky key）。
4. **视频编辑**（`render`）：在 Lumos 上逐段调用 Seedance 2.5（`doubao-seedance-2-5-260628`）。模式是 `video_edit`，分辨率 1080p，同一视频的各段依次渲染，后段的参考图都取自前段已生成的画面：图 1 是前一段在本段起点那一帧，原样使用、不经 GPT 重画，也不再附本段单独转换的关键帧（两者的明暗和雪地都与前段画的不同，接缝会跳，GOF3D10232 实测），保证接缝连贯；硬切后的段用本段关键帧；接着是本段的揭示帧；再按原片画面匹配，从前段成片里挑最多 3 帧本段会再次出现的场景（如回到河边时的树和码头），总共最多 6 张。提示词说明每张图的用途，并要求只取观感、不加画面里没有的东西，同时要求去掉 logo 和警示语。不同视频同时进行 4 条，可以断点续跑；前段重做时，后面的段也会跟着重做。
5. **拼接**（`assemble`）：Seedance 返回的是 24fps 视频（成片也是 24fps），时长比原片段略短，所以每段都会按原时长精确拉伸。镜头内部的接缝先做颜色匹配，先在重叠里检查后一段是否比前一段慢 1–2 帧（拉伸后会出现，镜头平移时会让同一物体错位），慢了就去掉开头几帧、补齐末帧；再在 0.5 秒重叠里找两段最接近的那一帧，用 3 帧短过渡切换（不再用整段交叉淡化，避免两次生成叠在一起出现重影），画面运动比两段差异还大时（如平移）直接在该帧切换、不做过渡，并测量切换处的差异：明显超过正常帧间变化或某个区域差异很大时，判为可见接缝；硬切处直接拼接。最后铺上原片对应区间的音频，输出 1080p H.264。
6. **质检**（`review`）：Gemini 看整条视频——原片和成片按成片原尺寸并排、24fps、近无损编码（crf 4），码率上限按 Gemini 接口的 256,000,000 字节算满，只在放不下时才压低，媒体分辨率 HIGH，检查穿帮、bug、拼接色差、残留的 logo/文字和尾板；每个接缝再单独看前后各 2 秒。主 agent 自己抽帧审：每 2 秒一张原尺寸无损 PNG 对比图，每个接缝从 -2 秒到 +2 秒、切换处逐帧。程序测得的可见接缝直接记为高优先级问题。确认属实的片段用 `redo` 打回重做，审到没有明显瑕疵为止。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

## 账号

- **Lumos 登录**：`login` 会用隐藏的 Edge（或 Chrome）打开 Lumos 登录页，把飞书二维码保存成图片并弹出。用户扫码后，技能只保存 refresh token，用它换取访问令牌。
- **Winky API key**：通过 `auth --key` 保存，只用于 Gemini 审片。
- **存储方式**：两者都用 Windows DPAPI 加密，存在 `%APPDATA%\video-reskin\credentials.json`，只有当前 Windows 用户能解密。也可以用环境变量 `VIDEO_RESKIN_WINKY_KEY` 临时指定 key。

## 接口来源

| 用途 | 接口 |
| --- | --- |
| 登录 | `POST /marauders-map/lumos-auth/refresh`（携带 Cookie `lumos_refresh_token`）→ `lumos_access_token`，请求头为 `Authorization: LumosBearer …` |
| 上传 | `POST /legil/server/input-media-url` → PUT 到 OSS 签名地址 → `POST /legil/server/upload-callback`（`file_id`）→ `POST /legil/server/output-media-url` |
| 视频编辑 | `POST /legil/server/generations/video-to-video`，参数 `generation_mode: video_edit`、`resolution: 1080p`、`model: doubao-seedance-2-5-260628`、`reference_videos`、`reference_images`；轮询 `GET /legil/server/generations/{id}` |
| 图生图（关键帧） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.6-sunburst` |
| 审片 | `POST https://lumos.diandian.info/winky/gemini/v1/models/gemini-3.8-flash:generateContent`（`inlineData` 视频 + `videoMetadata.fps` + `responseSchema`） |

Lumos 的接口是从网页的实际请求中整理出来的，网页改版后可能需要跟着调整。

## 按需依赖与缓存

| 使用功能 | 自动准备 |
| --- | --- |
| 基础命令 | 私有 Python 3.13.15（uv 0.13.4） |
| 分析、关键帧、渲染、拼接、审片 | NumPy 2.5.3、Pillow 12.3.0、FFmpeg/FFprobe 9.0.2 |
| 扫码登录 | Playwright 1.64.1（使用系统自带的 Edge，不下载浏览器） |

依赖版本和哈希都已锁定。缓存默认放在技能目录内的 `runtime-cache/`，可以用 `VIDEO_RESKIN_CACHE_DIR` 改到其他位置；设置 `VIDEO_RESKIN_OFFLINE=1` 可禁止联网。退出码：`0` 成功；`2` 输入、凭据或规则问题；`3` 运行时或服务故障。

## 开发与验证

- `python dev/run_tests.py`：不需要网络的回归测试。
- `python dev/test_bootstrap.py`：在中文路径下验证冷启动和离线复用。
- `python dev/smoke/e2e.py`：用 `.素材` 里的视频跑一遍真实的端到端流程，需要 Lumos 登录和 key。
- `python dev/check_skill.py`：检查根目录是否是可直接分发的技能（`SKILL.md` 头部、引用文件、文件大小与类型）。
