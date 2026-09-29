# -*- coding: utf-8 -*-
"""
岗位维度知识图谱抽取（03_人才需求与岗位 全量版，三表设计）。

三张表（每张表 = 图谱一个维度）：
  表1 岗位基本信息  职业标准 · 职业概况（名称/编码/定义/等级/方向/学历/培训/申报条件），每标准 1 行
  表2 岗位-职业能力  职业标准 · 工作要求（岗位×职业方向×技术等级×工作内容），原子单位=工作内容条目；
                   按"职业方向/技术等级"切片并发抽取后合并，规避 8192 token 输出截断
  表3 岗位-人才需求  人才需求/薪酬报告 · 需求侧（薪酬/口径/满意度/来源专业/需求趋势），一行=一个岗位画像

文档自动分类（按内容，不按目录）：
  含"职业编码"+"工作要求"章节  -> 职业标准（表1+表2）
  其余                          -> 需求侧报告（表3）
  无文本层（扫描件）            -> 跳过并记入汇总

流程：本地解析（分类/章节定位/能力切片）-> 阶段A并发 LLM（表1+表3）
  -> 阶段B并发 LLM（表2 各切片，空结果切片自动重试一次）
  -> Markdown 表格解析 -> 实体键去重 -> 行校验 -> 页码定位（page_locator
     四级策略，未定位行丢弃）-> 保存 xlsx -> 写抽取汇总.json。

职业标准"工作要求"章节两种目录结构（自动识别）：
  A 型（如人工智能工程技术人员）：3.x 职业方向 -> 3.x.y 技术等级 -> 功能表，切片=职业方向；
  B 型（如云计算/招聘师等）：3.x 技术等级 -> 功能表，切片=技术等级；
  招聘师(2026版)等级为 五级/初级工…一级/高级技师 五级体系，表格列名为"技能要求"。

运行:
    python gangwei_extract.py                       # 默认跑 pdfs/03_人才需求与岗位 全部 PDF
    python gangwei_extract.py --dir "pdfs/03_人才需求与岗位" --out know_graph_jobs
    python gangwei_extract.py --pdf "xxx.pdf"       # 单文件
    python gangwei_extract.py --dir ... --dry-run   # 只打印分类与切片规划，不发 LLM
"""
import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font
from tqdm import tqdm

import llm_api
import page_locator

# ============================================================
# 配置
# ============================================================
INFO_DIM_NAME = "岗位基本信息"
ABILITY_DIM_NAME = "岗位-职业能力"
DEMAND_DIM_NAME = "岗位-人才需求"

INFO_COLUMNS = [
    "岗位名称", "职业编码", "职业定义", "专业技术等级", "职业方向",
    "职业环境条件", "职业能力特征", "普通受教育程度",
    "培训学时要求", "申报条件摘要", "原文支撑内容",
]

ABILITY_COLUMNS = [
    "岗位名称", "职业方向", "技术等级", "职业功能", "工作内容",
    "专业能力要求", "相关知识要求", "原文支撑内容",
]

DEMAND_COLUMNS = [
    "岗位名称", "岗位类别", "所属行业领域", "平均月收入", "薪酬统计口径",
    "就业满意度", "专业相关度", "主要来源专业", "需求趋势及驱动因素", "原文支撑内容",
]

# 实体键（用于去重防复读）
INFO_KEY_COLS = (0,)               # 岗位名称
ABILITY_KEY_COLS = (0, 1, 2, 4)    # 岗位+方向+等级+工作内容
DEMAND_KEY_COLS = (0, 1)           # 岗位名称+岗位类别

SAMPLING_PARAMS = {
    "max_tokens": 8192,
    "temperature": 0.1,
    "top_p": 0.9,
}

MAX_INPUT_CHARS = 50000    # 截断过长的输入文本
MAX_SLICE_CHARS = 9000     # 能力表单片输入上限，超过再按等级细分
MAX_ABILITY_ROWS = 300     # 能力表行数上限（防复读退化）
MAX_DEMAND_ROWS = 60       # 人才需求表行数上限

DEFAULT_OUT_DIR = Path("./know_graph_jobs")
DEFAULT_INPUT_DIR = Path("./pdfs/03_人才需求与岗位")
LLM_WORKERS = 8            # LLM 并发数

VERBOSE_PER_FILE = True

