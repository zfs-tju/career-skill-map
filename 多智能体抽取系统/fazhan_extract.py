# -*- coding: utf-8 -*-
"""维度3：发展路径。

数据源：职业标准"申报条件"小节（五级→一级的从业年限即天然晋升链）。
口径：职级用国家体系枚举（经 normalize_level 归一）；"平均年限"只取原文
明确年限，禁止估算；"转换难度"由系统按年限规则推导（非 LLM 估）。
"""
import re
from pathlib import Path

import employment_schema as schema
from doc_loader import load_any
from gangwei_extract import (
    LLM_WORKERS, MAX_INPUT_CHARS, extract_job_name, join_pages, locate_rows,
    markdown_table_to_rows, normalize_level, run_llm_batch, save_to_xlsx,
)
from jineng_extract import _job_from_stem
from page_locator import dedup_rows, stats_summary

DIM_NAME = "发展路径"
LLM_COLUMNS = schema.llm_header(DIM_NAME)      # 8业务列 + 原文支撑内容 = 9列
KEY_COLS = (0, 2, 1, 3)                        # 起始岗位, 目标岗位, 起始职级, 目标职级
QUOTE_IDX = len(LLM_COLUMNS) - 1               # 8
MAX_ROWS = 200

# 申报条件小节标题（独立行，可能带 1.9.1 类编号）
_APPLY_HEAD_RE = re.compile(r"^\s*\d*(?:\.\d+)*[\.．、\s]*申报条件\s*$", re.M)
# 小节结束：下一个编号标题（如 1.9.2 考核方式）或鉴定/比重/工作要求章节
_APPLY_END_RE = re.compile(
    r"^\s*\d+(?:\.\d+)+\s*\S|^\s*\d+\s*[\.．、]\s*(考核方式|鉴定方式|评价方式|比重表|权重表|工作要求)",
    re.M,
)
_YEAR_NUM_RE = re.compile(r"(\d+)\s*年")


def extract_apply_section(text):
    """提取"申报条件"小节文本；取最后一次出现（避开目录页）。"""
    starts = [m.start() for m in _APPLY_HEAD_RE.finditer(text)]
    if not starts:
        return None
    seg = text[starts[-1]:]
    m = _APPLY_END_RE.search(seg[20:])
    end = 20 + m.start() if m else len(seg)
    seg = seg[:end]
    return seg if len(re.sub(r"\s+", "", seg)) > 60 else None


def build_path_messages(section, job_name):
    columns = "|".join(LLM_COLUMNS)
    prompt = f"""以下是《{job_name}》国家职业标准的"申报条件"小节。

请把其中的职业晋升条件抽取成表格，一行一条晋升路径：
1. 每条"取得X级证书后从事本职业满N年可申报Y级"的证书晋升条件一行；
2. "起始岗位""目标岗位"统一填"{job_name}"；
3. "起始职级""目标职级"只能从这些词里选：初级、中级、高级、技师、高级技师
   （原文"五级/初级工"=初级，"四级/中级工"=中级，"三级/高级工"=高级，"二级/技师"=技师，"一级/高级技师"=高级技师）；
4. "平均年限"只填原文明确给出的从业年限数字（"满3年"填"3"），原文没有则留空，严禁估算；
5. "所需新增技能""职业方向"原文未提及则留空；
6. "转换难度"留空（由系统按年限推导）；
7. "原文支撑内容"逐字摘录该条申报条件原文15~80字（必须含职级与年限表述），
   严禁混入其他条件的文字或小节标题；
8. 只输出Markdown表格，表头固定为：{columns}

申报条件原文如下：
{section}"""
    return [
        {"role": "system",
         "content": "你是严谨的国家职业标准分析员。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": prompt + " /no_think"},
    ]


