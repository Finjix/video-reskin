# 换元素

每条原片勘察一次，选出观众在手机上正常播放就能一眼认出变化的目标。一版可以换一个元素，也可以在同一份成片里同时换多个元素。逐项写清「原元素 → 替代物」，每份成片单独命名，直接从原片制作。

## 选目标与设计替代物

比较几个候选的出现次数、首次清楚入镜时间、占屏大小、累计可见时间，以及被截断或遮挡的程度。优先选成堆的货物、来回经过画面的车队、占据前景的大件；单个物体也可入选，例如开场占大半画面的主角座驾。晚出现、常在画面边缘只露一角的机器，优先级低于从开场贯穿加工、搬运和销售的物资。用 `target_selection` 记下与备选对象的比较理由，以及最早清楚可见、变化最明显、再次出现的帧。

把设计落实到可见结构，例如青竹的管壁、竹节与空心切面，或翻斗车厢的铆钉、斜侧板与翻转轴。先确认手机大小下能辨认轮廓、截面、部件或堆叠方式，再用配色和纹理加强差异。用户指定了品种、结构或配色时照做。替换后仍要讲得通：角色能拿起、加工、装载并交付这种物资。

- **钞票不替换。** 地上的纸币、交付台上的钱捆、角色背负的钱叠、消费时飞散的钞票和金额界面，都保留原片的外形、颜色、数量变化和运动；参考图保持原片当时的现金状态，包括清空后的货台。即使钞票与目标共用背运、掉落或拾取动作，也只改选中的物资。工单会拒绝以钞票为目标，每条生成提示词也会附带保留钞票的说明。
- **冰冻版**沿用原模型，在表面包一层手机上能看清厚度的冰：冻木透过冰层仍能看见树皮和年轮，冻肉保留原来的形状和切面，冰层随物体一起搬运、掉落和转弯。参考图和 Seedance 提示词用同一种处理。多版批次里至少有一版是非冰冻造型（如空心竹束、带铆钉的木箱），否则工单会被拒绝。
- **各版造型不重复。** 一次交付多个版本时，每种替代造型只给一份成片，换个称呼也算重复。工单按替代物描述和可选的 `replacement_key` 查重（统一全半角、大小写并去掉空白），同义名请用同一个 `replacement_key`。最后看参考图复核实际外观。

## 建工单与勘察

```json
{"sources":["D:/footage/source.mp4"],"style":"elements","output_root":"D:/delivery"}
```

用 `init --work-order order.json --job-dir <工作区>/temp` 初始化，成片直接写入 `output_root`。后续命令都带 `--job-dir <工作区>/temp`，`--only V01` 可只处理一版（`analyze`、`survey` 用源 ID，如 `S01`）。

`survey --only S01` 输出带原生帧号的采样图，`survey --at 3.25` 输出指定秒数的原尺寸帧。记录目标进出画、遮挡和再出现的区间；帧范围用原片帧号，含起点不含终点。尾板和非游戏镜头保持原样。

在任务临时目录做一张紧凑参考板：每行写目标、阶段和原生帧号，左侧原片，中间同视角的替代结构，右侧按手机大小缩小的样张。出现截面变化、远近切换或新的遮挡关系时补一行，同尺度同视角的行可复用。

| 原片阶段 | 板上需要辨认的细节 |
| --- | --- |
| 近景加工 | 大件轮廓、截面厚薄、数量，以及切开后的小件结构 |
| 地面料堆与远景 | 单件缩小后的外形、朝向、堆叠层次和留在地面的余料 |
| 拾取、背运与转身 | 侧面外形，角色身体、手、计数文字与物资的前后关系 |
| 交接与卸料 | 穿过栏杆或车厢边缘时的遮挡、装载位置、末次退场及再出现 |

同一机位可能先后出现不同动作，例如超载过桥和正常过桥、空车和装煤、地面发钱和角色取走。分别查看动作前后的帧，参考图里保留各自的空货台、桥栏、背包和已装货物。多个目标交叠时（如货车和货物都替换），在装车阶段的样张上同时核对车沿、货物和角色；只出现车斗的画面只给车斗样图。

## 计划

把计划存为 `variants.json`，执行 `plan --input variants.json`。若初始化前已勘察完，也可直接写进工单的 `variants`。下例生成两份成片：第一份同时换木料和货斗，第二份只换货箱。

