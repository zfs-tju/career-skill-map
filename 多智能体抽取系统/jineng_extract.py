# -*- coding: utf-8 -*-
"""维度2：技能结构。职业标准 PDF -> 12列技能结构表（路径a：按新契约直接抽取）。"""
import re
from pathlib import Path

import employment_schema as schema
from doc_loader import load_any
from gangwei_extract import (
    LLM_WORKERS,
    MAX_INPUT_CHARS,
    clean_cell,
    compute_ability_slices,
    extract_chapter,
    extract_job_name,
    join_pages,
    locate_rows,
    markdown_table_to_rows,
    normalize_level,
    run_llm_batch,
    save_to_xlsx,
    scan_direction_names,
)
from page_locator import dedup_rows, stats_summary

DIM_NAME = "技能结构"
LLM_COLUMNS = schema.llm_header(DIM_NAME)
KEY_COLS = (0, 1, 2)
QUOTE_IDX = len(LLM_COLUMNS) - 1
MAX_ROWS = 500
BLANK_WORDS = {"", "未知", "无", "-", "—", "–", "nan", "null", "none"}
_STD_TAIL_RE = re.compile(r"^(.*?)(?:国家职业技术技能标准|国家职业技能标准|国家职业标准)")
_YEAR_TAIL_RE = re.compile(r"(?:[（(]\s*)?20\d{2}\s*年版(?:\s*[)）])?\s*$")
_SYS_PROMPT = "你是严谨的职业标准分析员。只返回Markdown表格，不要任何思考过程和解释。"


def _blank(v):
    s = str(v or "").strip()
    return "" if s.lower() in BLANK_WORDS else s


def _pad_empty(row, ncols):
    r = [str(x) for x in row][:ncols]
    return r + [""] * (ncols - len(r))


def _job_from_stem(stem):
    s = Path(stem).stem.split("（来源")[0]
    m = _STD_TAIL_RE.match(s)
    name = m.group(1) if m else s
    name = _YEAR_TAIL_RE.sub("", name).strip(" -—_·《》 ")
    return name[:25]


def build_skill_messages(slice_text, job_name, direction, level):
    scope = f"（职业方向：{direction}）" if direction else ""
    if level:
        level_hint = f"岗位级别统一填“{level}”。"
    else:
        level_hint = ("岗位级别从片段内的等级小节标题（初级/中级/高级/技师/高级技师）"
                      "判断，判断不出留空。")
    columns = "|".join(LLM_COLUMNS)
    example = ("|示例岗位|初级|编写数据清洗脚本|数据与数据库|熟练掌握|Python| |"
               "完成训练数据的采集清洗与质检|中| |能编写脚本完成数据清洗与格式转换|")
    prompt = f"""以下是《{job_name}》国家职业标准"3 工作要求"章节的一个片段{scope}。

请通读片段，把其中每一项职业技能要求抽取成表格，一行一个技能点：
1. 原文一条"能……"描述里若包含多个独立技能动作（用分号/顿号并列），拆成1~3行；核心技能每行10~15字。
2. 岗位名称统一填"{job_name}"。
3. {level_hint}
4. 技能分类只能从这些词里选：{'、'.join(schema.SKILL_CATEGORIES)}。
5. 技能等级要求：照抄原文对该技能的掌握程度词（如"熟练掌握""能够独立完成""了解"），原文没有则留空。
6. 工具/平台、认证证书、替代技能：原文明确提到才填，没提到一律留空。
7. 技能描述：20~40字概括该技能做什么、用在什么场景，不得编造原文没有的能力。
8. 学习难度：按技能复杂度填 低/中/高 之一。
9. 原文支撑内容：从片段逐字摘录支撑该行最关键的15~60字（含"能……"原句或关键词），必须与片段原文完全一致，便于定位页码。
10. 只输出Markdown表格，表头固定为：{columns}

示例行（仅供参考格式，严禁照抄内容）：
{example}

片段如下：
{slice_text}"""
    return [
        {"role": "system", "content": _SYS_PROMPT},
        {"role": "user", "content": prompt + " /no_think"},
    ]


def parse_skill_rows(md_text, job_name, level):
    parsed = markdown_table_to_rows(md_text, LLM_COLUMNS)
    out = []
    for r in parsed[1:]:
        r = [_blank(clean_cell(c)) for c in r]
        r = _pad_empty(r, len(LLM_COLUMNS))
        if r[0] == LLM_COLUMNS[0]:
            continue
        if not r[2] or len(re.sub(r"\s+", "", r[QUOTE_IDX])) < 4:
            continue
        if job_name:
            r[0] = job_name
        lv = level if level else normalize_level(r[1])
        r[1] = lv if lv in schema.STD_JOB_LEVELS else ""
        if r[3] and r[3] not in schema.SKILL_CATEGORIES:
            r[3] = "其他"
        if r[8] and r[8] not in schema.DIFFICULTY_LEVELS:
            r[8] = ""
        out.append(r)
    return out


def extract_skill_structure(path, out_dir, workers=LLM_WORKERS):
    """单部职业标准 -> 技能结构表。返回结果 dict（含 note 时 rows=0）。"""
    path, out_dir = Path(path), Path(out_dir)
    loaded = load_any(path)
    pages = loaded["pages"]
    text = join_pages(pages, MAX_INPUT_CHARS)
    job_name = extract_job_name(text)
    if not job_name or len(job_name) < 4 or not re.search(r"[\u4e00-\u9fff]", job_name):
        job_name = _job_from_stem(path.stem) or "未知岗位"
    chapter = extract_chapter(text)
    if not chapter or len(re.sub(r"\s+", "", chapter)) < 300:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0, "job": job_name,
                "note": "未定位“3 工作要求”章节或章节过短，跳过"}
    direction_names = scan_direction_names(chapter)
    slices = compute_ability_slices(chapter, direction_names)
    tasks = [{"key": f"s{i}",
              "messages": build_skill_messages(s["text"], job_name,
                                               s.get("direction", ""), s.get("level")),
              "level": s.get("level")}
             for i, s in enumerate(slices)]
    results = run_llm_batch(tasks, workers=workers, desc=f"技能结构·{job_name}")
    empty = [t for t in tasks if not results.get(t["key"])]
    if empty:
        results.update(run_llm_batch(empty, workers=min(2, workers),
                                     desc=f"技能结构·重试{len(empty)}片"))
    rows = []
    for t in tasks:
        md = results.get(t["key"])
        if md:
            rows.extend(parse_skill_rows(md, job_name, t["level"]))
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
            "job": job_name, "slices": len(slices), "dup": dup,
            "locate": stats_summary(stats), "note": ""}
