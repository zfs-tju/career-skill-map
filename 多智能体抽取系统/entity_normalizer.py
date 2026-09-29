# -*- coding: utf-8 -*-
"""实体对齐层：岗位/技能/城市归一（设计方案三）。

图谱质量关键：5 维度共享岗位节点（4 表引用）、技能节点（3 表引用），
命名不一致会把同一实体拆成孤立节点。本模块在落表后统一归一：

  - 岗位：格式归一（空白/招聘后缀/职级前缀）——职级前缀拆解仅在
    "拆解后命中标准名录"时执行（防止把"高级产品经理"误拆）；
    标准名录 = 技能结构表的岗位名称（extract_job_name 产出，权威）；
  - 技能：词表化（技能结构全部核心技能），导出 技能词表.json 供下游；
  - 城市：去"市"后缀（北京市->北京，杭州市->杭州）。

产出（out_root 下）：
  - 岗位标准名录.json  {"标准岗位": [...], "别名归并": {variant: canonical}}
  - 技能词表.json      {"技能": [...], "数量": n}
用法：
    python entity_normalizer.py            # 对 know_graph_employment 执行归一
    或 from entity_normalizer import apply_normalization
"""
import json
import re
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

# 各维度表中"岗位名称"类业务列（模板列名）
JOB_COLS = {
    "岗位聚集": ["岗位名称"],
    "技能结构": ["岗位名称"],
    "发展路径": ["起始岗位", "目标岗位"],
    "培养供给链": ["对口岗位"],
    "岗位竞合": ["岗位A", "岗位B"],
}
CITY_COLS = {
    "岗位聚集": ["企业所在城市"],
    "培养供给链": ["院校城市"],
}

_WS_RE = re.compile(r"\s+|　")
# 招聘后缀噪声（JD 岗位名常见）
_RECRUIT_SUFFIX_RE = re.compile(
    r"(?:20\d{2}届|\d{2}届|\d{2}校招|校招|秋招|春招|提前批|社招|实习|校园)+$")
# 职级前缀（拆解需命中标准名录才执行）
_LEVEL_PREFIX_RE = re.compile(r"^(?:初级|中级|高级|技师|高级技师)(?:工)?[/／]?")
_STRIP_PUNCT = " 《》-—－·,，.()（）"


def _blank(v):
    s = str(v or "").strip()
    return "" if s in ("", "nan", "None", "未知") else s


def normalize_city(v):
    s = _blank(v)
    if not s:
        return v if v is None else s
    s2 = s[:-1] if s.endswith("市") and len(s) > 2 else s
    return s2


def normalize_job_format(v):
    """格式级归一：去空白/书名号/招聘后缀。不做职级拆解（需名录配合）。"""
    s = _blank(v)
    if not s:
        return s
    s = _WS_RE.sub("", s).strip(_STRIP_PUNCT)
    s = _RECRUIT_SUFFIX_RE.sub("", s)
    return s.strip(_STRIP_PUNCT)


def _normalize_with_registry(v, registry):
    """格式归一 + 名录门控的职级前缀拆解。"""
    s = normalize_job_format(v)
    if not s or not registry:
        return s
    base = _LEVEL_PREFIX_RE.sub("", s)
    if base and base != s and base in registry:
        return base
    return s


def _iter_tables(out_root):
    """(xlsx 路径, 维度名) —— 文件名即维度名（5 表契约）。"""
    for xlsx in sorted(Path(out_root).rglob("*.xlsx")):
        dim = xlsx.stem
        if dim in JOB_COLS or dim in CITY_COLS:
            yield xlsx, dim


def build_registry(out_root):
    """标准岗位名录 = 技能结构表全部岗位名称（权威标准名）。"""
    registry = set()
    for xlsx in sorted(Path(out_root).rglob("技能结构.xlsx")):
        df = pd.read_excel(xlsx, dtype=str)
        if "岗位名称" in df.columns:
            for v in df["岗位名称"].dropna().unique():
                s = normalize_job_format(v)
                if s:
                    registry.add(s)
    return registry


