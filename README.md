# video-reskin

版本 `0.0.1`。批量把视频转换画风的标准 Agent Skill（技能），支持 Windows 10/11 x64 的本地执行环境。默认风格是“写实版材质，风格不变”，成片为 1080p（短边 1080），会去掉游戏 logo、底部警示语和片尾尾板，保留原声。

## 目录与分发

技能放在 `skill/video-reskin/`：里面是 `SKILL.md` 与 `references/`、`scripts/`、`assets/`、`requirements/`。直接压缩 `skill/video-reskin/` 文件夹即可打包分发（首次运行后在里面下载的 `runtime-cache/` 不需要带上），解压到任意技能目录（例如 `~/.claude/skills/video-reskin/`）即可使用。仓库根目录只放说明文档和 `dev/`（测试与验证工具）。

## 工作流

AI 阅读 `SKILL.md` 后通过统一入口操作：

```powershell
& "skill/video-reskin/scripts/video_reskin.cmd" --help
& "skill/video-reskin/scripts/video_reskin.cmd" auth --key <Winky API key>
& "skill/video-reskin/scripts/video_reskin.cmd" login --no-open
```

`init` → `analyze` → `survey` → `plan` → `refs` → `render` ⇄ `check` / `approve` / `redo`（逐段门禁：片段本身、与上一段的接缝、交接帧）→ `assemble`。没有全片审片：每一段和每个接缝都过了门禁，全片就没问题。

适用对象是 2.5D 俯视角的游戏视频：镜头在同一个场景里移动，角色走到不同地方，中途会冒出新的角色、物品和道具。所有片段共用同一套参考图（每个地点一张场景图、每个主体一张三视图），同一镜头内接续的片段再串联到前一段的成片上。