def _difficulty_from_years(years):
    """转换难度规则推导（透明规则，非 LLM 估）：≤1年低，2-4年中，≥5年高。"""
    if not years:
        return ""
    n = int(years)
    if n <= 1:
        return "低"
    if n <= 4:
        return "中"
    return "高"


def parse_path_rows(md_text, job_name):
    parsed = markdown_table_to_rows(md_text, LLM_COLUMNS)
    out = []
    for r in parsed[1:]:
        r = [str(c or "").strip() for c in r]
        r += [""] * (len(LLM_COLUMNS) - len(r))
        if r[0] == LLM_COLUMNS[0]:
            continue
        if len(re.sub(r"\s+", "", r[QUOTE_IDX])) < 4:
            continue
        # 职级归一（五级体系词 → 枚举）
        r[1] = normalize_level(r[1]) if r[1] else ""
        r[3] = normalize_level(r[3]) if r[3] else ""
        if r[1] and r[1] not in schema.STD_JOB_LEVELS:
            r[1] = ""
        if r[3] and r[3] not in schema.STD_JOB_LEVELS:
            r[3] = ""
        # 起始/目标必须至少有一处不同（禁止 A=A 同级自环）
        if r[0] == r[2] and r[1] == r[3]:
            continue
        # 年限只留数字（"3"/"3年" -> "3"）
        m = re.search(r"(\d+)", r[6]) if r[6] else None
        r[6] = m.group(1) if m else ""
        r[5] = _difficulty_from_years(r[6])
        if not r[0]:
            r[0] = job_name
        if not r[2]:
            r[2] = job_name
        out.append(r[:len(LLM_COLUMNS)])
    return out


def extract_development_path(path, out_dir, workers=LLM_WORKERS):
    """职业标准 -> 发展路径.xlsx。返回结果 dict。"""
    path, out_dir = Path(path), Path(out_dir)
    loaded = load_any(path)
    pages = loaded["pages"]
    text = join_pages(pages, MAX_INPUT_CHARS)
    job_name = None
    jn = extract_job_name(text)
    if jn and len(jn) >= 4 and re.search(r"[\u4e00-\u9fff]", jn):
        job_name = jn
    if not job_name:
        job_name = _job_from_stem(path.stem) or "未知岗位"
    section = extract_apply_section(text)
    if not section:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "job": job_name,
                "note": "未定位“申报条件”小节，跳过"}
    md, rows = "", []
    for attempt in range(3):  # 偶发空表时最多重试 2 次
        results = run_llm_batch(
            [{"key": "path",
              "messages": build_path_messages(section, job_name)}],
            workers=1, desc=f"发展路径·{job_name}")
        md = results.get("path") or ""
        if not md:
            if attempt < 2:
                print(f"    [重试] 发展路径 {job_name} LLM 未返回，重调 {attempt + 1}/2")
            continue
        rows = parse_path_rows(md, job_name)
        if rows:
            break
        if attempt < 2:
            print(f"    [重试] 发展路径 {job_name} 解析 0 行，重调 LLM {attempt + 1}/2")
    if not md:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "job": job_name,
                "note": "LLM未返回结果"}
    if not rows:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "job": job_name,
                "note": "LLM 返回内容解析后 0 行（已重试 3 次）"}
    merged = [list(LLM_COLUMNS)] + rows
    merged, dup = dedup_rows(merged, KEY_COLS, QUOTE_IDX)
    if len(merged) - 1 > MAX_ROWS:
        merged = merged[: MAX_ROWS + 1]
    merged, stats = locate_rows(merged, pages, QUOTE_IDX)
    merged = schema.assemble_rows(merged, DIM_NAME, schema.make_source_value(path.name))
    schema.assert_header(merged, DIM_NAME)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_to_xlsx(merged, out_dir / f"{DIM_NAME}.xlsx", DIM_NAME)
    return {"dim": DIM_NAME, "doc": path.name, "rows": len(merged) - 1,
            "job": job_name, "dup": dup, "locate": stats_summary(stats), "note": ""}
