# 改进事件质量报告

生成时间 2026-09-04T02:01:40.587888+00:00，由 `scripts/10_analyze_event_quality.py` 对阶段 08 的 523908 个事件计算，覆盖 `.kicad_pro` 搜到的 39902 个仓库。每个事件的标志在 `data/cache/event_quality/state.sqlite` 的 `event_quality` 表。

## 过滤规则

| 过滤 | 规则 |
|---|---|
| 范围 | 事件落在合格工程目录内；PR 事件必须已合并；至少改动一个 `.kicad_pcb` |
| 文本 | 标题 + 正文去掉 checklist、HTML 注释、引用、签名后 >= 100 字；不是 PR 模板（checklist 标记）；TODO 行 < 50%；不是纯 issue 编号 / URL |
| 规模 | 事件内 KiCad 文件的增删行合计 <= 8000；改动文件总数 <= 40 |
| 只改已有文件 | 每个 KiCad 文件状态都是 `modified`，保证前后状态一一对应 |
| 语义 | 从 patch 文本看，行首 token 不属于存盘 churn（`uuid`、`tstamp`、`version`、`generator` 等）的改动行 >= 5；这是网表比较的代理，完整比较需要整个文件 |
| 限额 | 每个仓库最多 20 个事件，先按等级再按文本长度 |

等级：**A** 全部通过且关联 issue；**B** 全部通过；**C** 文本 / 规模 / modified 通过但 GitHub 省略了 patch（文件过大），语义未知；**X** 排除。

## 漏斗：PR 事件

| 步骤 | 事件 | 仓库 |
|---|---|---|
| 全部事件 | 33844 | 4418 |
| 落在合格工程目录内 | 16372 | 3520 |
| PR 已合并（commit 一律通过） | 14200 | 3197 |
| 改动了 .kicad_pcb | 12152 | 3022 |
| 文本：清洗后正文 >= 100 字，非模板 / TODO / 纯引用 | 3534 | 1125 |
| 规模：KiCad 改动行 <= 8000，改动文件 <= 40 | 1066 | 435 |
| KiCad 文件全为 modified（无 added / renamed / removed） | 855 | 325 |
| patch 文本可用 | 511 | 190 |
| 语义改动行 >= 5（非只存盘 churn） | 328 | 169 |
| 仓库限额 20 以内 | 283 | 168 |

## 漏斗：commit 事件

| 步骤 | 事件 | 仓库 |
|---|---|---|
| 全部事件 | 490064 | 32026 |
| 落在合格工程目录内 | 490064 | 32026 |
| PR 已合并（commit 一律通过） | 490064 | 32026 |
| 改动了 .kicad_pcb | 275885 | 31972 |
| 文本：清洗后正文 >= 100 字，非模板 / TODO / 纯引用 | 23086 | 5138 |
| 规模：KiCad 改动行 <= 8000，改动文件 <= 40 | 10456 | 2899 |
| KiCad 文件全为 modified（无 added / renamed / removed） | 8444 | 2359 |
| patch 文本可用 | 4398 | 1429 |
| 语义改动行 >= 5（非只存盘 churn） | 3957 | 1372 |
| 仓库限额 20 以内 | 3416 | 1372 |

## 分层

| 等级 | PR 事件 | commit 事件 | 仓库 |
|---|---|---|---|
| A | 70 | 130 | 57 |
| B | 258 | 3827 | 1411 |
| C | 344 | 4046 | 1722 |
| X | 33172 | 482061 | 32544 |

最终池（A + B 且在限额内）：**3699** 个事件，来自 **1441** 个仓库（PR 283，commit 3416）；限额去掉 586 个。

## 排除原因

在范围内事件（合格目录、已合并或 commit、改了 pcb）上统计；一个事件可能同时不过多个过滤。

| 原因 | PR 事件 | commit 事件 |
|---|---|---|
| 文本 | 8618 | 252799 |
| 规模 | 8653 | 136734 |
| 含 added / renamed / removed | 6062 | 88818 |
| 只存盘 churn | 183 | 441 |

文本不合格的细分：

| 细分 | PR 事件 | commit 事件 |
|---|---|---|
| 清洗后正文短于阈值 | 8016 | 252743 |
| PR 模板 | 585 | 14 |
| TODO 为主 | 2 | 80 |
| 只有 issue 编号 / URL | 149 | 2417 |

## 只存盘 churn

4909 个事件的每个 KiCad 文件都有 patch 文本。语义行与 churn 行：

| | 事件 | 语义行中位数 | churn 行中位数 | 只有 churn 的事件 |
|---|---|---|---|---|
| pull_request | 511 | 24 | 3 | 183 |
| commit | 4398 | 165 | 4 | 441 |

## 仓库集中度

限额前 A + B 事件最多的仓库：