def build_skill_vocab(out_root):
    """技能词表 = 技能结构表全部核心技能（频次降序）。"""
    from collections import Counter
    skills = Counter()
    for xlsx in sorted(Path(out_root).rglob("技能结构.xlsx")):
        df = pd.read_excel(xlsx, dtype=str)
        if "核心技能" in df.columns:
            for v in df["核心技能"].dropna():
                s = _WS_RE.sub("", str(v))
                if s and s not in ("nan", "未知"):
                    skills[s] += 1
    return [s for s, _ in skills.most_common()]


def apply_normalization(out_root):
    """对 out_root 全部 5 表执行岗位/城市归一（原地重写，幂等）。

    返回统计 dict；同时导出 岗位标准名录.json / 技能词表.json。
    """
    out_root = Path(out_root)
    registry = build_registry(out_root)
    alias_merge = {}
    changed_files = changed_cells = 0

    for xlsx, dim in _iter_tables(out_root):
        wb = load_workbook(xlsx)
        ws = wb.active
        header = [c.value for c in ws[1]]
        job_idx = [header.index(c) for c in JOB_COLS.get(dim, []) if c in header]
        city_idx = [header.index(c) for c in CITY_COLS.get(dim, []) if c in header]
        if not job_idx and not city_idx:
            continue
        changed = 0
        for row in ws.iter_rows(min_row=2):
            for i in job_idx:
                cell = row[i]
                if not isinstance(cell.value, str) or not cell.value.strip():
                    continue
                new = _normalize_with_registry(cell.value, registry)
                if new and new != cell.value:
                    if new in registry and cell.value not in registry:
                        alias_merge[cell.value] = new
                    cell.value = new
                    changed += 1
            for i in city_idx:
                cell = row[i]
                if isinstance(cell.value, str) and cell.value.strip():
                    new = normalize_city(cell.value)
                    if new != cell.value:
                        cell.value = new
                        changed += 1
        if changed:
            wb.save(xlsx)
            changed_files += 1
            changed_cells += changed
            print(f"[对齐] {dim} {changed} 格：{xlsx.relative_to(out_root)}")

    (out_root / "岗位标准名录.json").write_text(
        json.dumps({"标准岗位": sorted(registry),
                    "别名归并": alias_merge}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    vocab = build_skill_vocab(out_root)
    (out_root / "技能词表.json").write_text(
        json.dumps({"技能": vocab, "数量": len(vocab)}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[对齐] 标准岗位 {len(registry)} 个，别名归并 {len(alias_merge)} 条，"
          f"技能词表 {len(vocab)} 条；改写 {changed_files} 文件 / {changed_cells} 格")
    return {"标准岗位": len(registry), "别名归并": len(alias_merge),
            "改写文件": changed_files, "改写单元格": changed_cells}


DERIVED_NAME_EXCLUDE = "_跨文档推导"


def ensure_all_dim_tables(out_root):
    """每源 5 表齐备：缺失维度补仅表头的空表（下游合并依赖表齐全）。"""
    out_root = Path(out_root)
    created = 0
    for doc_dir in [d for d in out_root.iterdir() if d.is_dir()
                    and d.name != DERIVED_NAME_EXCLUDE]:
        for dim in JOB_COLS:  # 5 个维度名
            f = doc_dir / f"{dim}.xlsx"
            if not f.exists():
                save_header_only(f, dim)
                created += 1
    print(f"[表齐备] 补建空表 {created} 份")
    return created


def save_header_only(path, dim):
    from gangwei_extract import save_to_xlsx
    save_to_xlsx([schema_full_header(dim)], path, dim)


def schema_full_header(dim):
    import employment_schema as schema
    return schema.full_header(dim)


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "know_graph_employment"
    apply_normalization(root)
    ensure_all_dim_tables(root)
