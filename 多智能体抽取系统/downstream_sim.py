# -*- coding: utf-8 -*-
"""P3 下游联调模拟：按列序（非列名）读 5 表建图，验证下游兼容性。

模拟下游展示系统的读表方式——契约规定"下游建图系统按位置读列"：
  岗位聚集(0-8)   岗位名称/类别/年份/企业/城市/行业/薪资/来源/页号
  技能结构(0-11)  岗位/级别/技能/分类/等级/工具/证书/描述/难度/替代/来源/页号
  发展路径(0-9)   起始岗位/职级/目标岗位/职级/技能/难度/年限/方向/来源/页号
  培养供给链(0-9) 专业/门类/知识点/技能/院校/城市/对口岗位/匹配度/来源/页号
  岗位竞合(0-8)   岗位A/技能组A/描述A/重叠度/岗位B/技能组B/描述B/来源/页号

建图规则（节点/边）：
  岗位节点（岗位聚集+技能结构+发展路径+培养供给链+岗位竞合 全部岗位名并集）
  技能节点、专业节点；
  边：岗位-技能（技能结构）、岗位→岗位（发展路径）、专业-岗位（培养供给链）、
      岗位-岗位（岗位竞合，标注重叠度）、岗位-(城市/行业/企业)（岗位聚集）。

验收：读表无异常、节点/边规模合理、共享岗位节点在各维度间连通（无孤立碎片）。
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

OUT_ROOT = Path("know_graph_employment")
DERIVED_DIR = "_跨文档推导"


def _read_all(dim):
    """按列序读某维度全部表（跳过仅表头空表），返回 DataFrame 列表。"""
    frames = []
    for f in sorted(OUT_ROOT.rglob(f"{dim}.xlsx")):
        df = pd.read_excel(f, dtype=str).fillna("")
        if len(df):
            frames.append(df)
    return frames


def build_graph():
    nodes = {"岗位": set(), "技能": set(), "专业": set(), "城市": set(),
             "行业": set(), "企业": set()}
    edges = Counter()  # (类型, 节点A, 节点B) -> 出现次数
    per_dim_jobs = defaultdict(set)

    # 岗位聚集：列 0 岗位 / 4 城市 / 5 行业 / 3 企业
    for df in _read_all("岗位聚集"):
        for r in df.itertuples(index=False):
            job, ent, city, ind = r[0], r[3], r[4], r[5]
            nodes["岗位"].add(job)
            per_dim_jobs["岗位聚集"].add(job)
            if city:
                nodes["城市"].add(city)
                edges[("聚集-城市", job, city)] += 1
            if ind:
                nodes["行业"].add(ind)
                edges[("聚集-行业", job, ind)] += 1
            if ent and "汇总" not in ent:
                nodes["企业"].add(ent)
                edges[("聚集-企业", job, ent)] += 1

    # 技能结构：列 0 岗位 / 2 核心技能
    for df in _read_all("技能结构"):
        for r in df.itertuples(index=False):
            job, skill = r[0], r[2]
            nodes["岗位"].add(job)
            nodes["技能"].add(skill)
            per_dim_jobs["技能结构"].add(job)
            edges[("岗位-技能", job, skill)] += 1

    # 发展路径：列 0 起始岗位 / 1 起始职级 / 2 目标岗位 / 3 目标职级
    # 晋升边以（岗位·职级）复合节点表示——职业标准类文档同一岗位名跨职级晋升，
    # 若仅用岗位名建边会把合法晋升误呈现为自环
    for df in _read_all("发展路径"):
        for r in df.itertuples(index=False):
            a, la, b, lb = r[0], r[1], r[2], r[3]
            nodes["岗位"].add(a)
            nodes["岗位"].add(b)
            per_dim_jobs["发展路径"].add(a)
            per_dim_jobs["发展路径"].add(b)
            na = f"{a}·{la}" if la else a
            nb = f"{b}·{lb}" if lb else b
            if na != nb:
                edges[("晋升路径", na, nb)] += 1

    # 培养供给链：列 0 专业 / 6 对口岗位
    for df in _read_all("培养供给链"):
        for r in df.itertuples(index=False):
            major, job = r[0], r[6]
            nodes["专业"].add(major)
            per_dim_jobs["培养供给链"].add(job)
            if job:
                nodes["岗位"].add(job)
                edges[("专业-对口", major, job)] += 1

    # 岗位竞合：列 0 岗位A / 4 岗位B
    for df in _read_all("岗位竞合"):
        for r in df.itertuples(index=False):
            a, b = r[0], r[4]
            nodes["岗位"].add(a)
            nodes["岗位"].add(b)
            per_dim_jobs["岗位竞合"].add(a)
            per_dim_jobs["岗位竞合"].add(b)
            edges[("岗位竞合", a, b)] += 1

    return nodes, edges, per_dim_jobs


def connectivity(nodes, edges, per_dim_jobs):
    """共享岗位节点的跨维度连通性：技能结构岗位是否被其它维度引用。"""
    skill_jobs = per_dim_jobs.get("技能结构", set())
    linked = sum(1 for j in skill_jobs
                  if any(j in per_dim_jobs[d] for d in per_dim_jobs
                         if d != "技能结构"))
    return {"技能结构岗位数": len(skill_jobs),
            "被其它维度引用": linked,
            "跨维度引用率": round(linked / len(skill_jobs), 3) if skill_jobs else 0}


def main():
    nodes, edges, per_dim_jobs = build_graph()
    total_nodes = sum(len(s) for s in nodes.values())
    print("=" * 56)
    print("下游建图联调（按列序读 5 表）")
    print("=" * 56)
    print("节点：", {k: len(v) for k, v in nodes.items()}, f"共 {total_nodes}")
    by_type = Counter(t for (t, _, _) in edges)
    print("边：", {t: n for t, n in by_type.items()}, f"共 {sum(edges.values())}")
    conn = connectivity(nodes, edges, per_dim_jobs)
    print("连通性：", conn)
    print("\n样例边：")
    seen_types = set()
    for (t, a, b), n in edges.items():
        if t not in seen_types:
            seen_types.add(t)
            print(f"  [{t}] {a} — {b} (×{n})")
    report = {"节点": {k: len(v) for k, v in nodes.items()},
              "边": {t: n for t, n in by_type.items()},
              "总边数": sum(edges.values()),
              "连通性": conn}
    (OUT_ROOT / "下游建图联调.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {OUT_ROOT / '下游建图联调.json'}")


if __name__ == "__main__":
    main()