# ---- 章节与结构正则 ----
# 工作要求章节标题（"3 工作要求"/"3. 工作要求"/"3.  工作要求"），取最后一次出现（避开目录页）
CH_REQ_RE = re.compile(r"^\s*3\s*[\.．、\s]*工作要求", re.M)
# 章节结束（"4 比重表"/"4.权重表"）
CH_END_RE = re.compile(r"^\s*4\s*[\.．、\s]*(比重|权重)表", re.M)
# 等级标题：A 型 "3.1.1 初级"；B 型 "3.1 初级"/"3. 1 五级/初级工"（标题行以等级词结尾）
_LEVEL_TOKEN = (
    r"(一级\s*/\s*高级技师|二级\s*/\s*技师|三级\s*/\s*高级工|四级\s*/\s*中级工|五级\s*/\s*初级工"
    r"|高级技师|初级工|中级工|高级工|技师|初级|中级|高级)"
)
LEVEL_HEAD_RE = re.compile(
    r"^\s*3\s*[\.．]\s*(\d+)(?:\s*[\.．]\s*(\d+))?\s*" + _LEVEL_TOKEN + r"\s*$", re.M)
# A 型方向标题行："3.2 人工智能平台产品实现"（行首 3.x + 空格 + 名称，非等级词开头）
DIR_HEAD_RE = re.compile(r"^\s*3\s*[\.．]\s*(\d+)\s+(\S[^\n]{1,30})$", re.M)
# 岗位名称：封面"职业编码：X-X-X-X"后紧跟的名称行/词
JOB_NAME_RE = re.compile(r"职业编码[：:]\s*[\d\-]+\s*\n?[ \t]*([^\n\r\d（(]{2,25})")
# 等级归一化（顺序敏感：高级技师 > 技师 > 高级工/高级 > 中级工/中级 > 初级工/初级）
_LEVEL_NORMALIZE = [
    ("一级/高级技师", "高级技师"), ("高级技师", "高级技师"),
    ("二级/技师", "技师"), ("技师", "技师"),
    ("三级/高级工", "高级"), ("高级工", "高级"), ("高级", "高级"),
    ("四级/中级工", "中级"), ("中级工", "中级"), ("中级", "中级"),
    ("五级/初级工", "初级"), ("初级工", "初级"), ("初级", "初级"),
]


# ============================================================
# 1. 文档读取与本地结构解析
# ============================================================
def load_pages_from_pdf(pdf_path):
    """PDF 逐页提取文本，返回 list[str]。"""
    import pymupdf
    pages = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pages.append(page.get_text("text") or "")
    return pages


