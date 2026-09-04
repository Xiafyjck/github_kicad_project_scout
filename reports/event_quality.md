# 改进事件质量报告

生成时间 2026-09-04T03:36:58.249709+00:00，由 `scripts/10_analyze_event_quality.py` 对阶段 08 的 381008 个事件计算，覆盖 `.kicad_pro` 搜到的 39902 个仓库。每个事件的标志在 `data/cache/event_quality/state.sqlite` 的 `event_quality` 表。

## 过滤规则

| 过滤 | 规则 |
|---|---|
| 范围 | 事件落在合格工程目录内；PR 事件必须已合并；至少改动一个 `.kicad_pcb` |
| 文本 | 标题 + 正文去掉 checklist、HTML 注释、引用、签名后 >= 100 字（PR 模板行删掉后按剩余文字计）；TODO 行 < 50%；不是纯 issue 编号 / URL |
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
| 文本：删模板行后正文 >= 100 字，非 TODO / 纯引用 | 4119 | 1191 |
| 规模：KiCad 改动行 <= 8000，改动文件 <= 40 | 1257 | 466 |
| KiCad 文件全为 modified（无 added / renamed / removed） | 1005 | 352 |
| patch 文本可用 | 581 | 205 |
| 语义改动行 >= 5（非只存盘 churn） | 394 | 185 |
| 仓库限额 20 以内 | 336 | 184 |

## 漏斗：commit 事件

| 步骤 | 事件 | 仓库 |
|---|---|---|
| 全部事件 | 347164 | 31977 |
| 落在合格工程目录内 | 347164 | 31977 |
| PR 已合并（commit 一律通过） | 347164 | 31977 |
| 改动了 .kicad_pcb | 275885 | 31972 |
| 文本：删模板行后正文 >= 100 字，非 TODO / 纯引用 | 23100 | 5138 |
| 规模：KiCad 改动行 <= 8000，改动文件 <= 40 | 10459 | 2899 |
| KiCad 文件全为 modified（无 added / renamed / removed） | 8447 | 2359 |
| patch 文本可用 | 4399 | 1429 |
| 语义改动行 >= 5（非只存盘 churn） | 3958 | 1372 |
| 仓库限额 20 以内 | 3403 | 1372 |

## 分层

| 等级 | PR 事件 | commit 事件 | 仓库 |
|---|---|---|---|
| A | 91 | 131 | 59 |
| B | 303 | 3827 | 1414 |
| C | 424 | 4048 | 1726 |
| X | 33026 | 339158 | 32498 |

最终池（A + B 且在限额内）：**3739** 个事件，来自 **1445** 个仓库（PR 336，commit 3403）；限额去掉 613 个。

## 排除原因

在范围内事件（合格目录、已合并或 commit、改了 pcb）上统计；一个事件可能同时不过多个过滤。

| 原因 | PR 事件 | commit 事件 |
|---|---|---|
| 文本 | 8033 | 252785 |
| 规模 | 8653 | 136734 |
| 含 added / renamed / removed | 6062 | 88818 |
| 只存盘 churn | 187 | 441 |

文本不合格的细分：

| 细分 | PR 事件 | commit 事件 |
|---|---|---|
| 清洗后正文短于阈值 | 8016 | 252743 |
| 带 PR 模板且删模板行后仍太短 | 0 | 0 |
| TODO 为主 | 2 | 80 |
| 只有 issue 编号 / URL | 149 | 2417 |

## 只存盘 churn

4980 个事件的每个 KiCad 文件都有 patch 文本。语义行与 churn 行：

| | 事件 | 语义行中位数 | churn 行中位数 | 只有 churn 的事件 |
|---|---|---|---|---|
| pull_request | 581 | 34 | 4 | 187 |
| commit | 4399 | 165 | 4 | 441 |

## 仓库集中度

限额前 A + B 事件最多的仓库：

| 仓库 | A + B 事件 | 限额后保留 |
|---|---|---|
| sabas0ba/kicad_skills | 158 | 20 |
| rjwalters/kicad-tools | 116 | 20 |
| LastZactionHero/defcon-silent-disco | 110 | 20 |
| Tinkerbug-Robotics/TinkerRocket | 67 | 20 |
| dektronics/printalyzer-timer | 63 | 20 |
| nielsverhoeven/PoE-FanController | 52 | 20 |
| Swyter/psdaptwor | 46 | 20 |
| worlickwerx/pi-cluster-two | 45 | 20 |
| nanographs/Scan-Gen-Glasgow-Testing | 36 | 20 |
| ideocentric/caryatid | 36 | 20 |
| EwoudVV/ducktop2 | 34 | 20 |
| BennetLeff/temper | 33 | 20 |