1. **分析**（`analyze`）：识别镜头切换，逐帧估算镜头在画面上的移动速度和方向（相位相关），找出尾板起点候选和静止叠加物（logo、警示语、HUD），生成联系表。
2. **看全片**（`survey`）：Gemini 完整看一遍原片，返回两类信息。一是场景：每个地点给出 3–8 个时刻，这些帧拼起来能覆盖镜头走过的整个场景范围；每块区域取它最早清楚出现的时刻，免得场景图里提前出现后来才堆起、建起的东西。二是主体：中途出现的角色、物品、道具，以及一直在场、会走动的主要角色，最多 8 个，每个给出出场区间、一个清楚的时刻和框。视频里读出的框常偏一个身位，所以脚本把那一帧截出来，让 Gemini 在静帧上重新框一次。同一次观看里，Gemini 还给出尾板起点、每个要去除的 logo/警示语/水印（文字、位置、出现区间）和要保留的 HUD，并据此生成 `suggested`（`keep` 和分时段的 `remove`）。之后 Gemini 再把地点帧和裁切图当静态图检查一遍（模糊、被挡、选得太晚、框错或框不全）。命令把这些都以文字输出，主 agent 只看尾板前后三帧、每个去除项一张标了红框的静态图和 Gemini 标出问题的图片，不再看整张联系表和汇总图（它们还在 `catalog/` 里，有疑问时再看）。然后写 edits：保留区间、材质、确认过的去除项，需要时修正 `scenes` / `subjects`。进入提示词的文字（材质、去除项、`extra`、纠正句）一律用英文。
3. **分段**（`plan`）：Seedance 2.5 视频编辑每次最多 30 秒（输出与输入等长，不传时长）。保留区间按最长 30 秒切段，优先在硬切处断开；镜头内部的断点放在可选范围最后 7 秒里镜头移动最快、或者画面本身有跳变（闪光、爆点、甩镜）的时刻。镜头内的接缝两侧各多渲染 0.3 秒作为重叠，不足 4 秒的段补到 4 秒。每段记录它用到的场景图和主体三视图（最多 2 张，本段新出现的优先，只生成会被某段用到的三视图）。
4. **参考图**（`refs`）：都用 GPT Image 2.6（`gpt-image-2.6-sunburst`，Lumos 图生图）。每个场景生成一张完整场景大图，4K、质量 high：把 survey 选出的几帧和风格参考图 `assets/styles/realistic.jpg` 一起交给模型，拼成整个场景的写实俯视全景，不带人物、UI 和文字，宽高比按镜头走过的范围估算。每个主体生成一张三视图（正、侧、背），1K、质量 medium：输入是裁切图、原帧和场景大图，写实程度与场景图一致。做完后 Gemini 逐张对照输入检查：场景图对照地点帧（布局、卡通残留、凭空多出的地方、提前出现的东西、人物/UI/文字），三视图对照裁切图（是否同一主体、部件和颜色、有无多加东西）。主 agent 只看被标出的那几张（`refs/check/<key>.png`，输入图和成图并排），确认后用 `refs --redo` 单独重做；全部图片仍汇总在 `sheets/<id>-refs.png`，只列出有段落用到的三视图。
5. **视频编辑**（`render`）：在 Lumos 上逐段调用 Seedance 2.5（`doubao-seedance-2-5-260628`，`video_edit` 模式，不生成音频），1080p 渲染，输入一段原片（本地先降到 24 帧/秒，即 Seedance 自己的帧率，不交给模型转换）和该段的参考图。提示词第一句是 “Edit the video: replace the materials …”（编辑模式需要“编辑/替换”这类字眼），要求逐帧换材质、机位和取景不变、只在原片切镜的地方切镜、不推近。镜头内接续的段多带一张参考图作为图 1：前一段成片在本段起点那一帧（取自前一段结尾的重叠部分，按比例对时），提示词要求开头与它完全一致，所以同一镜头内的段依次渲染，而且后一段只在前一段审过、`approve` 之后才提交：每次 `render` 渲出各镜头的下一段就停下（其余报 `waiting`），免得有问题的结果被串进后面的段、回炉时整串重渲；已批准的段在前一段日后重做时保留原结果，接缝在后一段的门禁里重新检查；不同镜头、不同视频 4 路同时进行。结果下载后用边缘图和粗布局两项指标与原片比对，两项都接近随机配对时判为偏离原片（自己切镜、推近），自动只带场景图重渲一次，不计入回炉次数；同一轮两次都偏离时保留较接近的一次，逐段门禁时列为高优先级问题。
6. **逐段门禁**（`check` / `approve`）：每次 `render` 之后，`check` 逐段审刚渲好、还没批准的结果，分三部分：
   - **片段本身**：Gemini 看该段和对应原片的并排对比（近无损，码率尽量填满 256,000,000 字节上限，每秒 6 帧），同时看下一段要接着画的交接帧（静态图），交接帧上的问题一律算高优先级。
   - **接缝**：把到这一段为止的所有片段先拼成预览（与成片用同一套对时、调色和接缝选帧，不带声音，放在 `work/preview/`），Gemini 看接缝前后各 1 秒和原片的并排对比；镜头内的接缝还要对比“这一段被要求接着画的那一帧”和“这一段实际的第一帧”。拼接测出的叠化差异、调色残差也算进问题。上一段还没批准时接缝记为 `pending`，等上一段批准后再查一次。
   - **判定**：片段有高优先级问题或两个以上中等问题、接缝有中等以上问题、或接缝还没查，都算不过。`approve` 只接受当前结果查过且通过的段；不通过的只能在核对了每张图、确认每个问题都不成立后，用 `--dismiss "理由"` 批准。下一段只在上一段批准后才生成，所以问题不会往后带。
   - **主 agent 核对**：先由 Gemini 看。Gemini 报了问题的地方，主 agent 看该问题的三帧核对图（开头、中间、结尾，短问题取中间和前后 0.3 秒）；Gemini 在接缝或交接帧上没发现问题时，主 agent 再自己复审接缝 5 帧图（前后各 0.5 秒，每 0.25 秒一帧）和全尺寸交接帧。不管看哪张图，都不能只核对报告的问题，也要通看整张图有没有别的问题（卡通材质残留、文字或 logo、人物或物体变了或不见了、颜色、地面），发现的同样算问题。确认属实的用 `redo` 打回（接缝问题打回后一段），每段最多回炉五次；同一问题前两次回炉不加或只加最少的纠正句，两次都没修好再注入针对性的提示词（按原片写明东西的名字和应有的样子、动作），必要时同时改参考图（`sheets`、`motion`、`split_at`）。
7. **拼接**（`assemble`）：
   - **对时**：每段只用一个恒定速度：按画面内容找出该段在接缝处对应原片的时刻，两头钉住，中间按成片自己的节奏逐帧播放；需要补的少量重复帧或跳帧放在画面静止的帧上，相隔至少 4 帧，不插帧。
   - **调色**：镜头内的接缝用最小二乘做颜色匹配。
   - **接缝**：以不加效果为主（用户要求）。在两段的重叠里找跳变最小的一帧切换：跳变不超过正常帧间变化的 1.15 倍、最差区域不超过 1.2 倍、两版画面相差不超过 30 时直接切；否则只做 6 帧（0.25 秒）非常轻微的叠化，不加模糊等其他效果。硬切处直接拼接。叠化处两版画面差异很大、或调色后仍有明显色差时，后一段的门禁里列为接缝问题。
   - **成片**：铺上原片对应区间的音频，输出 1080p（短边 1080）H.264，文件名为 `<原名>_realistic.mp4`。