def load_pages(source):
    """source 为 PDF 或 page_json 路径，返回 (pages, stem)。"""
    source = Path(source)
    if source.suffix.lower() == ".pdf":
        return load_pages_from_pdf(source), source.stem
    with open(source, "r", encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("result", data) if isinstance(data, dict) else data
    if not isinstance(pages, list) or not pages:
        raise ValueError(f"{source} 无有效页面")
    return [p if isinstance(p, str) else "" for p in pages], source.stem


def join_pages(pages, max_chars=MAX_INPUT_CHARS):
    text = "\n\n".join(p for p in pages if isinstance(p, str))
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[文本过长，已截断]"
    return text


def extract_job_name(text):
    """从职业标准封面确定性提取岗位名称（职业编码后的名称行）。失败返回 ""。"""
    m = JOB_NAME_RE.search(text[:3000])
    if not m:
        return ""
    name = m.group(1).strip()
    # 剔除"制定/颁布"类噪声词
    for bad in ("制定", "颁布", "试行", "年版"):
        if bad in name:
            name = name.split(bad)[0].strip()
    return name if len(name) >= 2 else ""


def extract_chapter(text):
    """提取"工作要求"章节文本（到比重表为止）。取标题最后一次出现以避开目录页；
    未找到返回 None。"""
    starts = list(CH_REQ_RE.finditer(text))
    if not starts:
        return None
    start = starts[-1].start()
    ends = [m.start() for m in CH_END_RE.finditer(text) if m.start() > start]
    end = ends[0] if ends else len(text)
    return text[start:end]


def normalize_level(v):
    """技术等级归一化：五级体系/工种类等级映射为 初级/中级/高级/技师/高级技师。"""
    s = re.sub(r"\s+", "", str(v or ""))
    for pat, out in _LEVEL_NORMALIZE:
        if pat in s:
            return out
    return s[:10] if s else "未分级"


def parse_directions(info_row):
    """从表1数据行解析职业方向有序列表（"；"分隔）。"""
    if not info_row or len(info_row) <= 4:
        return []
    raw = str(info_row[4] or "")
    parts = re.split(r"[；;、]", raw)
    out = []
    for p in parts:
        p = p.strip()
        if p and p not in ("无", "未知", "通用", "未划分"):
            out.append(p)
    return out


def scan_direction_names(chapter):
    """兜底：从章节"3.x 方向名"标题行提取方向名（取每个 x 最长的一次）。"""
    names = {}
    for m in DIR_HEAD_RE.finditer(chapter):
        x = int(m.group(1))
        title = m.group(2).strip()
        if title in ("初级", "中级", "高级"):
            continue
        if x not in names or len(title) > len(names[x]):
            names[x] = title
    return [names[x] for x in sorted(names)]


def compute_ability_slices(chapter, direction_names):
    """把工作要求章节切成若干抽取切片。

    A 型（存在 3.x.y 两级编号）：按职业方向 x 分组，一组一片（含该方向全部等级）；
        组内文本超过 MAX_SLICE_CHARS 时再按等级细分。
    B 型（仅 3.x 一级编号=等级）：每个等级标题一片。
    方向名优先取表1"职业方向"列（与图谱口径一致），不足时用文档标题扫描，再不足用"方向x"。
    返回 [{"direction", "level", "text"}]，level 为 None 表示由 LLM 从片段内
    等级小节标题（3.x.y 初级/中级/高级）自行判断。
    """
    matches = [(m.start(), int(m.group(1)),
                int(m.group(2)) if m.group(2) else None,
                m.group(3)) for m in LEVEL_HEAD_RE.finditer(chapter)]
    if not matches:
        return [{"direction": "通用", "level": None, "text": chapter}]

    def _direction_of(x):
        if 0 <= x - 1 < len(direction_names):
            return direction_names[x - 1]
        return f"方向{x}"

    def _extend_start(pos, x):
        """向前并入"3.x 方向名"标题行（A 型：方向标题在首个等级标题之前）。"""
        window = chapter[max(0, pos - 400):pos]
        pat = re.compile(rf"^\s*3\s*[\.．]\s*{x}\s+\S.{{0,40}}$", re.M)
        starts = [m.start() for m in pat.finditer(window)]
        if starts:
            return pos - (len(window) - starts[-1])
        return pos

    slices = []
    has_sub = any(y is not None for _, _, y, _ in matches)
    if has_sub:  # A 型：按方向分组
        groups = {}
        for pos, x, y, level in matches:
            groups.setdefault(x, []).append((pos, y, level))
        xs = sorted(groups)
        for gi, x in enumerate(xs):
            group = groups[x]
            g_start = _extend_start(group[0][0], x)
            g_end = groups[xs[gi + 1]][0][0] if gi + 1 < len(xs) else len(chapter)
            g_text = chapter[g_start:g_end]
            direction = _direction_of(x)
            if len(g_text) <= MAX_SLICE_CHARS or len(group) <= 1:
                slices.append({"direction": direction, "level": None, "text": g_text})
            else:  # 组内按等级细分
                bounds = [p for p, _, _ in group] + [g_end]
                for (p, y, level), nxt in zip(group, bounds[1:]):
                    slices.append({"direction": direction,
                                   "level": normalize_level(level),
                                   "text": chapter[p:nxt]})
    else:  # B 型：每个等级标题一片
        bounds = [p for p, _, _, _ in matches] + [len(chapter)]
        for (p, x, y, level), nxt in zip(matches, bounds[1:]):
            slices.append({"direction": "通用", "level": normalize_level(level),
                           "text": chapter[p:nxt]})
    return slices


# ============================================================
# 2. LLM 提示词构建
# ============================================================
def build_info_messages(text):
    columns_str = " | ".join(INFO_COLUMNS)
    prompt = f"""你是一个知识图谱抽取助手。请从以下《国家职业技术技能标准》文本中抽取"岗位基本信息"（主要来自第1章 职业概况），只输出 1 行数据。

目标知识图谱格式（{len(INFO_COLUMNS)}列）：
| {columns_str} |

字段说明：
- 岗位名称：标准正文中的职业名称（如"人工智能工程技术人员"）
- 职业编码：如 2-02-10-09
- 职业定义：原文的职业定义整句
- 专业技术等级：如"初级、中级、高级"（按原文顺序，用"、"连接）；五级体系标准按原文列举（如"五级/初级工、四级/中级工……"）
- 职业方向：职业划分的全部方向，按原文顺序用"；"连接；标准无方向划分时填"无"
- 职业环境条件：如"室内，常温"
- 职业能力特征：原文概括（如"具有一定的学习、分析、推理和判断能力……"）
- 普通受教育程度：如"大学专科学历（或高等职业学校毕业）"
- 培训学时要求：按等级概括，如"初级64标准学时；中级80标准学时；高级80标准学时"
- 申报条件摘要：按等级概括申报条件要点，每等级一条，用"；"连接，整体 200 字内
- 原文支撑内容：从"职业定义"中逐字摘录一段连续片段（15~60字，须含职业名称或核心职责词），用于页码定位，禁止改写

抽取要求：
1. 严禁编造原文没有的信息；文本未提及的字段填"未知"
2. 仅输出Markdown表格（1 行数据），不要附带任何说明文字

标准文本：
{text}"""

    messages = [
        {"role": "system",
         "content": "你是从国家职业技术技能标准中抽取岗位职业概况信息的知识图谱助手。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": prompt + " /no_think"},
    ]
    return messages


def build_ability_messages(slice_text, job_name, direction, level):
    columns_str = " | ".join(ABILITY_COLUMNS)
    scope = f"职业方向='{direction}'"
    if level:
        scope += f"，技术等级='{level}'"
    level_rule = (f'统一填"{level}"' if level
                  else "从片段内的等级小节标题（如 3.1.1 初级 / 3.2.3 高级）判断，填初级/中级/高级")
    prompt = f"""你是一个知识图谱抽取助手。请从以下《国家职业技术技能标准》"工作要求"章节的一个片段中抽取"岗位-职业能力"信息。本片段范围：{scope}。

目标知识图谱格式（{len(ABILITY_COLUMNS)}列）：
| {columns_str} |

背景：工作要求章节的表格列为[职业功能|工作内容|专业能力要求(部分标准该列名为"技能要求")|相关知识要求]，转文本后表格结构丢失，内容按"功能编号+名称、工作内容编号+名称、若干'能…'条目、若干知识条目"的顺序平铺；分页处可能出现"续表"及重复的表头/小节标题，直接忽略。

字段说明：
- 岗位名称：统一填"{job_name}"
- 职业方向：统一填"{direction}"
- 技术等级：{level_rule}
- 职业功能：编号+名称，如"1 人工智能共性技术应用"（编号数字可省略，名称与原文一致）
- 工作内容：功能下的工作内容条目，如"1.1 人工智能算法选型及调优"（编号可省略，名称与原文一致）
- 专业能力要求：该工作内容下全部"能…"条目合并，用"；"连接（可去掉 1.1.1 类编号）
- 相关知识要求：该工作内容下全部知识条目合并，用"；"连接
- 原文支撑内容：从该工作内容的专业能力要求中逐字摘录一条连续片段（15~60字，含"能"字），用于页码定位，禁止改写、概括或拼接

抽取要求：
1. ⚠️ 一行 = 片段内一个工作内容条目，覆盖片段内全部工作内容，不重不漏；片段范围之外的内容（其他方向/等级）一律忽略
2. 严禁编造原文没有的内容；"职业功能/工作内容"名称与原文一致
3. 仅输出Markdown表格，不要附带任何说明文字；片段内没有工作内容条目时只输出表头行

标准文本片段：
{slice_text}"""

    messages = [
        {"role": "system",
         "content": "你是从国家职业技术技能标准的工作要求章节中抽取岗位职业能力信息的知识图谱助手。一行=一个工作内容条目，能力/知识多条目用'；'合并。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": prompt + " /no_think"},
    ]
    return messages


def build_demand_messages(text):
    columns_str = " | ".join(DEMAND_COLUMNS)
    prompt = f"""你是一个知识图谱抽取助手。请从以下人才需求/薪酬类报告文本中抽取"岗位-人才需求"信息，一行 = 报告中一个岗位/职业/专业的人才需求画像条目。

目标知识图谱格式（{len(DEMAND_COLUMNS)}列）：
| {columns_str} |

字段说明：
- 岗位名称：报告中的具体岗位/职业/专业名称（如"电气工程师"、"电子信息类专业"、"信息传输、软件和信息技术服务业就业人员"）
- 岗位类别：岗位/职业/本科专业/高职专业/行业类别 之一（按报告口径）
- 所属行业领域：如"新能源"、"工业互联网"、"云计算"；未提及填"未知"
- 平均月收入：保留原文数值与单位，如"7699元"、"12000元/月"；原文为年薪等口径时保留原文数值并注明（如"年薪25万元"）；未提及填"未知"
- 薪酬统计口径：统计人群+时间，如"2025届本科毕业生毕业半年后"、"2026年第一季度招聘薪酬"；未提及填"未知"
- 就业满意度：如"81%"；未提及填"未知"
- 专业相关度：如"76%"；未提及填"未知"
- 主要来源专业：报告指出的与该岗位/专业强相关的专业，多个用"；"连接；未提及填"未知"
- 需求趋势及驱动因素：原文对该岗位需求量/增长/缺口/竞争的描述，60字内概括；未提及填"未知"
- 原文支撑内容：从原文逐字摘录一段连续片段（15~60字，含岗位名称或收入数字），用于页码定位，禁止改写

抽取要求：
1. 覆盖报告中全部岗位/专业/行业的需求与薪酬条目，不重不漏；行数以报告实际条目数为准
2. 严禁编造原文没有的数据；未提及字段一律填"未知"
3. 仅输出Markdown表格，不要附带任何说明文字

报告文本：
{text}"""

    messages = [
        {"role": "system",
         "content": "你是从人才需求与薪酬报告中抽取岗位需求画像信息的知识图谱助手。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": prompt + " /no_think"},
    ]
    return messages


# ============================================================
# 3. Markdown 表格解析与校验
# ============================================================
def markdown_table_to_rows(md_text, columns):
    """解析 LLM 输出的 Markdown 表格（首行固定替换为表头）。"""
    lines = str(md_text).strip().split("\n")
    rows, dropped = [], 0
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if cells and all(re.match(r"^[-:]+$", c) for c in cells if c):
            continue
        if not cells:
            continue
        if len(cells) > len(columns):
            dropped += 1
            continue
        cells = ["" if c.lower() in ("nan", "null", "none", "无") else c for c in cells]
        cells += ["未知"] * (len(columns) - len(cells))
        rows.append(cells)
    if dropped and VERBOSE_PER_FILE:
        print(f"    [解析] 丢弃 {dropped} 个超宽退化行")
    if rows and rows[0][:len(columns)] == columns[:len(rows[0])]:
        rows = rows[1:]
    rows.insert(0, list(columns))
    return rows


def clean_cell(v):
    """去单元格内的换行与首尾空白。"""
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _pad(row, ncols):
    row = list(row)[:ncols]
    return row + ["未知"] * (ncols - len(row))


def validate_info_rows(rows):
    """基本信息表行校验：岗位名称为空的残行丢弃。"""
    if len(rows) <= 1:
        return rows
    out = [rows[0]]
    for r in rows[1:]:
        r = _pad(r, len(INFO_COLUMNS))
        if not r[0]:
            if VERBOSE_PER_FILE:
                print("    [校验] 丢弃岗位名称为空的残行")
            continue
        out.append([clean_cell(c) for c in r])
    return out


def validate_demand_rows(rows):
    """人才需求表行校验：岗位名称为空丢弃，行数封顶。"""
    if len(rows) <= 1:
        return rows
    out = [rows[0]]
    for r in rows[1:]:
        r = _pad(r, len(DEMAND_COLUMNS))
        if not r[0]:
            continue
        out.append([clean_cell(c) for c in r])
    if len(out) - 1 > MAX_DEMAND_ROWS:
        out = out[: MAX_DEMAND_ROWS + 1]
        if VERBOSE_PER_FILE:
            print(f"    [校验] 行数超过上限 {MAX_DEMAND_ROWS}，截断")
    return out


def parse_ability_slice(md_text, job_name, direction, level):
    """解析单切片输出 -> 数据行列表（岗位/方向/等级强制回填，保证跨切片一致）。"""
    rows = markdown_table_to_rows(md_text, ABILITY_COLUMNS)
    out = []
    for r in rows[1:]:
        r = _pad(r, len(ABILITY_COLUMNS))
        if not r[4]:  # 工作内容为空的残行
            continue
        r = [clean_cell(c) for c in r]
        r[0] = job_name
        r[1] = direction
        r[2] = level if level else normalize_level(r[2])
        if (not r[5] or r[5] == "未知") and (not r[6] or r[6] == "未知"):
            continue  # 能力/知识双空 = 退化行
        out.append(r)
    return out


# ============================================================
# 4. LLM 批量并发（单任务失败不中断）
# ============================================================
def run_llm_batch(tasks, workers=LLM_WORKERS, desc="LLM 抽取"):
    """tasks: [{"key", "messages"}, ...] -> {key: text 或 None}。"""
    results = {}
    if not tasks:
        return results
    s_time = time.time()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(llm_api.chat, t["messages"], **SAMPLING_PARAMS): t["key"]
            for t in tasks
        }
        with tqdm(total=len(futures), desc=desc, ncols=100) as pbar:
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    print(f"  [LLM失败] {key}: {e}")
                    results[key] = None
                pbar.update(1)
    ok = sum(1 for v in results.values() if v)
    print(f"{desc}: {ok}/{len(tasks)} 成功, 耗时 {time.time() - s_time:.1f}s")
    return results


