# -*- coding: utf-8 -*-
"""P3 质检：5 表契约校验 / 空表诊断 / 准确率抽样评估 / 跨维度碎片检查。

用法：
    python kg_validator_employment.py [--sample 30] [--skip-locate]

输出（know_graph_employment/质检报告.json + 控制台）：
  1. 契约校验：每份 xlsx 表头 == full_header(维度)，列序断约即报错；
  2. 空表诊断：每个源文档目录 × 5 维度的存在性/空表清单；
  3. 准确率抽样：随机抽 N 行，"原文支撑内容"回源页面重定位，报告命中率
     （命中=quote 能在《数据来源》指向的源文档页面文本中定位）；
  4. 碎片检查：跨维度共享的岗位名是否字符串一致（P2 验收：抽样30条零碎片）。
"""
import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

import employment_schema as schema
from doc_loader import load_any
from gangwei_extract import join_pages
from page_locator import PageLocator

DIMS = list(schema.TEMPLATE_HEADERS)
DERIVED_DIR = "_跨文档推导"

# 各维度的岗位名称类列（碎片检查用）
JOB_COLS = {
    "岗位聚集": ["岗位名称"],
    "技能结构": ["岗位名称"],
    "发展路径": ["起始岗位", "目标岗位"],
    "培养供给链": ["对口岗位"],
    "岗位竞合": ["岗位A", "岗位B"],
}


def _doc_dirs(out_root):
    return [d for d in sorted(Path(out_root).iterdir())
            if d.is_dir() and d.name != DERIVED_DIR]


def check_contract(out_root):
    """1. 契约校验：所有 xlsx 表头与模板逐列一致。"""
    bad = []
    n = 0
    for xlsx in Path(out_root).rglob("*.xlsx"):
        dim = xlsx.stem
        if dim not in DIMS:
            continue
        n += 1
        df = pd.read_excel(xlsx, dtype=str, nrows=0)
        got = list(df.columns)
        expect = schema.full_header(dim)
        if got != expect:
            bad.append({"file": str(xlsx.relative_to(out_root)),
                        "expect": expect, "got": got})
    return {"检查文件数": n, "断约": bad}, not bad


def diagnose_empty(out_root):
    """2. 空表诊断：目录 × 5 维度 矩阵。"""
    matrix, missing = {}, []
    for d in _doc_dirs(out_root):
        row = {}
        for dim in DIMS:
            f = d / f"{dim}.xlsx"
            if not f.exists():
                row[dim] = "缺表"
                missing.append(f"{d.name}/{dim}.xlsx")
            else:
                row[dim] = len(pd.read_excel(f, dtype=str))
        matrix[d.name] = row
    derived = Path(out_root) / DERIVED_DIR / "岗位竞合.xlsx"
    return {"矩阵": matrix, "缺表": missing,
            "推导表": (len(pd.read_excel(derived, dtype=str))
                       if derived.exists() else 0)}


def _source_pages(out_root, source_val):
    """数据来源《stem》 -> 源文件 pages。"""
    m = re.match(r"《(.+?)》", str(source_val or ""))
    if not m:
        return None
    stem = m.group(1)
    if "（技能结构推导）" in str(source_val):
        return None  # 推导行无单一源文档
    for ext in (".pdf", ".html", ".txt", ".docx", ".csv", ".xlsx", ".xls"):
        for p in Path("pdfs").rglob(stem + ext):
            try:
                return load_any(p)["pages"]
            except Exception:  # noqa: BLE001
                return None
    return None


def sample_accuracy(out_root, sample_n, seed=42):
    """3. 抽样回源定位：quote 在《数据来源》源文档页面中可定位 = 准确。"""
    rng = random.Random(seed)
    pool = []  # (dim, doc_dir, 行, xlsx)
    loc_cache = {}
    for d in _doc_dirs(out_root):
        for dim in DIMS:
            f = d / f"{dim}.xlsx"
            if not f.exists():
                continue
            df = pd.read_excel(f, dtype=str)
            for _, row in df.iterrows():
                if str(row.get("数据来源") or "").strip():
                    pool.append((dim, d.name, row))
    if not pool:
        return {"样本": 0}
    sample = rng.sample(pool, min(sample_n, len(pool)))
    results = defaultdict(lambda: {"n": 0, "hit": 0, "modes": defaultdict(int)})
    for dim, doc, row in sample:
        r = results[dim]
        r["n"] += 1
        src = row["数据来源"]
        if src not in loc_cache:
            pages = _source_pages(out_root, src)
            loc_cache[src] = PageLocator(pages) if pages else None
        loc = loc_cache[src]
        if loc is None:
            r["modes"]["无源文档"] += 1
            continue
        page, mode = loc.locate(str(row.get("原文支撑内容") or ""))
        if page:
            r["hit"] += 1
        r["modes"][mode] += 1
    out = {}
    total_n = total_hit = 0
    for dim, r in results.items():
        total_n += r["n"]
        total_hit += r["hit"]
        out[dim] = {"样本": r["n"], "命中": r["hit"],
                    "命中率": round(r["hit"] / r["n"], 3) if r["n"] else 0,
                    "模式": dict(r["modes"])}
    return {"样本": total_n, "命中": total_hit,
            "总命中率": round(total_hit / total_n, 3) if total_n else 0,
            "分维度": out}


