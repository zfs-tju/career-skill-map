# -*- coding: utf-8 -*-
"""维度1：岗位聚集。

通道A：岗位技能表 CSV 规则直通（行业领域由单次 LLM 批量归一）；
通道B：试点人力资源报告（薪酬调查/最缺工排行）LLM 抽取。
两条通道产出同一 9 列契约（+原文支撑内容），共用落表流水线。
"""
import re
from pathlib import Path

import pandas as pd

import employment_schema as schema
from doc_loader import load_any
from gangwei_extract import (
    LLM_WORKERS, MAX_INPUT_CHARS, clean_cell, join_pages, locate_rows,
    markdown_table_to_rows, run_llm_batch, save_to_xlsx,
)
from page_locator import dedup_rows, stats_summary

DIM_NAME = "岗位聚集"
LLM_COLUMNS = schema.llm_header(DIM_NAME)      # 7业务列 + 原文支撑内容 = 8列
KEY_COLS = (0, 3)                              # 岗位名称 + 招聘企业
QUOTE_IDX = len(LLM_COLUMNS) - 1               # 7
MAX_ROWS = 200
BLANK_WORDS = {"", "未知", "无", "-", "—", "–", "nan", "null", "none", "未标注"}

_JOB_SUFFIX_RE = re.compile(r"(20\d{2}届|\d{2}届|\d{2}校招|校招|秋招|春招|提前批|社招|实习|校园)")
_JIEBIE_RE = re.compile(r"(?:20)?(\d{2})届")
_YEAR_RE = re.compile(r"20\d{2}")
_NUM_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)$")
_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)$")
_SALARY_UNIT_CHARS = "元万kK千薪·/"
_STRIP_PUNCT = " -—–·,，。.()（）/\\|[]【】"


def _blank(v):
    """未知/无/未标注/nan 等占位词 -> ""。"""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s in BLANK_WORDS or s.lower() in BLANK_WORDS else s


def _pad_empty(row, ncols):
    row = list(row)
    while len(row) < ncols:
        row.append("")
    return row


def clean_job_name(raw):
    """剥离届别/校招/实习等招聘噪声；清洗后不足2字回退原值。"""
    s = _blank(raw)
    if not s:
        return ""
    cleaned = _JOB_SUFFIX_RE.sub("", s)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(_STRIP_PUNCT)
    return cleaned if len(cleaned) >= 2 else s


def year_from_jiebie(raw):
    """届别 -> 需求年份："26届"->"2026"；无届别返回 ""。"""
    s = _blank(raw)
    if not s:
        return ""
    m = _JIEBIE_RE.search(s)
    return f"20{m.group(1)}" if m else ""


def _year_from_name(name):
    m = _YEAR_RE.search(Path(name).stem)
    return m.group(0) if m else ""


def clean_salary(raw):
    """薪资归一：面议/占位->面议；带单位原样保留；纯数值/区间按量级补单位。

    量级规则：>9999000 视为占位薪资（如 0-9999999）-> 面议；
    >=1000 -> 元/月；>=100 -> 元/天；否则 -> k/月。
    """
    s = _blank(raw)
    if not s:
        return ""
    if "面议" in s:
        return "面议"
    compact = s.replace(",", "").replace("，", "").strip()
    if not compact:
        return s
    if any(ch in compact for ch in _SALARY_UNIT_CHARS):
        return compact
    m = _NUM_RANGE_RE.fullmatch(compact)
    if m:
        lo, hi = m.group(1), m.group(2)
        hi_f = float(hi)
        if hi_f > 9999000:
            return "面议"
        if hi_f >= 1000:
            return f"{lo}-{hi} 元/月"
        if hi_f >= 100:
            return f"{lo}-{hi} 元/天"
        return f"{lo}-{hi} k/月"
    m = _NUM_RE.fullmatch(compact)
    if m:
        v = float(m.group(1))
        if v > 9999000:
            return "面议"
        if v >= 1000:
            return f"{m.group(1)} 元/月"
        if v >= 100:
            return f"{m.group(1)} 元/天"
        return f"{m.group(1)} k/月"
    return compact


# ============================================================
# 行业领域归一（单次 LLM 批量，公司列表封顶 150）
# ============================================================
def build_industry_messages(companies):
    lines = ["| 公司 | 行业领域 |", "| --- | --- |"]
    lines += [f"| {c} |  |" for c in companies]
    user = (
        "任务：为下列招聘企业判断所属行业领域（参考值：互联网/IT软件、金融、"
        "制造业、教育、医疗健康、能源/化工、建筑/房地产、交通运输、批发零售、"
        "文化/传媒、政府/事业单位、科研、农业、其他）。\n"
        "规则：\n"
        "1. 严格保持输入顺序逐家输出，公司名与输入完全一致；\n"
        "2. 无法判断的行业领域留空；\n"
        "3. 表头固定为：| 公司 | 行业领域 |\n\n"
        + "\n".join(lines)
        + " /no_think"
    )
    return [
        {"role": "system",
         "content": "你是严谨的行业分类分析员。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": user},
    ]