# ============================================================
# 5. 页码定位 + 写入 xlsx
# ============================================================
def locate_rows(rows, pages, quote_idx):
    """页码定位：未定位行丢弃（与五维度口径一致）。"""
    if not pages:
        return rows, {}
    locator = page_locator.PageLocator(pages)
    return page_locator.attach_page_column(rows, locator, quote_idx)


def save_to_xlsx(rows, output_path, sheet_title):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row_idx, row in enumerate(rows, start=1):
        for col_idx, value in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if row_idx == 1:
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal="center", vertical="center")
    ncols = max(len(r) for r in rows) if rows else 0
    for col_idx in range(1, ncols + 1):
        max_len = 0
        for r in rows:
            v = str(r[col_idx - 1]) if len(r) >= col_idx else ""
            for piece in v.split("；"):
                max_len = max(max_len, len(piece))
        letter = openpyxl.utils.get_column_letter(col_idx)
        ws.column_dimensions[letter].width = min(max_len + 4, 60)
    wb.save(output_path)


# ============================================================
# 6. 文档收集与分类
# ============================================================
def collect_sources(paths):
    """paths: PDF/page_json 路径列表 -> 源信息列表（含分类）。"""
    sources = []
    for p in paths:
        p = Path(p)
        entry = {"path": p, "stem": p.stem, "tables": {}, "errors": []}
        try:
            pages, stem = load_pages(p)
            entry["stem"] = stem
            entry["pages"] = pages
            text = "\n".join(pages)
            entry["text"] = text
        except Exception as e:
            entry["type"] = "error"
            entry["errors"].append(f"读取失败: {e}")
            sources.append(entry)
            continue
        if len(re.sub(r"\s+", "", text)) < 100:
            entry["type"] = "no_text"  # 扫描件无文本层
        elif "职业编码" in text and CH_REQ_RE.search(text):
            entry["type"] = "standard"
            entry["chapter"] = extract_chapter(text)
            entry["job_name"] = extract_job_name(text)
        else:
            entry["type"] = "demand"
        sources.append(entry)
    return sources