def check_fragmentation(out_root, sample_n=30, seed=7):
    """4. 跨维度碎片检查：同一岗位在不同维度表中字符串是否一致。

    P2 验收口径：抽样 30 条岗位引用，跨维度零碎片（同一岗位的写法归一后
    在全部出现处一致；不一致即视为会把图谱节点拆碎）。
    """
    rng = random.Random(seed)
    # dim -> {job_name: [(doc, col)]}
    refs = defaultdict(lambda: defaultdict(list))
    for d in _doc_dirs(out_root) + [Path(out_root) / DERIVED_DIR]:
        for dim, cols in JOB_COLS.items():
            f = d / f"{dim}.xlsx"
            if not f.exists():
                continue
            df = pd.read_excel(f, dtype=str)
            for col in cols:
                if col not in df.columns:
                    continue
                for v in df[col].dropna().unique():
                    s = re.sub(r"\s+", "", str(v))
                    if s and s not in ("nan", ""):
                        refs[dim][s].append((d.name, col))
    # 岗位 -> 出现维度集合
    all_jobs = defaultdict(set)
    for dim, jobs in refs.items():
        for j in jobs:
            all_jobs[j].add(dim)
    shared = [j for j, dims in all_jobs.items() if len(dims) >= 2]

    def _base(j):
        """去职级前缀/招聘后缀后的岗位主干（写法归一基底）。"""
        s = re.sub(r"^(初级|中级|高级|技师|高级技师)[/／]?", "", j)
        s = re.sub(r"(?:20\d{2}届|\d{2}届|校招|秋招|春招|提前批|实习|校园)$", "", s)
        return s

    # 基底 -> {写法: 维度集合}；同基底多种写法即碎片
    groups = defaultdict(lambda: defaultdict(set))
    for j, dims in all_jobs.items():
        groups[_base(j)][j].update(dims)
    fragments = [{"标准名": base, "写法与维度": {w: sorted(ds) for w, ds in ws.items()}}
                 for base, ws in groups.items() if len(ws) > 1]
    sample_jobs = rng.sample(shared, min(sample_n, len(shared))) if shared else []
    return {"跨维度共享岗位数": len(shared), "抽样": len(sample_jobs),
            "碎片写法组": fragments[:20],
            "零碎片": not fragments}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="know_graph_employment")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--skip-locate", action="store_true")
    args = ap.parse_args()
    out_root = Path(args.out)

    report = {}
    contract, ok = check_contract(out_root)
    report["契约校验"] = contract
    print(f"[契约] 检查 {contract['检查文件数']} 份，断约 {len(contract['断约'])}")
    for b in contract["断约"]:
        print("  断约:", b["file"])

    report["空表诊断"] = diagnose_empty(out_root)
    empt = report["空表诊断"]
    print(f"[空表] 缺表 {len(empt['缺表'])} 份；推导竞合 {empt['推导表']} 行")

    if not args.skip_locate:
        report["准确率抽样"] = sample_accuracy(out_root, args.sample)
        acc = report["准确率抽样"]
        print(f"[准确率] 抽样 {acc['样本']} 行，回源命中 {acc['命中']}"
              f"（{acc['总命中率']:.0%}）")
        for dim, r in acc["分维度"].items():
            print(f"  {dim}: {r['命中']}/{r['样本']} = {r['命中率']:.0%} {r['模式']}")

    report["碎片检查"] = check_fragmentation(out_root, args.sample)
    frag = report["碎片检查"]
    print(f"[碎片] 跨维度共享岗位 {frag['跨维度共享岗位数']} 个，抽样 {frag['抽样']}，"
          f"零碎片={'是' if frag['零碎片'] else '否'}")
    for f in frag["碎片写法组"]:
        print("  碎片:", f["标准名"], "<>", list(f["写法与维度"].keys()))

    (out_root / "质检报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"报告已写入 {out_root / '质检报告.json'}")


if __name__ == "__main__":
    main()
