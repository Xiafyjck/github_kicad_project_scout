# pcb_project_scout

[English](README.md) | 中文

从 GitHub 挖掘开源 KiCad 工程，保留文件齐全且有说明的，再把每一次有说明的设计改动（PR 或 commit）提取出来，作为 PCB 修改 benchmark 的素材：改动前的工程是输入，改动后的工程是参考答案，PR / issue / commit 的文字是题面。

贡献者与编码 agent 的开发约定见 [AGENTS.md](AGENTS.md)。

## 不跑 GitHub API 直接拿数据

每个阶段把原始 API 响应缓存在 `data/cache/` 下各自的 SQLite 里。整套缓存发布为 ModelScope 数据集 [Mask2X/pcb-project-scout](https://modelscope.cn/datasets/Mask2X/pcb-project-scout)，布局与 `data/cache/` 完全一致：每个阶段一个 `cache/<stage>/state.sqlite.zst`，外加 `manifest.json`。

```bash
uv run scripts/00_restore_cache.py
```

按脚本顶部 `STAGES` 常量下载所需阶段，校验 sha256，解压到 `data/cache/<stage>/state.sqlite`。缓存就位后，后续所有脚本全部命中缓存，不发任何 API 请求。只要派生表的话，把大库（`github_repo_history` 解压 24 GB、`github_commit_files` 解压 50 GB）从 `STAGES` 里注释掉。

| 阶段 | 脚本 | 解压后 | 内容 |
|---|---|---|---|
| github_code_search_kicad_pro / kicad_pcb / kicad_sch / sch | 01 | 8.3 GB | 各后缀的代码搜索页 |
| github_candidates | 02 | 0.7 GB | 合并后的候选仓库列表 |
| github_trees | 03 | 5.0 GB | 每个仓库的完整文件树 |
| filter_kicad_projects | 04 | 0.04 GB | 初筛合格的工程与仓库 |
| github_repo_stats | 06 | 0.05 GB | 每仓库的 commit / PR / issue / star 计数 |
| github_repo_history | 07 | 24.6 GB | commit、PR、PR 改动文件（含 patch）、issue |
| improvement_events | 08 | 6.1 GB | 派生：commit、PR、issue、文件、改进事件 |
| github_commit_files | 09 | 50.3 GB | 工程目录内每个 commit 的改动文件（含 patch） |

## 运行流程

> 多 GitHub 账号 token 并行可提升速度；同一账号多 token 不增加 API 配额。

0. **恢复缓存** `00_restore_cache.py`。可选。从 ModelScope 下载已发布的缓存（见上）。
1. **多策略搜索** `01_github_code_search_bins.py`。GitHub Search API 不保证返回全部结果，且只索引 384 KB 以下的文件，所以搜多个后缀取并集。`SUFFIXES` 常量里的每个后缀按文件大小二分枚举，各自缓存到 `data/cache/github_code_search_<suffix>/state.sqlite`。
2. **合并候选仓库** `02_github_merge_candidates.py`。汇总各后缀搜索返回的仓库，按 `repo_id` 去重，写出统一候选列表 `data/cache/github_candidates/state.sqlite`。纯本地，可重复执行。
3. **拉取文件树** `03_github_fetch_trees.py`。只读候选库，通过 GitHub Tree API 拉每个仓库的完整递归文件列表，原始响应与每仓库状态存入 `data/cache/github_trees/state.sqlite`。不做业务判断。被 GitHub 截断的仓库（39902 中 21 个）标记 `truncated`，下游忽略。
4. **本地初筛** `04_filter_kicad_projects.py`。只读候选库与文件树库，纯本地运算，每次全量重算，写出 `data/cache/filter_kicad_projects/state.sqlite`：`qualified_projects`（同目录含 `.kicad_pro` + `.kicad_pcb` + `.kicad_sch` 且附近有 README，每目录一行）、`qualified_repos`（每仓库一行）、`repo_filter_status`。
5. **发布表导出** `05_release_github_trees.py`。导出 `repos.csv`、`trees.jsonl`、`qualified_repos.csv`、`manifest.json` 到 `data/releases/<date>/` 并打 zip。为 GitHub Release A 保留；ModelScope 数据集已取代它。
6. **仓库活跃度统计** `06_github_fetch_repo_stats.py`。GitHub GraphQL，每次查询 25 个仓库：默认分支 commit 数、PR 总数与已合并数、issue 数、fork / archived / disabled 标志、parent、pushedAt、stars。原始响应按 query + variables 缓存，每仓库一行 `repo_stats`。
7. **仓库历史** `07_github_fetch_repo_history.py`。对每个候选仓库拉四类列表的全部分页：每个合格工程目录的 `commits?path=<project_dir>`（无合格目录则拉整库历史）、全部 PR、每个 PR 的改动文件（含 patch）、全部 issue。原始页按 URL + 参数缓存；`listing_pages` 按仓库、类型、主体、页号索引。
8. **改进事件** `08_build_improvement_events.py`。本地后处理，读 07 与 09 的缓存：`commits`、`commit_touches`、`pull_requests`、`pull_request_files`、`commit_files`、`issues`、`improvement_events`。一个事件是一次带说明的 PCB 改动：改动文件含 `.kicad_pcb` / `.kicad_sch` / `.kicad_pro` 的 PR（每个 PR 与工程目录一条，before = base sha，after = merge 或 head sha），或合格工程目录下列出且改了该目录 KiCad 文件的 commit（before = 第一个父提交，after = 该 commit）。附带按后缀的文件计数、改动文件清单、`#N` 引用的 issue。
9. **commit 改动文件** `09_github_fetch_commit_files.py`。对 07 在合格工程目录下列出的每个 commit 拉 `commits/{sha}` 的全部文件页，使 commit 事件与 PR 事件有同样的文件明细。之后重跑 08。
10. **事件质量** `10_analyze_event_quality.py`。本地。给每个事件打 benchmark 可用性分：文本质量（剔除模板、TODO 列表、纯 issue 编号）、改动规模上限、KiCad 文件全为 modified、从 patch 文本识别只存盘的 churn（uuid / tstamp / version 行与真实改动分开计数）、仓库限额，另附合格仓库的 3D 模型普查。写出 `data/cache/event_quality/state.sqlite` 与报告 `reports/event_quality.md`，以及 issue 驱动事件的种子 CSV。

按编号顺序运行：

```bash
uv run scripts/00_restore_cache.py          # 可选，跳过全部 API
uv run scripts/01_github_code_search_bins.py
uv run scripts/02_github_merge_candidates.py
uv run scripts/03_github_fetch_trees.py
uv run scripts/04_filter_kicad_projects.py
uv run scripts/06_github_fetch_repo_stats.py
uv run scripts/07_github_fetch_repo_history.py
uv run scripts/08_build_improvement_events.py
uv run scripts/09_github_fetch_commit_files.py
uv run scripts/08_build_improvement_events.py   # 再跑一次，把 commit 事件的文件填上
uv run scripts/10_analyze_event_quality.py
```

每个脚本从自己的 SQLite 缓存断点续跑。缓存完整时整条链重跑不发任何 API 请求。脚本无命令行参数，运行参数是各脚本顶部的常量。

## 目前结论（2026-09-03，深度分析覆盖 `.kicad_pro` 搜到的 39902 个仓库）

- **发现**。69181 个仓库含 PCB 设计文件，其中 50796 个用 KiCad 6+ 格式，18385 个只有旧格式 `.sch`。代码搜索只索引 384 KB 以下的文件，一半 `.kicad_pcb` 超过此值，仅靠 `.kicad_pcb` 搜索只能找到 43% 的完整工程；`.kicad_pro` 是可靠入口，因此取多后缀并集。
- **完整工程**。39902 个 `.kicad_pro` 仓库中 32029 个（80%）至少有一个目录同时含工程、原理图、布局三类文件且附近有 README，共 66793 个工程目录。
- **活跃度**。仓库中位数：16 次 commit，0 PR，0 issue，0 star。80% 从未收到 PR，84% 从未有 issue；6897 个仓库（17%）有合并 PR。91% 建于 2022 年之后；67% 最近一年有推送。
- **历史**。140678 个 PR，其中 18348 个（13%）改动 KiCad 文件，来自 4418 个仓库、5453 位作者；已合并 15483 个。KiCad PR 逐年翻倍（2022 年 1287，2024 年 3016，2025 年 4031，2026 年至今 3871）。423293 个 commit 触及完整工程目录；97849 个 issue。
- **benchmark 素材**。完整工程内已合并且改了 `.kicad_pcb` 的 PR：8719 个，来自 3022 个仓库，其中说明超过 100 字的 3292 个，关联 issue 的 801 个。改了 `.kicad_pcb` 的 commit 事件 275885 个，其中多行提交说明的 34535 个，正文超过 100 字的 15864 个。每个事件带前后 sha、改动的 KiCad 文件及状态与行数，patch 全文在缓存里。

## 十个示例事件

从最终池（A / B 级）挑选：每条是一次有说明的改动，原理图与布局都动了，改动前后状态一一对应。

| 仓库 | 改动 | 改了什么 |
|---|---|---|
| Tinkerbug-Robotics/TinkerRocket | [commit edef97e7](https://github.com/Tinkerbug-Robotics/TinkerRocket/commit/edef97e7cfe0f62e66d90c6e89ba15fb602df408) | 按 JST VH 电池输入重新计算 eFuse 限流电阻，100R 改 127R，依据数据手册；关联 issue #658 |
| greatscottgadgets/cynthion-hardware | [commit 9f84e603](https://github.com/greatscottgadgets/cynthion-hardware/commit/9f84e60331b9a53fa23bea8aa7e268448d4a9bc2) | 测试表明 ULPI 串联电阻对辐射无改善，改为 0 欧 |
| v3l0c1r4pt0r/ucm4 | [commit 77b04758](https://github.com/v3l0c1r4pt0r/ucm4/commit/77b04758c1153bfeca18e843d32b71fc71f97494) | TVS 二极管换成 TI 器件，封装改 USON-10 |
| worlickwerx/pi-cluster-two | [commit b4cad36b](https://github.com/worlickwerx/pi-cluster-two/commit/b4cad36b61249c70ef9f3a4511c5a185b0e4123f) | v3.1 板上测试点丝印标号没出来，修正字段设置使其绘制到丝印层 |
| DashiellRussell/18650-cell-carriers | [commit 32ce50d9](https://github.com/DashiellRussell/18650-cell-carriers/commit/32ce50d989bf1e01b6f9e1e0f4eb47fda2270419) | 板长缩到 117.4 mm，倒圆角，线焊盘与锡桥两列合并为一个封装 |
| jalopezg-git/libledmtx | [PR #13](https://github.com/jalopezg-git/libledmtx/pull/13) | 32x8 LED 矩阵驱动板评审修正，含换用电流余量更大的移位寄存器 |
| MatthiasElectronic/AWG_DHO8-900 | [PR #2](https://github.com/MatthiasElectronic/AWG_DHO8-900/pull/2) | 加封装旋转字段，让 JLCPCB 贴片方向正确 |
| flohoff/vbus-adapter | [commit 2f14aad9](https://github.com/flohoff/vbus-adapter/commit/2f14aad9af962806a60f4045787c935b95a1f3d8) | 隔离器换成默认低电平型号，掉电时不再把总线拉低，加下拉电阻 |
| eaguirre12/CUNY-Js5 | [commit e48cdeb3](https://github.com/eaguirre12/CUNY-Js5/commit/e48cdeb3b19f2a4b7889bdd19c922902555c40da) | 删掉未用连接器，USB 连接器改为说明，加热电阻标记为不上 BOM |
| cajunpanda/gameboy-hifi-audio | [commit b201af75](https://github.com/cajunpanda/gameboy-hifi-audio/commit/b201af75e8ed27f324039df629b385db4026f844) | B 版：ESP32 模组换内置天线型号，放大器输入改交流耦合，去掉两个电阻 |

## 目录结构

```
pcb_project_scout/
├── README.md
├── README.zh-CN.md
├── AGENTS.md         # 贡献者与编码 agent 的开发约定
├── pyproject.toml    # 依赖：uv、httpx、python-dotenv、modelscope、zstandard
├── .env.example      # 环境变量：GITHUB_TOKEN_1..N（拉取）、MODELSCOPE_TOKEN（仅发布）
├── reports/          # 生成的分析报告（入库）
├── scripts/          # 各阶段自包含脚本，按编号顺序执行，均支持断点续跑或全量重算
│   ├── 00_restore_cache.py             # 从 ModelScope 下载并解压已发布缓存
│   ├── 01_github_code_search_bins.py   # 多后缀代码搜索（网络）
│   ├── 02_github_merge_candidates.py   # 合并候选仓库（本地）
│   ├── 03_github_fetch_trees.py        # 拉取文件树（网络，仅缓存原始响应）
│   ├── 04_filter_kicad_projects.py     # 本地初筛（本地）
│   ├── 05_release_github_trees.py      # GitHub Release A 的 CSV / JSONL 导出（本地）
│   ├── 06_github_fetch_repo_stats.py   # 仓库活跃度统计，GraphQL（网络）
│   ├── 07_github_fetch_repo_history.py # 每仓库的 commit / PR / PR 文件 / issue（网络）
│   ├── 08_build_improvement_events.py  # 从历史缓存构建改进事件（本地）
│   ├── 09_github_fetch_commit_files.py # 工程目录内每个 commit 的改动文件（网络）
│   └── 10_analyze_event_quality.py     # 事件质量分层、限额、3D 普查、报告（本地）
└── data/             # gitignore；缓存只留本地，发布走 ModelScope
    ├── cache/<stage>/state.sqlite    # 各阶段断点缓存，上游库对下游只读
    └── releases/                     # 打包好的发布与 restore 的下载目录
```

打包上传缓存的脚本不在本仓库内。

## TODO

### 搜索策略

- [x] 搜索含 `kicad_pro` 后缀的仓库（39902 个）
- [x] 搜索含 `kicad_pcb` 后缀的仓库（26676 个）
- [x] 搜索含 `kicad_sch` 后缀的仓库（37202 个）
- [x] 搜索含 `sch` 后缀的仓库（30515 个；合并后候选 69181 个，其中 50796 个含 KiCad 6+ 文件）

### 拉取

- [x] 初版候选集的文件树（39902 仓库，21 个截断忽略）
- [x] GraphQL 仓库活跃度统计（39895 取到，7 个不存在）
- [x] 全部候选仓库的 commit、PR、PR 文件、issue（32.1 万次请求，96.4 万 commit，14 万 PR，345 万 PR 文件，23.8 万 issue）
- [x] 工程目录内每个 commit 的改动文件（423293 个 commit，887 万文件）
- [ ] 对其余三个后缀新增的 29279 个仓库跑 03 到 09（文件树、统计、历史、commit 文件），然后重建 08

### 后处理

- [x] 初筛：有完整工程的仓库（同目录 kicad_pro + kicad_sch + kicad_pcb，附近有 README）：32029 仓库，66793 工程目录
- [x] 改进事件表（381008 条：PR 33844，commit 347164，commit 须改了该目录的 KiCad 文件，全部带文件明细与按后缀计数）
- [x] 事件质量过滤（阶段 10，见 `reports/event_quality.md`）
  - [x] 文本：剔除模板、TODO 列表、纯 issue 编号的 PR / commit 正文
  - [x] 规模：KiCad 改动行数与改动文件数上限
  - [x] 只保留已有文件：KiCad 文件全为 `modified`
  - [x] 只存盘不改电路：基于 patch 的 churn 启发式（uuid / tstamp / version 行）；完整网表比较仍待做
  - [x] 仓库限额
  - [ ] 在前后 sha 的完整文件上比较网表
- [ ] 用 KiCad CLI 验证候选事件：改动前后跑 DRC / ERC，错误数不增加的保留
- [ ] 人工审阅 A 级种子集（`reports/seed_issue_driven_prs.csv`）
- [x] 合格仓库的 3D 模型普查（表 `repo_3d_models`，数字见报告）

### 发布

- [x] GitHub Release A（`release-A`：仓库、文件树、合格仓库）
- [x] ModelScope Release B（2026-09-04）：每个阶段缓存打成 `cache/<stage>/state.sqlite.zst`，压后 8.2 GB / 解压 95 GB，`00_restore_cache.py` 恢复已验证
- [ ] ModelScope Release C：新增 29279 仓库拉完后发布，同布局，只替换变化的库
- [ ] 数据字典（`DATA.md`）：每张表每列、来源接口、已知限制（384 KB 搜索上限、单查询 1000 条上限、分页第 100 页封顶、截断的树、merge_commit_sha 为空）

### 重构

- [x] 拆分脚本，实现 API 缓存与业务逻辑解耦（各阶段独立 stage DB）
- [x] 脚本重编号 00（恢复）到 09；无 CLI，参数为脚本顶部常量