# ============================================================
# 7. 三表落表
# ============================================================
def finalize_info(source, md_text, out_dir):
    """表1：解析->校验->去重->定位->保存。返回 (行数, 页码统计, 数据行)。"""
    rows = markdown_table_to_rows(md_text, INFO_COLUMNS)
    rows = validate_info_rows(rows)
    rows, _ = page_locator.dedup_rows(rows, INFO_KEY_COLS, len(INFO_COLUMNS) - 1)
    rows, pg = locate_rows(rows, source["pages"], len(INFO_COLUMNS) - 1)
    save_to_xlsx(rows, out_dir / f"{INFO_DIM_NAME}.xlsx", INFO_DIM_NAME)
    info_row = rows[1] if len(rows) > 1 else None
    return len(rows) - 1, pg, info_row


def finalize_demand(source, md_text, out_dir):
    """表3：解析->校验->去重->定位->保存。返回 (行数, 页码统计)。"""
    rows = markdown_table_to_rows(md_text, DEMAND_COLUMNS)
    rows = validate_demand_rows(rows)
    rows, _ = page_locator.dedup_rows(rows, DEMAND_KEY_COLS, len(DEMAND_COLUMNS) - 1)
    rows, pg = locate_rows(rows, source["pages"], len(DEMAND_COLUMNS) - 1)
    save_to_xlsx(rows, out_dir / f"{DEMAND_DIM_NAME}.xlsx", DEMAND_DIM_NAME)
    return len(rows) - 1, pg