## 合格仓库中的 3D 模型

32029 个合格仓库中，16977 个至少带一个 3D 模型文件（.step, .stp, .wrl, .stl, .iges, .igs, .3mf, .obj, .f3d, .fcstd, .scad）；10492 个在工程目录内。按后缀的文件数：.obj 153016，.step 134340，.stl 67954，.stp 37827，.wrl 32117，.scad 19855，.fcstd 11431，.3mf 10295，.f3d 3674，.igs 565，.iges 317。见表 `repo_3d_models`。

## 样本

### A 级

- **rjwalters/kicad-tools** PR #2954，目录 `boards/03-usb-joystick/output`，pcb 2 sch 0 pro 0，语义 1206 行 churn 565 行，文本 6575 字，issue [2918, 2919, 2943]。*fix(board-03): nudge J2 west 2mm to clear JOY_Y channel (closes #2943)* [链接](https://github.com/rjwalters/kicad-tools/pull/2954)
- **nielsverhoeven/PoE-FanController** commit b58334002b，目录 `hardware/kicad`，pcb 1 sch 0 pro 0，语义 599 行 churn 95 行，文本 589 字，issue [148, 152]。*hw(pcb): T003 — sync PCB J8 pad nets to corrected assignments* [链接](https://github.com/nielsverhoeven/PoE-FanController/commit/b58334002b1ec606df897aec4962960e85ac7f19)
- **rjwalters/kicad-tools** commit 82a9525ec6，目录 `boards/00-simple-led/output`，pcb 1 sch 0 pro 0，语义 36 行 churn 2 行，文本 5168 字，issue [3509, 3714]。*feat(pcb): auto-size drawing sheet to board + center (page_fit) (#3715)* [链接](https://github.com/rjwalters/kicad-tools/commit/82a9525ec6d0004ff10d6a30b2d180fec8660301)
- **andreika-git/hellen-one** commit a5da2e4e43，目录 `kicad/modules/hellen1-wbo`，pcb 1 sch 1 pro 0，语义 79 行 churn 0 行，文本 109 字，issue [281]。*Revert " #281 CANH and CANL aren`t flipped now"* [链接](https://github.com/andreika-git/hellen-one/commit/a5da2e4e434119020e0df7d370dd62611a0455ff)

### B 级

- **mayashapiro19/table-zamboni** commit 964547fdc9，目录 `ToF_PCB`，pcb 1 sch 0 pro 0，语义 454 行 churn 175 行，文本 113 字，issue []。*Added layers and fixed layout + shape of pcb. Going to add esd protection now so pushing changes bef* [链接](https://github.com/mayashapiro19/table-zamboni/commit/964547fdc953fadebc64ebc95b675016558caaaa)
- **PubInv/general-purpose-alarm-device** commit aaae4f9fef，目录 `Hardware/GeneralPurposeAlarmDevicePCB`，pcb 1 sch 1 pro 1，语义 1537 行 churn 12 行，文本 145 字，issue []。*Make JST footprint for J105 in porject library. Change schematic for JST footprint. Import to PCB. P* [链接](https://github.com/PubInv/general-purpose-alarm-device/commit/aaae4f9fefec6c89240c208de056af3d295ea779)
- **DeMarco/DMH-VCO-40106** commit 320838d409，目录 `DMH_VCO_40106_PANEL`，pcb 1 sch 0 pro 0，语义 30 行 churn 0 行，文本 164 字，issue []。*Fixed resistor values and regenerated gerbers* [链接](https://github.com/DeMarco/DMH-VCO-40106/commit/320838d409babeaa6bfa71672ae396be86eb6533)
- **CactusRockets/Weiss_Placas** commit e3014e1841，目录 `Placa de processamento`，pcb 1 sch 1 pro 1，语义 849 行 churn 6 行，文本 242 字，issue []。*Placa de processamento: Aterrando o pino SDO do BMP388 para escolher o endereço do sensor. Com o BMP* [链接](https://github.com/CactusRockets/Weiss_Placas/commit/e3014e1841003d8d661f6f3ac7b91237aacbd3ca)

## 下一步

- 网表比较：在改动前后 sha 上解析 `.kicad_sch` / `.kicad_pcb` 的 S 表达式（需要完整文件，即 checkout），比较网络、元件、封装；上面的 churn 启发式只读 patch。
- 用 `kicad-cli`（本机在 /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli）对前后状态跑 DRC / ERC，保留错误数不增加的事件。
- 人工审阅 A 级种子集：`reports/seed_issue_driven_prs.csv`。
- 新增的 29279 个仓库拉完后重跑（Release C）。