| 仓库 | A + B 事件 | 限额后保留 |
|---|---|---|
| sabas0ba/kicad_skills | 158 | 20 |
| LastZactionHero/defcon-silent-disco | 110 | 20 |
| rjwalters/kicad-tools | 97 | 20 |
| Tinkerbug-Robotics/TinkerRocket | 67 | 20 |
| dektronics/printalyzer-timer | 63 | 20 |
| nielsverhoeven/PoE-FanController | 52 | 20 |
| Swyter/psdaptwor | 46 | 20 |
| worlickwerx/pi-cluster-two | 45 | 20 |
| nanographs/Scan-Gen-Glasgow-Testing | 36 | 20 |
| ideocentric/caryatid | 36 | 20 |
| EwoudVV/ducktop2 | 34 | 20 |
| v3l0c1r4pt0r/ucm4 | 30 | 20 |

## 合格仓库中的 3D 模型

32029 个合格仓库中，16977 个至少带一个 3D 模型文件（.step, .stp, .wrl, .stl, .iges, .igs, .3mf, .obj, .f3d, .fcstd, .scad）；10492 个在工程目录内。按后缀的文件数：.obj 153016，.step 134340，.stl 67954，.stp 37827，.wrl 32117，.scad 19855，.fcstd 11431，.3mf 10295，.f3d 3674，.igs 565，.iges 317。见表 `repo_3d_models`。

## 样本

### A 级

- **spacelab-ufsc/pc104-adapter** commit 72978706ff，目录 `/`，pcb 1 sch 0 pro 0，语义 834 行 churn 4 行，文本 115 字，issue [3]。*bottom-board: layout: Fixing the PC-104 outline (silk screen) and adding a label with the board name* [链接](https://github.com/spacelab-ufsc/pc104-adapter/commit/72978706ff397f3ddf600666e1ed5b88997e9526)
- **nielsverhoeven/PoE-FanController** commit 92e69c7dab，目录 `hardware/kicad`，pcb 1 sch 1 pro 0，语义 246 行 churn 77 行，文本 2400 字，issue [40]。*hw(pcb): fix DRC violations from ESP32-P4 layout -- J1 footprint + placement (#40)* [链接](https://github.com/nielsverhoeven/PoE-FanController/commit/92e69c7dab3576d3d744fe9c62401113ac47f6da)
- **nielsverhoeven/PoE-FanController** commit 6f8b905e63，目录 `hardware/kicad`，pcb 1 sch 0 pro 1，语义 895 行 churn 118 行，文本 765 字，issue [75]。*hw(pcb): portrait layout 42x78mm, J8 repositioned per constitution v3.1.0* [链接](https://github.com/nielsverhoeven/PoE-FanController/commit/6f8b905e63a310fa9a67fdc973318f9555ae7e0e)
- **open-ephys/ephys-test-board** PR #45，目录 `pcb`，pcb 1 sch 1 pro 1，语义 499 行 churn 59 行，文本 112 字，issue [41]。*Add bypass for battery protection circuit* [链接](https://github.com/open-ephys/ephys-test-board/pull/45)

### B 级

- **OpenDrone-hw/OpenESC-30x30** commit 63119a4f0f，目录 `hardware`，pcb 1 sch 0 pro 0，语义 688 行 churn 8 行，文本 212 字，issue []。*Rebrand back silkscreen to incutec, 4.7uF bulk caps on board* [链接](https://github.com/OpenDrone-hw/OpenESC-30x30/commit/63119a4f0f041316157fbf0d3bf13e6b7f86bf2d)
- **tnl3pdx/SleepBud** commit a5678791ff，目录 `KiCad Schematics/SleepBud`，pcb 1 sch 1 pro 0，语义 42 行 churn 52 行，文本 131 字，issue []。*PCB Done Needs Checking Over* [链接](https://github.com/tnl3pdx/SleepBud/commit/a5678791ff23c33bdb8be031b1187fe799e45b99)
- **10-X-eng/KiChad** commit c62b9aad23，目录 `qa/data/pcbnew`，pcb 1 sch 0 pro 0，语义 55 行 churn 11 行，文本 181 字，issue []。*API: Avoid redundant serialization of polyline arcs* [链接](https://github.com/10-X-eng/KiChad/commit/c62b9aad23f88a5beb2a5f50a8b3a862e2f2b1b3)
- **portlandrobotics/common_platform** PR #14，目录 `hardware/romi_board`，pcb 1 sch 0 pro 0，语义 577 行 churn 10 行，文本 102 字，issue []。*Fix power/ground terminal swap* [链接](https://github.com/portlandrobotics/common_platform/pull/14)

## 下一步

- 网表比较：在改动前后 sha 上解析 `.kicad_sch` / `.kicad_pcb` 的 S 表达式（需要完整文件，即 checkout），比较网络、元件、封装；上面的 churn 启发式只读 patch。
- 用 `kicad-cli`（本机在 /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli）对前后状态跑 DRC / ERC，保留错误数不增加的事件。
- 人工审阅 A 级种子集：`reports/seed_issue_driven_prs.csv`。
- 新增的 29279 个仓库拉完后重跑（Release C）。