def finalize_ability(source, slice_data_rows, out_dir):
    """表2：合并各切片行->去重->封顶->定位->保存。返回 (行数, 页码统计, 去重丢弃数)。"""
    merged = [list(ABILITY_COLUMNS)]
    for rows in slice_data_rows:
        merged.extend(rows)
    merged, dup = page_locator.dedup_rows(merged, ABILITY_KEY_COLS, len(ABILITY_COLUMNS) - 1)
    if len(merged) - 1 > MAX_ABILITY_ROWS:
        merged = merged[: MAX_ABILITY_ROWS + 1]
    merged, pg = locate_rows(merged, source["pages"], len(ABILITY_COLUMNS) - 1)
    save_to_xlsx(rows=merged, output_path=out_dir / f"{ABILITY_DIM_NAME}.xlsx",
                 sheet_title=ABILITY_DIM_NAME)
    return len(merged) - 1, pg, dup


# ============================================================
# 8. 主流程
# ============================================================
def _source_out_dir(source, out_dir):
    d = Path(out_dir) / source["stem"].rstrip(" .")
    d.mkdir(parents=True, exist_ok=True)
    return d


def dry_run(sources):
    """打印分类与切片规划，不发 LLM。"""
    print("=" * 70)
    print("DRY RUN：文档分类与能力切片规划")
    print("=" * 70)
    for s in sources:
        print(f"\n[{s['type']}] {s['stem']}")
        if s["type"] == "no_text":
            print("  扫描件无文本层，将跳过")
            continue
        if s["type"] == "standard":
            ch = s.get("chapter")
            print(f"  岗位名称(本地): {s.get('job_name') or '(未识别)'}")
            if ch is None:
                print("  工作要求章节未定位 -> 整文单片")
                slices = [{"direction": "通用", "level": None, "text": s["text"]}]
            else:
                slices = compute_ability_slices(ch, [])
            print(f"  工作要求章节: {len(ch) if ch else 0} 字, 切片 {len(slices)} 个")
            for i, sl in enumerate(slices):
                preview = re.sub(r"\s+", " ", sl["text"][:60])
                print(f"    #{i} 方向={sl['direction']} 等级={sl['level']} "
                      f"{len(sl['text'])}字 | {preview}")
        else:
            print(f"  需求侧报告: {len(s['text'])} 字 -> 表3 单次抽取")


