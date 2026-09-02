# pcb_project_scout

[English](README.md) | 中文

从 GitHub 挖掘并筛选 KiCad 开源项目。

贡献者与编码 agent 的开发约定见 [AGENTS.md](AGENTS.md)。

## 运行流程

> 多 GitHub 账号 token 并行可提升速度；同一账号多 token 不增加 API 配额。

1. **多策略搜索** `00_github_code_search_bins.py`。GitHub Search API 无法保证返回全部匹配结果，采用多种搜索策略提升覆盖率。按脚本顶部常量 `SUFFIXES` 逐个后缀搜索，各自独立缓存至 `data/cache/github_code_search_<suffix>/state.sqlite`。加后缀直接改常量。
2. **合并候选仓库** `01_github_merge_candidates.py`。汇总所有策略返回的仓库，按 `repo_id` 去重生成统一候选列表，存入 `data/cache/github_candidates/state.sqlite`。纯本地，可重复执行。
3. **拉取文件树** `02_github_fetch_trees.py`。只读候选库，通过 GitHub Tree API 拉取每个仓库的完整文件列表，原始响应与每仓库状态存入 `data/cache/github_trees/state.sqlite`。不做任何业务判断。
4. **忽略截断仓库** 文件量过大导致 Tree API 返回被截断的仓库（21 / 39902，占比约 0.05%）直接忽略，不进入初筛。这类仓库多为大型杂项集合，逐个下载源码包补全成本过高，收益不值。状态保留为 `truncated`。
5. **本地初筛（后处理）** `03_filter_kicad_projects.py`。只读候选库与文件树库，纯本地运算，无网络请求，每次全量重算，结果存入 `data/cache/filter_kicad_projects/state.sqlite`：`qualified_projects`（每工程目录一行）、`qualified_repos`（每仓库一行汇总）、`repo_filter_status`（每个候选仓库的筛选结论）。
6. **发布** `04_release_github_trees.py`。只读以上三库，导出到 `data/releases/<date>/`：`repos.csv`（全量候选仓库与拉取状态）、`trees.jsonl`（每仓库一行完整文件树，条目保留 path/mode/type/sha/size）、`qualified_repos.csv`（初筛合格仓库清单，以仓库为单位）、`manifest.json`（计数、各库 meta、筛选规则、文件 sha256）。随后整个目录打包为 `data/releases/<date>.zip`，作为 GitHub Release 附件上传。`data/` 下任何内容都不提交。

按编号顺序运行：

```bash
uv run scripts/00_github_code_search_bins.py
uv run scripts/01_github_merge_candidates.py
uv run scripts/02_github_fetch_trees.py
uv run scripts/03_filter_kicad_projects.py
uv run scripts/04_release_github_trees.py
```

每个脚本从自己的 SQLite 缓存断点续跑。缓存完整时整条链重跑不发任何 API 请求。

## 目录结构

```
pcb_project_scout/
├── README.md
├── README.zh-CN.md
├── AGENTS.md         # 贡献者与编码 agent 的开发约定
├── pyproject.toml    # 依赖：uv、httpx、python-dotenv
├── .env.example      # 环境变量：GITHUB_TOKEN_1..N
├── scripts/          # 各阶段自包含脚本，按编号顺序执行，均支持断点续跑或全量重算
│   ├── 00_github_code_search_bins.py # 多策略搜索（网络）
│   ├── 01_github_merge_candidates.py # 合并候选仓库（本地）
│   ├── 02_github_fetch_trees.py      # 拉取文件树（网络，仅缓存原始响应）
│   ├── 03_filter_kicad_projects.py   # 本地初筛（本地）
│   └── 04_release_github_trees.py    # 发布导出（本地）
└── data/             # gitignore；缓存只留本地，发布走 GitHub Releases
    ├── cache/<stage>/state.sqlite    # 各阶段断点缓存，上游库对下游只读
    ├── releases/<date>/              # 发布产物，未打包
    └── releases/<date>.zip           # 同一发布的打包，上传到 GitHub Releases
```

## TODO

### 搜索策略

- [x] 搜索含 `kicad_pro` 后缀的仓库
- [ ] 搜索含 `kicad_pcb` 后缀的仓库
- [ ] 搜索含 `kicad_sch` 后缀的仓库
- [ ] 搜索含 `sch` 后缀的仓库

### 文件树与发布

- [x] 拉取初版候选仓库文件树
- [x] 截断仓库处理：决定忽略（占比极小，补全成本过高）
- [x] 发布初版 Release A（`data/releases/2026-09-02/`，39902 仓库，39898 文件树，1728 万条目，32029 合格仓库 / 66793 工程）

### 后处理

- [x] 初筛工程完整仓库（含 kicad_pro、kicad_sch、kicad_pcb、readme）
- [ ] 筛选含 3D 模型的仓库

### 重构

- [x] 拆分脚本，实现 API 缓存与业务逻辑解耦（各阶段独立 stage DB）