成片默认放在桌面的 `视频换风格_YYYYMMDD_HHMMSS/` 下，同级只有一个任务目录 `temp/`。

## 账号

- **Lumos 登录**：`login` 会用隐藏的 Edge（或 Chrome）打开 Lumos 登录页，把飞书二维码保存成图片。每出一张新码就打印一行 `qr: <路径>`，AI 把图片发给用户扫码。飞书一两分钟换一次码，换了就发新的。页面刚打开时的第一张码会马上被替换，所以只有连续两秒不变的码才会打印。用户扫码后，技能只保存 refresh token，用它换取访问令牌。
- **Winky API key**：通过 `auth --key` 保存，只用于 Gemini（看全片、逐段门禁、检查参考图）。
- **存储方式**：两者都用 Windows DPAPI 加密，存在 `%APPDATA%\video-reskin\credentials.json`，只有当前 Windows 用户能解密。也可以用环境变量 `VIDEO_RESKIN_WINKY_KEY` 临时指定 key。

## 接口来源

| 用途 | 接口 |
| --- | --- |
| 登录 | `POST /marauders-map/lumos-auth/refresh`（携带 Cookie `lumos_refresh_token`）→ `lumos_access_token`，请求头为 `Authorization: LumosBearer …` |
| 上传 | `POST /legil/server/input-media-url` → PUT 到 OSS 签名地址 → `POST /legil/server/upload-callback`（`file_id`）→ `POST /legil/server/output-media-url` |
| 视频编辑 | `POST /legil/server/generations/video-to-video`，参数 `model: doubao-seedance-2-5-260628`、`generation_mode: video_edit`、`generate_audio: false`、`output_format: mp4`、`reference_videos`（1 段，2–30 秒，≤200 MB）、`reference_images`（≤30 张，≤30 MB）、`prompt`、`resolution: 1080p`（或 `480p`/`720p`）、`aspect_ratio: adaptive`、`sample_count: 1`，编辑模式不传时长；轮询 `GET /legil/server/generations/{id}` |
| 图生图（参考图） | `POST /legil/server/generations/image-to-image`，参数 `model: gpt-image-2.6-sunburst`、`resolution`（`1K`/`2K`/`4K`，4K 需指定宽高比）、`quality`（`low`/`medium`/`high`/`xhigh`/`max`/`auto`），最多 16 张参考图 |
| 看全片、逐段门禁、检查参考图 | `POST https://lumos.diandian.info/winky/gemini/v1/models/gemini-3.8-flash:generateContent`（`inlineData` 视频或图片 + `videoMetadata.fps` + `responseSchema`） |

Lumos 的接口是从网页的实际请求中整理出来的，网页改版后可能需要跟着调整。

## 按需依赖与缓存

| 使用功能 | 自动准备 |
| --- | --- |
| 基础命令 | 私有 Python 3.13.15（uv 0.13.4） |
| 分析、看全片、参考图、渲染、拼接、逐段门禁 | NumPy 2.5.3、Pillow 12.3.0、FFmpeg/FFprobe 9.0.2 |
| 扫码登录 | Playwright 1.64.1（使用系统自带的 Edge，不下载浏览器） |

依赖版本和哈希都已锁定。缓存默认放在技能目录内的 `runtime-cache/`，可以用 `VIDEO_RESKIN_CACHE_DIR` 改到其他位置；设置 `VIDEO_RESKIN_OFFLINE=1` 可禁止联网。退出码：`0` 成功；`2` 输入、凭据或规则问题；`3` 运行时或服务故障。

## 开发与验证

- `python dev/run_tests.py`：不需要网络的回归测试（用 `runtime-cache/environment/Scripts/python.exe` 运行，需要 NumPy）。
- `python dev/test_bootstrap.py`：在中文路径下验证冷启动和离线复用。
- `python dev/smoke/e2e.py`：用 `.素材` 里的视频跑一遍真实的端到端流程，需要 Lumos 登录和 key。
- `python dev/check_skill.py`：检查 `skill/video-reskin/` 是否是可直接分发的技能（`SKILL.md` 头部、引用文件、文件大小与类型）。
- `dev/VALIDATION.md`：各项规则和阈值背后的实测依据。
