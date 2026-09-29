# -*- coding: utf-8 -*-
"""维度5：岗位竞合（由技能结构表规则推导，不调 LLM）。

推导口径（设计方案：技能组交集算重叠度的兜底路径）：
  - 岗位技能组 = 该职业标准 技能结构.xlsx 全部"核心技能"（归一后去重）；
  - 技能匹配 = 精确相等 / 包含 / 共享 4 字子串（跨标准动作短语粒度不一）；
  - 岗位对(A,B) 匹配技能数 ≥ MIN_OVERLAP 且重叠率 ≥ MIN_RATIO 时产出竞合行；
  - 技能重叠度 = 匹配数 / 较小方技能数，输出百分数（下游已做颜色映射）；
  - 岗位A/B 描述取源标准"职业定义"首句（取不到则按技能分类分布概括）；
  - 无序对归并：A-B 与 B-A 视为同一条；
  - 溯源：数据来源指向两部标准，页号留空，原文支撑内容列出重叠技能清单。
"""
import re
from collections import Counter
from pathlib import Path

import pandas as pd

import employment_schema as schema
from doc_loader import load_any
from gangwei_extract import join_pages, save_to_xlsx

DIM_NAME = "岗位竞合"
MIN_OVERLAP = 6          # 匹配技能数下限
MIN_RATIO = 0.10         # 技能重叠率下限（匹配数/较小方技能数）
MAX_PAIR_SKILLS = 12     # 技能组单元格最多列的技能数
DERIVED_DIR = "_跨文档推导"

_SENT_RE = re.compile(r"职业定义[：:]?\s*\n?\s*([\u4e00-\u9fff][^。]{9,90}。)")


def extract_job_definition(pdf_path):
    """从职业标准源文件提取"职业定义"首句；取不到返回空串。"""
    try:
        t = join_pages(load_any(pdf_path)["pages"])
    except Exception:
        return ""
    for m in _SENT_RE.finditer(t):
        s = re.sub(r"\s+", "", m.group(1))
        return s
    return ""


def _norm_skill(s):
    return re.sub(r"\s+|　", "", str(s or ""))


def _share4(a, b):
    """两技能短语是否共享任一 4 字子串（词面部分重合，如"深度神经网络"~"深度神经网络部署"）。"""
    if len(a) < 4 or len(b) < 4:
        return False
    grams = {a[i:i + 4] for i in range(len(a) - 3)}
    return any(b[i:i + 4] in grams for i in range(len(b) - 3))


def _overlap_skills(sa, sb, cap=200):
    """技能组交集：精确相等 / 包含 / 共享4字子串 三级词面匹配。

    跨标准的核心技能为动作短语、粒度不一（"深度学习" vs "深度学习模型训练"），
    仅精确相等会系统性低估重叠。返回 (重叠技能代表集合, 匹配数/较小方技能数)。"""
    sa = [s for s, _ in Counter(sa).most_common(cap)]
    sb = [s for s, _ in Counter(sb).most_common(cap)]
    inter = set(sa) & set(sb)
    used_b = set(inter)
    for a in sa:
        if a in inter:
            continue
        for b in sb:
            if b in used_b:
                continue
            if ((len(a) >= 4 and a in b) or (len(b) >= 4 and b in a)
                    or _share4(a, b)):
                inter.add(a if len(a) <= len(b) else b)
                used_b.add(b)
                break
    return inter, min(len(sa), len(sb))


def collect_job_skills(out_root):
    """扫描全部 技能结构.xlsx -> {job: (skills_set, cat_counter, source_dir)}。"""
    jobs = {}
    for xlsx in sorted(Path(out_root).rglob("技能结构.xlsx")):
        if DERIVED_DIR in xlsx.parts:
            continue
        df = pd.read_excel(xlsx, dtype=str)
        if "岗位名称" not in df.columns:
            continue
        skills, cats = Counter(), Counter()
        for _, row in df.iterrows():
            s = _norm_skill(row.get("核心技能"))
            if s and s not in ("nan", "未知"):
                skills[s] += 1
            c = str(row.get("技能分类") or "").strip()
            if c and c != "nan":
                cats[c] += 1
        if skills:
            jobs[str(df.iloc[0]["岗位名称"])] = (skills, cats, xlsx.parent.name)
    return jobs


def _skill_group(skills, n=MAX_PAIR_SKILLS):
    return "、".join(s for s, _ in skills.most_common(n))


def _job_desc(defin, cats):
    if defin:
        return defin
    top = [c for c, _ in cats.most_common(3)]
    return f"以{'、'.join(top)}类技能为主" if top else ""


def derive_jinghe(out_root):
    """技能结构表 -> _跨文档推导/岗位竞合.xlsx。返回结果 dict。"""
    out_root = Path(out_root)
    jobs = collect_job_skills(out_root)
    if len(jobs) < 2:
        return {"dim": DIM_NAME, "doc": "-", "rows": 0,
                "note": f"技能结构文档不足 2 份（现有 {len(jobs)}），无法推导"}

    # 职业定义（源 PDF 按目录名 stem 匹配）
    pdfs_root = Path("pdfs")
    def _find_source(stem_dir):
        for p in pdfs_root.rglob(f"{stem_dir}.pdf"):
            return p
        return None

    names = sorted(jobs)
    defs = {}
    for j in names:
        src = _find_source(jobs[j][2])
        defs[j] = extract_job_definition(src) if src else ""

    rows = []
    for i in range(len(names)):
        for k in range(i + 1, len(names)):
            a, b = names[i], names[k]
            inter, denom = _overlap_skills(jobs[a][0], jobs[b][0])
            ratio = len(inter) / denom
            if len(inter) < MIN_OVERLAP or ratio < MIN_RATIO:
                continue
            overlap_pct = f"{ratio:.0%}"
            src_val = (f"《{jobs[a][2]}》与《{jobs[b][2]}》（技能结构推导）")
            support = "重叠技能：" + "、".join(sorted(inter))
            biz = [a, _skill_group(jobs[a][0]), _job_desc(defs[a], jobs[a][1]),
                   overlap_pct, b, _skill_group(jobs[b][0]), _job_desc(defs[b], jobs[b][1])]
            rows.append(biz + [src_val, "", support])

    header = schema.full_header(DIM_NAME)
    # 无序对已由 (i,k) 有序遍历天然归并；再按 (岗位A, 岗位B) 保险去重
    seen, uniq = set(), []
    for r in rows:
        key = (r[0], r[4])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    out_dir = out_root / DERIVED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    save_to_xlsx([header] + uniq, out_dir / f"{DIM_NAME}.xlsx", DIM_NAME)
    return {"dim": DIM_NAME, "doc": f"{DERIVED_DIR}/（{len(jobs)} 岗位两两推导）",
            "rows": len(uniq),
            "locate": f"规则推导（技能组交集≥{MIN_OVERLAP}，共 {len(jobs)} 岗位）",
            "note": ""}
