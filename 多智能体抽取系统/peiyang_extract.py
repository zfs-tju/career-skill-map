# -*- coding: utf-8 -*-
"""维度4：培养供给链。

数据源：麦可思高薪专业榜/绿牌解读、本科起薪分布、智联就业前景研判、
新能源就业趋势等"专业×就业"报告。
口径：专业名照抄不拆并；学科门类限 14 门类枚举；匹配度限 高/中/低 且
必须有报告明确证据；开设院校/核心知识点原文没有就留空。
"""
import re
from pathlib import Path

import employment_schema as schema
from doc_loader import load_any
from gangwei_extract import (
    LLM_WORKERS, MAX_INPUT_CHARS, join_pages, locate_rows, markdown_table_to_rows,
    run_llm_batch, save_to_xlsx,
)
from page_locator import dedup_rows, stats_summary

DIM_NAME = "培养供给链"
LLM_COLUMNS = schema.llm_header(DIM_NAME)      # 8业务列 + 原文支撑内容 = 9列
KEY_COLS = (0, 3, 6)                           # 专业/课程, 培养技能, 对口岗位
QUOTE_IDX = len(LLM_COLUMNS) - 1              # 8
MAX_ROWS = 200

_MATCH_LEVELS = ("高", "中", "低")


def build_edu_messages(text):
    columns = "|".join(LLM_COLUMNS)
    prompt = f"""任务：从以下就业/专业报告中抽取"培养供给链"信息（专业 → 岗位/技能供给关系）。

规则：
1. 每个专业一行，"专业/课程"照抄报告中的专业名（如"微电子科学与工程"），不拆不并；
2. "学科门类"只能从这些词里选：{'、'.join(schema.SUBJECT_CATEGORIES)}，判断不出留空；
3. "核心知识点""培养技能"：报告明确提到该专业所学的知识/技能才填（顿号分隔），没提留空；
4. "开设院校""院校城市"：报告明确列出才填（院校用全称），没提留空；
5. "对口岗位"：报告提到的该专业毕业生从事的岗位/职业，或主要就业行业
   （如"半导体和其他电子元件制造业"），照抄原文，没提留空；
6. "匹配度"只能填 高/中/低：报告给出该专业明确的正面证据（高薪榜前列、
   绿牌、需求增速领先）填"高"，明确负面（红牌、就业预警）填"低"，其余留空；
7. "原文支撑内容"逐字摘录原文中包含该专业名称及其关键数字的连续片段
   （表格行照抄整行单元格，如"微电子科学与工程 7814"；
   新闻/散文体照抄原句片段，如"电子科学与技术以7752元位居第二"），
   保持原文顺序，严禁改写或混入其他专业的数值；
8. 最多输出200行，只输出Markdown表格，表头固定为：{columns}

报告原文如下：
{text}"""
    return [
        {"role": "system",
         "content": "你是严谨的教育就业数据分析师。只返回Markdown表格，不要任何思考过程和解释。"},
        {"role": "user", "content": prompt + " /no_think"},
    ]


def parse_edu_rows(md_text):
    parsed = markdown_table_to_rows(md_text, LLM_COLUMNS)
    out = []
    for r in parsed[1:]:
        r = [str(c or "").strip() for c in r]
        r += [""] * (len(LLM_COLUMNS) - len(r))
        if r[0] == LLM_COLUMNS[0]:
            continue
        if not r[0] or len(re.sub(r"\s+", "", r[QUOTE_IDX])) < 4:
            continue
        if r[1] and r[1] not in schema.SUBJECT_CATEGORIES:
            r[1] = ""
        if r[7] and r[7] not in _MATCH_LEVELS:
            r[7] = ""
        out.append(r[:len(LLM_COLUMNS)])
    return out


def extract_supply_chain(path, out_dir, workers=LLM_WORKERS):
    """专业/就业报告 -> 培养供给链.xlsx。返回结果 dict。"""
    path, out_dir = Path(path), Path(out_dir)
    loaded = load_any(path)
    pages = loaded["pages"]
    text = join_pages(pages, MAX_INPUT_CHARS)
    md, rows = "", []
    for attempt in range(3):  # 偶发空表时最多重试 2 次
        results = run_llm_batch(
            [{"key": "edu", "messages": build_edu_messages(text)}],
            workers=1, desc=f"培养供给链·{path.name[:24]}")
        md = results.get("edu") or ""
        if not md:
            if attempt < 2:
                print(f"    [重试] {path.name[:24]} LLM 未返回，重调 {attempt + 1}/2")
            continue
        rows = parse_edu_rows(md)
        if rows:
            break
        if attempt < 2:
            print(f"    [重试] {path.name[:24]} 解析 0 行，重调 LLM {attempt + 1}/2")
    if not md:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0,
                "note": "LLM未返回结果"}
    if not rows:
        return {"dim": DIM_NAME, "doc": path.name, "rows": 0,
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
            "dup": dup, "locate": stats_summary(stats), "note": ""}