def parse_industry(md_text):
    """LLM 行业表 -> {公司: 行业领域}。"""
    table = {}
    if not md_text:
        return table
    for line in str(md_text).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        name, ind = cells[0], cells[1]
        if not name or name == "公司" or set(name) <= {"-", ":", " "}:
            continue
        ind = _blank(ind)
        if ind and name not in table:
            table[name] = ind
    return table


def _norm_key(s):
    return re.sub(r"\s+", "", s)


def _lookup_industry(company, table):
    """三级查找：精确 -> 去空白精确 -> 双向子串。"""
    if not company:
        return ""
    if company in table:
        return table[company]
    ck = _norm_key(company)
    for name, ind in table.items():
        if _norm_key(name) == ck:
            return ind
    for name, ind in table.items():
        nk = _norm_key(name)
        if nk and (nk in ck or ck in nk):
            return ind
    return ""


# ============================================================
# 通道A：岗位技能表 CSV -> 规则直通
# ============================================================
REQUIRED_COLS = ("文件名", "类别", "公司", "岗位", "届别", "城市", "薪资", "技能关键词")


def _read_csv(csv_path):
    try:
        return pd.read_csv(csv_path, dtype=str, keep_default_na=False,
                           encoding="utf-8-sig", on_bad_lines="skip")
    except UnicodeDecodeError:
        pass
    try:
        return pd.read_csv(csv_path, dtype=str, keep_default_na=False,
                           encoding="gbk", on_bad_lines="skip")
    except UnicodeDecodeError:
        return pd.read_csv(csv_path, dtype=str, keep_default_na=False,
                           encoding="utf-8", encoding_errors="replace",
                           on_bad_lines="skip")


def _first_source_org(df):
    for v in df["文件名"]:
        org = schema.parse_source_org(_blank(v))
        if org:
            return org
    return ""


def extract_from_jd_csv(csv_path, out_dir, workers=LLM_WORKERS):
    """岗位技能表 CSV -> 岗位聚集.xlsx（页码=R+CSV行号，不做引文定位）。"""
    csv_path, out_dir = Path(csv_path), Path(out_dir)
    df = _read_csv(csv_path)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        return {"dim": DIM_NAME, "doc": csv_path.name, "rows": 0, "dup": 0,
                "note": f"缺少必需列：{'、'.join(missing)}，跳过"}

    companies, seen = [], set()
    for v in df["公司"]:
        c = _blank(v)
        if c and c not in seen:
            seen.add(c)
            companies.append(c)
        if len(companies) >= 150:
            break
    industry = {}
    if companies:
        results = run_llm_batch(
            [{"key": "industry", "messages": build_industry_messages(companies)}],
            workers=1, desc=f"岗位聚集·行业归一（{len(companies)}家公司）")
        industry = parse_industry(results.get("industry"))

    rows, skipped = [], 0
    for i, rec in df.iterrows():
        raw_job = _blank(rec.get("岗位", ""))
        job = clean_job_name(raw_job)
        if not job:
            skipped += 1
            continue
        company = _blank(rec.get("公司", ""))
        city = _blank(rec.get("城市", ""))
        category = _blank(rec.get("类别", ""))
        year = year_from_jiebie(rec.get("届别", ""))
        raw_salary = _blank(rec.get("薪资", ""))
        salary = clean_salary(raw_salary)
        ind = _lookup_industry(company, industry)
        support = "；".join(
            f"{k}：{v}" for k, v in (
                ("岗位", raw_job), ("公司", company), ("城市", city),
                ("薪资", raw_salary),
                ("技能关键词", _blank(rec.get("技能关键词", ""))),
            ) if v)
        rows.append([job, category, year, company, city, ind, salary,
                     support, f"R{i + 2}"])

    merged = [list(LLM_COLUMNS) + ["页码"]] + rows
    merged, dup = dedup_rows(merged, KEY_COLS, QUOTE_IDX)
    if len(merged) - 1 > MAX_ROWS:
        merged = merged[: MAX_ROWS + 1]
    source_value = f"《岗位技能表》（来源：{_first_source_org(df) or '未知机构'}）"
    merged = schema.assemble_rows(merged, DIM_NAME, source_value)
    schema.assert_header(merged, DIM_NAME)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_to_xlsx(merged, out_dir / f"{DIM_NAME}.xlsx", DIM_NAME)
    return {"dim": DIM_NAME, "doc": csv_path.name, "rows": len(merged) - 1,
            "dup": dup, "skipped": skipped, "note": ""}


