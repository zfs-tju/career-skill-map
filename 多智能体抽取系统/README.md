# 多智能体抽取系统（就业岗位技能图谱）

面向就业领域权威文档（国家职业技能标准 / 招聘 JD / 人力资源与薪酬报告 / 专业就业报告）的多智能体知识抽取管线。各类文档自动分类路由，由多个 LLM 抽取智能体按**五维度表契约**并发抽取，产出带"数据来源 + 页号 + 原文支撑内容"三级溯源的结构化 Excel 知识表（`know_graph_employment/`），交付下游就业图谱展示系统（`../图谱案例/`）导入建图与 PDF 页级溯源。

## 系统流程

```
扫描 pdfs/ → 文档分类路由 → 多通道分发 → 五维度抽取 → 实体对齐 → 抽取汇总.json
```

### 文档路由（按内容分类，不按目录）

| 路由结果 | 判定条件 | 去向 |
|---|---|---|
| `jd_csv` | 文件名 = `岗位技能表.csv` | 通道A：CSV 规则直通 + 行业领域 LLM 批量归一 → **岗位聚集** |
| `report_agg` | 试点报告白名单（薪酬数据报告 / 最缺工100个职业排行） | 通道B：LLM 抽取 → **岗位聚集** |
| `standard` | 含"职业编码" + "工作要求"章节（≥300 字） | 通道C：能力切片并发 LLM → **技能结构** + **发展路径** |
| `edu_report` | 培养类报告白名单（麦可思 / 本科起薪 / 就业前景研判 / 新能源就业趋势） | 通道D：LLM 抽取 → **培养供给链** |
| `skip` / `no_text` / `error` | 目录排除（`05_企业报告` 等）/ 后缀不支持 / 非职业标准 / 扫描件无文本层 / 读取失败 | 跳过并记入汇总 |

**岗位竞合**不独立抽取：全部技能结构表落盘后由规则推导产出（不调 LLM）。

## 目录结构

```
多智能体抽取系统/
├── run_employment_pipeline.py   统一入口：扫描 → 分类路由 → 通道分发 → 汇总
├── employment_schema.py         5 表契约（P0 冻结）：模板列序 / 枚举 / 数据来源生成
├── doc_loader.py                文档适配层：pdf/html/txt/md/docx/csv/xls(x) 统一为页面文本
│
├── gangju_extract.py            维度1 岗位聚集：JD CSV 规则直通 + 试点报告 LLM 抽取
├── jineng_extract.py            维度2 技能结构：职业标准"工作要求"章节按能力切片并发抽取
├── fazhan_extract.py            维度3 发展路径：职业标准"申报条件"小节（五级→一级晋升链）
├── peiyang_extract.py           维度4 培养供给链：专业×就业报告 LLM 抽取
├── jinghe_jobs_extract.py       维度5 岗位竞合：技能结构表规则推导（不调 LLM）
├── gangwei_extract.py           共享抽取底座：职业标准解析 / 能力切片 / LLM 批量调用 /
│                                Markdown 表格解析 / 落表（保留独立 CLI：--dir/--pdf/--dry-run）
│
├── llm_api.py                   LLM 调用封装（天大校园端点，并发、重试、限速）
├── page_locator.py              页码定位：exact / span / subseq / fuzzy 四级策略，
│                                定位失败的行直接丢弃（防编造）
├── entity_normalizer.py         实体对齐层：岗位 / 技能 / 城市归一，导出标准名录与技能词表
├── downstream_sim.py            下游联调模拟：按列序读 5 表建图，验收节点连通性
├── kg_validator_employment.py   质检：契约校验 / 空表诊断 / 准确率抽样 / 碎片检查
│
├── know_graph_employment/       输出目录：每文档一个文件夹（5 张维度 xlsx）+
│                                _跨文档推导/ + 抽取汇总.json + 岗位标准名录.json + 技能词表.json
└── pdfs/                        原始文档输入目录（运行时自备，管线默认扫描 ./pdfs）
```

## 五表契约（P0 冻结）

落表列结构 = **[模板业务列 …, 数据来源, 页号, 原文支撑内容]**：

- 列序即契约：下游建图系统**按列位置读列**，任何增删调序都是断约；
- "数据来源"由代码统一生成为 `《文档名》（来源：机构）`，LLM 不参与；
- **硬约束**：每行必须携带"原文支撑内容"（LLM 摘录的原文片段）且能在原文页面文本中定位，否则该行丢弃——这是全系统防幻觉、防编造的根基；
- 字段级允许空（薪资 / 证书原文没有就留空，行不受影响）。

| 维度 | 智能体 | 数据源 | 关键口径 |
|---|---|---|---|
| 岗位聚集 | `gangju_extract` | 招聘 JD CSV / 试点人力资源报告 | CSV 规则直通不走 LLM（仅行业归一）；薪资 / 届别 / 年份正则清洗；未知类占位词归空 |
| 技能结构 | `jineng_extract` | 职业标准 PDF"工作要求"章节 | 按"职业方向 / 技术等级"切片并发抽取后合并，规避 8192 token 输出截断；技能分类限 11 类枚举 |
| 发展路径 | `fazhan_extract` | 职业标准"申报条件"小节 | 五级→一级从业年限即天然晋升链；职级用国家体系枚举归一；"平均年限"只取原文明确年限，"转换难度"由系统按年限推导，**禁止 LLM 估算** |
| 培养供给链 | `peiyang_extract` | 麦可思 / 起薪分布 / 就业前景研判等报告 | 专业名照抄不拆并；学科门类限 14 门类枚举；匹配度限高/中/低且必须有报告明确证据 |
| 岗位竞合 | `jinghe_jobs_extract` | 技能结构表（跨文档推导） | 岗位对匹配技能 ≥6 且重叠率 ≥10% 才产出；技能匹配 = 精确相等 / 包含 / 共享 4 字子串；无序对归并（A-B 与 B-A 同条） |