```json
{
  "variants": [
    {
      "source": "S01",
      "name": "青竹和铆钉翻斗_竖版",
      "targets": [
        {
          "element": "原木和加工后的木段",
          "replacement": "带竹节与空心管壁的粗竹筒",
          "replacement_key": "hollow-bamboo",
          "ranges": [[0, 180], [300, 490]],
          "target_selection": {
            "reason": "开场和地面料堆占屏大，切开后竹节和截面仍可辨认。",
            "frames": {"加工近景": 24, "堆料远景": 355, "背运遮挡": 418}
          }
        },
        {
          "element": "运货车斗",
          "replacement": "有斜侧板和翻转轴的铆钉翻斗",
          "ranges": [[425, 525]],
          "target_selection": {"frames": {"与货物交叠": 461}}
        }
      ],
      "seedance": {
        "stages": [
          {"range": [0, 180], "reference_frame": 24, "reference_prompt": "Give the harvested logs thick hollow bamboo walls and raised nodes.",
           "prompt": "Replace the original logs with thick hollow bamboo. Keep the visible nodes and tube walls through the cut. Preserve camera movement, tools and text."},
          {"range": [300, 525], "references": ["D:/work/ref/bamboo-stack.png", "D:/work/ref/tipper.png"],
           "prompt": "Show hollow bamboo from pickup through loading. When the truck arrives, give it a riveted tipper bed with sloping sides. Preserve the order of the actor, load, rails and bed as they overlap."}
        ]
      },
      "motion_review": {"加工全程": [0, 180], "拾取至装车退场": [295, 530]}
    },
    {
      "source": "S01",
      "name": "货箱换蓝色冷藏箱_竖版",
      "element": "连续平移近景中占据前景的木货箱",
      "replacement": "同轮廓、带金属扣的蓝色冷藏箱",
      "ranges": [[300, 366]],
      "seedance": {
        "stages": [
          {"range": [300, 366], "references": ["D:/work/ref/cooler.png"],
           "prompt": "Replace the wooden crate with the blue insulated cooler shown in the reference, including metal latches and a thick lid. Preserve its motion and the foreground occlusions."}
        ]
      },
      "review_frames": [341],
      "motion_review": {"货箱入画至退场": [296, 370]}
    }
  ]
}
```

- **目标。** 多目标用 `targets` 逐个登记，各自的 `ranges` 省略时继承版级 `ranges`；单目标直接写版级 `element`、`replacement` 和 `ranges`。一份计划输出一条命名成片。
- **阶段。** `seedance.stages` 按动作写原生帧范围、英文 `prompt` 和参考图，合起来正好覆盖目标区间。提示词写明原对象、替代物结构、整段动作中的变化，以及要保留的镜头、角色、文字和遮挡；两个目标同框时分别说明结构、相互遮挡和出现时间。
- **参考图。** 已有成图写进 `references`（每段 1 至 30 张，选清晰、视角匹配的图）。需要新图时省略 `references`，填原片帧号 `reference_frame`（默认阶段首帧）和静帧说明 `reference_prompt`（默认沿用 `prompt`），由 Lumos 图生图生成；提示词会自动带上该阶段在场目标的替代物名称。同一原帧和说明会复用已生成的图片。
- **切片。** 与换风格共用同一个规划器：`surveys.S01` 可传入换风格勘察的 `non_game`、`end_card_start`、`cuts`（秒），同一原片的各版共用。规划器按最少段数和语义切点把阶段合并成 Seedance 请求，每个请求 4 至 30 秒；短动作以末帧补足 4 秒，输出时按原片帧号截回。没有目标的片段不生成，片段内目标离场的画面从原片恢复。目标不能落在非游戏画面或尾板上。

`plan` 只在本地登记，并列出 `image_tasks` 和计划的片段数。

## 生成

1. `refs` 提交并下载计划中的 Lumos 参考图。核对返回图片后再继续。
2. `render` 把原片区段、参考图和提示词交给 Seedance，保存每段的模型、生成 ID 和返回视频校验和（见本版 `render.json` 的 `generations`），再按原片帧序合成。参考图过小（短边低于 300 像素）时上传副本会等比放大。

选择多个版本时，`refs` 和 `render` 先提交所有版本的任务再统一等待，各版同时生成。已提交的任务会复用，中断后重跑同一命令即可续上。

同一原片帧在同一版本里最多提交 3 次 Seedance（首发加两次重试），失败和回执不明确的提交也计数；调整切点、参考图或提示词后，覆盖相同原片帧的提交仍计入。再试之前先查看同版已有的回包，保留合格的时间窗。`status` 显示逐段的生成 ID 和收件状态。

## 核对与交付

`check` 输出对照页和动态对照视频。先在优先展示的代表帧（`target_selection.frames`、`review_frames`）上核对结构和遮挡；`check --at 3.25` 输出指定时间的原尺寸对照帧。

动态对照视频左边原片、右边成片，保留全部连续帧和原播放间隔。`motion_review` 给原生帧区段命名（可含进出画前后的原片），Seedance 区段中未命名的部分按帧号自动补齐，路径与校验和记在本版 `check.json` 的 `motion_clips`。按正常速度从入画、形变、遮挡一直看到末次退场，重点看重复动作和多个目标同时在场的时刻。

- 对照同一时刻的货物种类、数量和货台空满，再看替代物是否稳定。
- 成堆物料移动和遮挡时会分成几簇：分别跟踪地面余料、背上的料和落到带面上的料，直到每一簇离开画面。
- 留意原色回闪、切面跳亮、轮廓漂移和遮挡物串色，特别是靠近物料的数字、栏杆立柱、手和货箱边缘。发现闪动时逐帧对照前后画面，区分游戏本身的拾取亮光。

发现形状、动作或遮挡错误时，在原阶段内调整提示词或参考图，用 `plan --input` 重新登记，再 `render --only V01` 并重新审核。只改审核帧、勘察备注或 `motion_review` 时，重新 `check` 会复用已有画面，不再生成。

通过后执行 `approve`，再 `assemble`。每版按工单的文件名导出 MP4，沿用原片帧序与节奏，保留原声。