# ============================================================
# 通道B：试点报告（薪酬调查 / 最缺工排行）-> LLM 抽取
# ============================================================
def build_report_messages(text, year_hint, is_missing):
    year_rule = (f"需求年份统一填“{year_hint}”。" if year_hint
                 else "需求年份从报告标题或内容判断，判断不出留空。")
    if is_missing:
        branch = (
            "本报告为“最缺工职业排行”，按以下规则抽取：\n"
            "1. 每个职业一行，“岗位名称”照抄排行中的职业名称；\n"
            "2. “岗位类别”填“最缺工职业第N名”，N照抄该职业在名单中的序号；\n"
            "3. “薪资范围”一律留空；\n"
            "4. “招聘企业”统一填“多家企业汇总”；\n"
            "5. “企业所在城市”留空；\n"
            "6. “行业领域”填“综合”。"
        )
        quote_rule = "7. “原文支撑内容”从原文逐字摘录15~80字，足以支撑该行结论；"
    else:
        branch = (
            "本报告为企业薪酬调查报告，按以下规则抽取：\n"
            "1. 每个职业取一行，薪资取该职业的中位数（50分位），照抄报告中的"
            "数字与单位，严禁换算或推算；\n"
            "2. “岗位名称”照抄报告中的职业/岗位名称；\n"
            "3. “岗位类别”填“薪酬调查职业”；\n"
            "4. 若报告按城市分列，则每个职业每个城市各取一行：“企业所在城市”填对应城市，"
            "“招聘企业”填“{城市}多家企业汇总”，“薪资范围”填该城市的薪酬中位数；\n"
            "5. 若报告未按城市分列，“企业所在城市”留空，“招聘企业”填“多家企业汇总”；\n"
            "6. “行业领域”填“综合”。"
        )
        # 薪酬表格为“产业|岗位|三个分位数值”逐单元格排列，行内连续可锚定；
        # 混入表头词或其他行数值会同时破坏 exact/subseq/fuzzy 定位（8-gram 不连续）
        quote_rule = (
            "7. “原文支撑内容”照抄该岗位所在数据行的连续单元格原文"
            "（组首行可带产业名，如“医药健康产业 医药技术研发人员 17139 19058 25104”；"
            "其余行如“临床研究员 14230 19012 24586”），保持原顺序、数字逐位照抄；"
            "严禁混入表头词（“热招岗位”“岗位类型”“中位值”等）"
            "或同一表格其他行的数值；"
        )
    common = (
        f"{quote_rule}\n"
        "8. 最多输出200行；\n"
        f"9. {year_rule}\n"
        f"10. 表头固定为：{'|'.join(LLM_COLUMNS)}"
    )
    user = (f"任务：从以下人力资源报告原文抽取岗位聚集信息。\n{branch}\n{common}\n\n"
            f"报告原文：\n{text} /no_think")
    return [
        {"role": "system",
         "content": "你是严谨的人力资源报告分析员。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": user},
    ]


def parse_report_rows(md_text, year_hint):
    parsed = markdown_table_to_rows(md_text, LLM_COLUMNS)
    rows = []
    for r in parsed[1:]:
        r = [_blank(clean_cell(c)) for c in r]
        r = _pad_empty(r, len(LLM_COLUMNS))
        if r[0] == LLM_COLUMNS[0]:
            continue
        if not r[0] or len(re.sub(r"\s+", "", r[QUOTE_IDX])) < 4:
            continue
        if not r[2]:
            r[2] = year_hint
        r[6] = clean_salary(r[6])
        rows.append(r)
    return rows


def extract_from_report(path, out_dir, workers=LLM_WORKERS):
    """试点报告 -> 岗位聚集.xlsx（LLM 通道，落表前页码定位）。"""
    path, out_dir = Path(path), Path(out_dir)
    loaded = load_any(path)
    pages = loaded["pages"]
    text = join_pages(pages, MAX_INPUT_CHARS)
    if len(re.sub(r"\s+", "", text)) < 100:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "dup": 0,
                "locate": "", "note": "无有效文本层，跳过"}
    year_hint = _year_from_name(path.name)
    is_missing = "最缺工" in path.stem
    md, rows = "", []
    for attempt in range(3):  # 偶发返回空表/非表格内容时最多重试 2 次
        results = run_llm_batch(
            [{"key": "report",
              "messages": build_report_messages(text, year_hint, is_missing)}],
            workers=1, desc=f"岗位聚集·报告 {path.name[:24]}")
        md = results.get("report") or ""
        if not md:
            if attempt < 2:
                print(f"    [重试] {path.name[:24]} LLM 未返回，重调 {attempt + 1}/2")
            continue
        rows = parse_report_rows(md, year_hint)
        if rows:
            break
        if attempt < 2:
            print(f"    [重试] {path.name[:24]} 解析 0 行，重调 LLM {attempt + 1}/2")
    if not md:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "dup": 0,
                "locate": "", "note": "LLM未返回结果"}
    if not rows:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "dup": 0,
                "locate": "", "note": "LLM 返回内容解析后 0 行（已重试 3 次）"}
    merged = [list(LLM_COLUMNS)] + rows
    merged, dup = dedup_rows(merged, KEY_COLS, QUOTE_IDX)
    if len(merged) - 1 > MAX_ROWS:
        merged = merged[: MAX_ROWS + 1]
    merged, stats = locate_rows(merged, pages, QUOTE_IDX)
    merged = schema.assemble_rows(merged, DIM_NAME,
                                  schema.make_source_value(path.name))
    schema.assert_header(merged, DIM_NAME)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_to_xlsx(merged, out_dir / f"{DIM_NAME}.xlsx", DIM_NAME)
    return {"dim": DIM_NAME, "doc": path.name, "rows": len(merged) - 1,
            "dup": dup, "locate": stats_summary(stats), "note": ""}
