# video-reskin

版本 `0.0.1`（见 `plugin/plugin.json`）。批量把视频转换画风的 OpenAI 插件，主要面向 ChatGPT / Codex 的本地执行环境，支持 Windows 10/11 x64。默认风格是“写实版材质，风格不变”，成片为 1080p，会去掉游戏 logo、底部警示语和片尾尾板，保留原声。

## 工作流

AI 阅读 `plugin/skills/video-reskin/SKILL.md` 后通过统一入口操作：

```powershell
& "plugin/scripts/video_reskin.cmd" --help
& "plugin/scripts/video_reskin.cmd" auth --key <Winky API key>
& "plugin/scripts/video_reskin.cmd" login
```

`init` → `analyze` → `plan` → `keyframes` → `render` → `assemble` → `review`（→ `frames` / `redo` → 重新 `render`、`assemble`、`review`）。

1. **分析**（`analyze`）：识别镜头切换，找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。AI 看图后写 edits 文件，内容包括保留区间、画面里的真实材质、要去除的元素和锚点帧。
2. **分段**（`plan`）：把保留区间切成 2–12 秒的片段，优先在硬切处断开。镜头内部的断点选在画面最静止的位置，并多渲染 0.5 秒用于交叉淡化。
3. **关键帧**（`keyframes`）：参考飞书案例，先用 `gpt-image-2.6-sunburst` 把锚点帧转成写实材质，风格不变，画面暗沉、色调深、纹理突出。转锚点帧时附上风格参考图 `plugin/assets/styles/realistic.jpg`，只取它的观感。之后以锚点为参考，把每段首帧转换成同一风格。所有提示词都是英文（`plugin/assets/prompts.json`）。图片统一走 Lumos 图生图接口（用 Lumos 登录态，不用 Winky key）。
4. **视频编辑**（`render`）：在 Lumos 上逐段调用 Seedance 2.5（`doubao-seedance-2-5-260628`）。模式是 `video_edit`，分辨率 1080p，参考图为本段关键帧和锚点，提示词同时要求去掉 logo 和警示语。同时进行 4 段，可以断点续跑。
5. **拼接**（`assemble`）：Seedance 返回的是 24fps 视频（成片也是 24fps），时长比原片段略短，所以每段都会按原时长精确拉伸。镜头内部的接缝会做颜色匹配和交叉淡化，硬切处直接拼接。最后铺上原片对应区间的音频，输出 1080p H.264。
6. **质检**（`review`）：把原片和成片并排同步交给 `gemini-3.8-flash`，检查穿帮、bug、拼接色差、残留的 logo/文字和尾板，并附上程序自己测得的接缝色差。每个问题由主 agent 用 `frames` 抽帧确认，确认属实的片段用 `redo` 打回重做，最多 3 轮。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

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
| 图生图（关键帧） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.6-sunburst` |
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