## 环境依赖

- Python 3.9+
- 第三方库：`pandas`、`openpyxl`、`requests`、`tqdm`；PDF 解析用 `pymupdf`、docx 用 `python-docx`（均在首次使用时懒加载）

```bash
pip install pandas openpyxl requests tqdm pymupdf python-docx
```

## 配置 API Key（合规要求，不入库）

所有模块统一通过 `llm_api.py` 调用天津大学校园 LLM 端点（`ai.tju.edu.cn`，OpenAI 兼容）。按照大赛合规要求，**Key 不写入代码与 Git 仓库**，两种配置方式任选其一：

- 在本目录新建 `api_key.txt`，将 Key 粘贴进去（已被 `.gitignore` 排除，不会被提交）；
- 或设置环境变量 `TUJU_API_KEY`。

未配置 Key 时脚本可做 dry-run 与规则直通（JD CSV / 岗位竞合推导），但 LLM 抽取不可用。

## 运行方式

统一入口 `run_employment_pipeline.py`：

```bash
python run_employment_pipeline.py                     # ./pdfs 全量 → ./know_graph_employment
python run_employment_pipeline.py --pdf "xxx.pdf"     # 单文档
python run_employment_pipeline.py --dir "pdfs/03_人才需求与岗位" --out know_graph_test
python run_employment_pipeline.py --dry-run           # 只打印分类与抽取计划，不调 LLM
python run_employment_pipeline.py --dims agg          # 只跑岗位聚集（jd_csv + report_agg）
python run_employment_pipeline.py --dims skill        # 技能结构 + 发展路径 + 岗位竞合推导
python run_employment_pipeline.py --dims edu          # 培养供给链
python run_employment_pipeline.py --coverage          # 全覆盖模式：每个数据文件 × 各维度逐一尝试
python run_employment_pipeline.py --force             # 忽略已有产出强制重抽（默认断点续跑）
python run_employment_pipeline.py --doc-workers 2 --llm-workers 8   # 文档并发 × LLM 并发
```

特性：

- **断点续跑**：已落盘的 文档×维度 产物自动跳过（`--force` 重抽）；
- **抽取汇总**：`know_graph_employment/抽取汇总.json` 记录每份文档的路由类型、各维度行数与跳过 / 失败原因；
- **`--coverage` 全覆盖**：打破"源类型 → 维度"路由，任何数据文件都过一遍 4 个 LLM 维度（原文确无该维度事实时优雅返回 0 行，引文回源校验兜底）。

## 抽取后处理（管线内自动执行）

1. **岗位竞合推导**：聚合全部技能结构表 → `know_graph_employment/_跨文档推导/岗位竞合.xlsx`；
2. **实体对齐**（`entity_normalizer.py`）：岗位按"技能结构表岗位名 = 权威标准名录"归一（防止把同一岗位拆成孤立节点）、技能词表化、城市去"市"后缀，导出 `岗位标准名录.json` 与 `技能词表.json`；
3. **5 表齐备**：为每份源文档补齐缺失的空维度表，保证下游按文件夹遍历不缺表。

## 质检与验收

```bash
python kg_validator_employment.py --sample 30        # 写 know_graph_employment/质检报告.json
python downstream_sim.py                             # 下游建图联调模拟
```

`kg_validator_employment.py` 四项检查：

1. **契约校验**：每份 xlsx 表头逐列比对契约（列序断约即报错）；
2. **空表诊断**：每源文档目录 × 5 维度的存在性 / 空表清单；
3. **准确率抽样**：随机抽 N 行把"原文支撑内容"回源重定位，报告命中率（命中 = 摘录能在《数据来源》指向的原文页面文本中定位）；
4. **碎片检查**：跨维度共享的岗位名字符串一致性（4 张表引用同一岗位节点，归一失败会拆成孤立节点）。

`downstream_sim.py` 模拟下游建图系统的按位置读表方式，验收节点 / 边规模合理性与共享岗位节点在各维度间的连通性。

## 与下游展示系统的衔接

五表列序契约冻结，下游就业图谱展示系统（`../图谱案例/`，含 ECharts 建图、双智能报告与检索）按列位置读表导入；每行强制携带的"数据来源 + 页号"两列直接支撑下游前端 **PDF 原件溯源跳转**（`#page=页号` 定位到被引用页）。

## 常见问题

- **扫描件 / 无文本层 PDF**：非空白字符 < 100 判为 `no_text` 跳过并记入汇总；
- **LLM 编造行**：`page_locator` 四级定位（exact → span → subseq → fuzzy）均失败时该行直接丢弃，不落表；
- **空表原因**：文档不在白名单 / 非职业标准 / LLM 判定无符合口径的信息 / 全部行定位失败，具体看 `抽取汇总.json` 明细。