def main(argv=None):
    parser = argparse.ArgumentParser(description="岗位维度知识图谱抽取（三表，03文件夹全量）")
    parser.add_argument("--dir", default=None, help="PDF 目录（递归扫描）")
    parser.add_argument("--pdf", default=None, help="单个 PDF 文件路径")
    parser.add_argument("--page-json", default=None, help="page_json 文件路径（单文件）")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="输出目录")
    parser.add_argument("--workers", type=int, default=LLM_WORKERS, help="LLM 并发数")
    parser.add_argument("--dry-run", action="store_true", help="只打印规划，不发 LLM")
    args = parser.parse_args(argv)

    if args.pdf or args.page_json:
        paths = [args.pdf or args.page_json]
    else:
        input_dir = Path(args.dir or DEFAULT_INPUT_DIR)
        paths = sorted(input_dir.rglob("*.pdf"))
    if not paths:
        parser.error("未找到待处理文件")

    sources = collect_sources(paths)
    standards = [s for s in sources if s["type"] == "standard"]
    demands = [s for s in sources if s["type"] == "demand"]
    skipped = [s for s in sources if s["type"] in ("no_text", "error")]
    print(f"共 {len(sources)} 个文档: 职业标准 {len(standards)} + 需求报告 {len(demands)}"
          + (f" + 跳过 {len(skipped)}" if skipped else ""))
    for s in skipped:
        print(f"  [跳过] {s['stem']}: {s['errors'][0] if s['errors'] else '扫描件无文本层'}")

    if args.dry_run:
        dry_run(sources)
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t_all = time.time()

    # ---------------- 阶段A：表1（标准）+ 表3（需求报告） ----------------
    phase_a = []
    for s in standards:
        phase_a.append({"key": (s["stem"], "info"),
                        "messages": build_info_messages(join_pages(s["pages"]))})
    for s in demands:
        phase_a.append({"key": (s["stem"], "demand"),
                        "messages": build_demand_messages(join_pages(s["pages"]))})
    res_a = run_llm_batch(phase_a, args.workers, "阶段A 基本信息+人才需求")

    for s in standards:
        out = _source_out_dir(s, out_dir)
        md = res_a.get((s["stem"], "info"))
        if not md:
            s["errors"].append("表1 LLM 调用失败")
            s["info_row"] = None
            continue
        try:
            n, pg, info_row = finalize_info(s, md, out)
            s["tables"][INFO_DIM_NAME] = {"rows": n, "pg": pg}
            s["info_row"] = info_row
            if VERBOSE_PER_FILE:
                print(f"  [{INFO_DIM_NAME}] {s['stem']}: {n} 行, "
                      f"{page_locator.stats_summary(pg)}")
        except Exception as e:
            s["errors"].append(f"表1 落表失败: {e}")
            s["info_row"] = None

    for s in demands:
        out = _source_out_dir(s, out_dir)
        md = res_a.get((s["stem"], "demand"))
        if not md:
            s["errors"].append("表3 LLM 调用失败")
            continue
        try:
            n, pg = finalize_demand(s, md, out)
            s["tables"][DEMAND_DIM_NAME] = {"rows": n, "pg": pg}
            if VERBOSE_PER_FILE:
                print(f"  [{DEMAND_DIM_NAME}] {s['stem']}: {n} 行, "
                      f"{page_locator.stats_summary(pg)}")
        except Exception as e:
            s["errors"].append(f"表3 落表失败: {e}")

    # ---------------- 阶段B：表2 按方向/等级切片并发 ----------------
    phase_b = []
    for s in standards:
        chapter = s.get("chapter")
        if chapter is None:
            chapter = s["text"]
            s["chapter"] = chapter
        directions = parse_directions(s.get("info_row"))
        if not directions:
            directions = scan_direction_names(chapter)
        s["directions"] = directions
        slices = compute_ability_slices(chapter, directions)
        s["slices"] = slices
        job_name = (s.get("job_name")
                    or (s["info_row"][0] if s.get("info_row") else "")
                    or s["stem"])
        s["job_name_final"] = job_name
        for i, sl in enumerate(slices):
            phase_b.append({
                "key": (s["stem"], "ability", i),
                "messages": build_ability_messages(sl["text"], job_name,
                                                   sl["direction"], sl["level"]),
            })
    print(f"\n阶段B：能力切片共 {len(phase_b)} 个 "
          f"({len(standards)} 个标准，方向名来源：表1/文档标题扫描)")
    res_b = run_llm_batch(phase_b, args.workers, "阶段B 职业能力切片")

    # 空结果切片重试一次
    retry_tasks = []
    for s in standards:
        for i, sl in enumerate(s["slices"]):
            md = res_b.get((s["stem"], "ability", i))
            if not md:
                retry_tasks.append({"key": (s["stem"], "ability", i),
                                    "messages": build_ability_messages(
                                        sl["text"], s["job_name_final"],
                                        sl["direction"], sl["level"])})
    if retry_tasks:
        print(f"重试 {len(retry_tasks)} 个失败/空切片")
        res_retry = run_llm_batch(retry_tasks, args.workers, "阶段B 重试")
        res_b.update({k: v for k, v in res_retry.items() if v})

    # 合并落表
    for s in standards:
        out = _source_out_dir(s, out_dir)
        slice_rows, empty_slices = [], 0
        for i, sl in enumerate(s["slices"]):
            md = res_b.get((s["stem"], "ability", i))
            if not md:
                empty_slices += 1
                continue
            try:
                rows = parse_ability_slice(md, s["job_name_final"],
                                           sl["direction"], sl["level"])
                slice_rows.append(rows)
            except Exception as e:
                empty_slices += 1
                s["errors"].append(f"表2 切片#{i} 解析失败: {e}")
        try:
            n, pg, dup = finalize_ability(s, slice_rows, out)
            s["tables"][ABILITY_DIM_NAME] = {
                "rows": n, "pg": pg, "dedup": dup,
                "slices": len(s["slices"]), "empty_slices": empty_slices,
            }
            if VERBOSE_PER_FILE:
                print(f"  [{ABILITY_DIM_NAME}] {s['stem']}: {n} 行 "
                      f"(切片 {len(s['slices'])}, 去重丢弃 {dup}), "
                      f"{page_locator.stats_summary(pg)}")
        except Exception as e:
            s["errors"].append(f"表2 落表失败: {e}")

    # ---------------- 汇总 ----------------
    elapsed = time.time() - t_all
    summary = {
        "总文档数": len(sources),
        "职业标准": len(standards),
        "需求报告": len(demands),
        "跳过": [{"文件": s["stem"], "原因": "扫描件无文本层" if s["type"] == "no_text"
                                           else s["errors"][0]} for s in skipped],
        "耗时秒": round(elapsed, 1),
        "文档": [],
    }
    print("\n" + "=" * 70)
    print(f"抽取汇总（耗时 {elapsed:.1f}s）")
    print("=" * 70)
    for s in sources:
        doc = {"文件": s["stem"], "类型": s["type"], "表": {}, "错误": s["errors"]}
        parts = []
        for name, st in s["tables"].items():
            doc["表"][name] = {"行数": st["rows"]}
            if "pg" in st:
                doc["表"][name]["定位"] = st["pg"]
            parts.append(f"{name} {st['rows']}行")
        if s["type"] == "no_text":
            parts = ["扫描件无文本层，未抽取"]
        if s["errors"]:
            parts.append("错误: " + "; ".join(s["errors"]))
        print(f"  {s['stem']}\n    {' | '.join(parts) if parts else '(无输出)'}")
        summary["文档"].append(doc)

    summary_path = out_dir / "抽取汇总.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n汇总已写入 {summary_path}")
    print(f"输出目录: {out_dir}（每文档一个文件夹，含对应维度 xlsx）")
    return summary


if __name__ == "__main__":
    main()
