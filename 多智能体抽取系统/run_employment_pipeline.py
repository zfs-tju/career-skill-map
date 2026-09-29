# -*- coding: utf-8 -*-
"""就业职业岗位技能图谱抽取统一入口。

流程：扫描 pdfs/ -> classify 路由（JD CSV / 试点报告 / 职业标准 / 跳过）
-> 通道分发（聚合项顺序执行，职业标准文档并发）-> 抽取汇总.json。
"""
import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from doc_loader import SUPPORTED_SUFFIXES, load_any
from gangju_extract import _read_csv, extract_from_jd_csv, extract_from_report
from gangwei_extract import (
    CH_REQ_RE, MAX_INPUT_CHARS, compute_ability_slices, extract_chapter,
    extract_job_name, join_pages, scan_direction_names,
)
from fazhan_extract import extract_apply_section, extract_development_path
from jineng_extract import extract_skill_structure
from peiyang_extract import extract_supply_chain

DEFAULT_INPUT_DIR = Path("./pdfs")
DEFAULT_OUT_DIR = Path("./know_graph_employment")
JD_CSV_NAME = "岗位技能表.csv"
DOC_WORKERS = 2

# 试点报告关键词组合白名单（避免同名冲突误收）
PILOT_REPORTS = [
    ("薪酬数据报告", "三季度）全文"),
    ("最缺工100个职业排行", "完整名单附件"),
]
# 培养供给链类报告白名单（专业×就业数据）
EDU_REPORTS = [
    ("麦可思",),
    ("本科毕业生起薪",),
    ("就业前景研判",),
    ("新能源行业大学生就业趋势",),
]
EXCLUDED_DIR_PARTS = {"05_企业报告", "eg人工智能", "data_extract"}

_SKIP_NOTE = {
    "skip": "试点范围外（P2 扩维度时接入）",
    "no_text": "无文本层，跳过",
    "error": "读取失败",
}


def is_pilot_report(stem):
    return any(all(k in stem for k in keys) for keys in PILOT_REPORTS)


def is_edu_report(stem):
    return any(all(k in stem for k in keys) for keys in EDU_REPORTS)


def _year_from_name(name):
    m = re.search(r"20\d{2}", Path(name).stem)
    return m.group(0) if m else ""


def classify(path):
    """路由一个文档 -> (kind, info)。kind ∈ {skip, jd_csv, no_text, error,
    report_agg, standard}。"""
    if set(path.parts) & EXCLUDED_DIR_PARTS:
        return "skip", "目录排除"
    if path.name == JD_CSV_NAME:
        return "jd_csv", None
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return "skip", f"后缀不支持：{path.suffix}"
    try:
        loaded = load_any(path)
    except Exception as exc:  # noqa: BLE001
        return "error", str(exc)
    text = join_pages(loaded["pages"], MAX_INPUT_CHARS)
    non_ws = len(re.sub(r"\s+", "", text))
    if non_ws < 100:
        return "no_text", f"非空白字符 {non_ws}"
    if is_pilot_report(path.stem):
        return "report_agg", {"text_len": non_ws, "year_hint": _year_from_name(path.name)}
    if is_edu_report(path.stem):
        return "edu_report", {"text_len": non_ws}
    chapter = extract_chapter(text)
    if ("职业编码" in text and CH_REQ_RE.search(text) and chapter
            and len(re.sub(r"\s+", "", chapter)) > 300):
        slices = compute_ability_slices(chapter, scan_direction_names(chapter))
        job = extract_job_name(text)
        if not job or len(job) < 4 or not re.search(r"[\u4e00-\u9fff]", job):
            job = "（封面抽取失败，回退文件名）"
        has_apply = extract_apply_section(text) is not None
        info = {
            "job": job,
            "chapter_len": len(re.sub(r"\s+", "", chapter)),
            "has_apply": has_apply,
            "slices": [
                {"direction": s.get("direction", ""),
                 "level": s.get("level") or "LLM判断",
                 "chars": len(s["text"]),
                 "preview": re.sub(r"\s+", " ", s["text"])[:60]}
                for s in slices
            ],
        }
        return "standard", info
    return "skip", "非职业标准（未匹配“职业编码”+“工作要求”章节）"


# ============================================================
# dry-run：只打印计划，不调 LLM、不写文件
# ============================================================
def _print_dry_run(items):
    print("=" * 60)
    print("dry-run 抽取计划（不调用 LLM、不产出文件）")
    print("=" * 60)
    for it in items:
        p, kind, info = it["path"], it["kind"], it["info"]
        print(f"\n[{kind}] {p.name}")
        if kind == "jd_csv":
            try:
                df = _read_csv(p)
                comps = df["公司"].nunique() if "公司" in df.columns else "?"
                print(f"  通道A：岗位技能表 CSV {len(df)} 行 / {comps} 家公司"
                      f" -> 规则直通 + 行业LLM归一 -> 岗位聚集.xlsx")
            except Exception as exc:  # noqa: BLE001
                print(f"  CSV 读取失败：{exc}")
        elif kind == "report_agg":
            print(f"  通道B：报告 LLM 抽取（文本 {info['text_len']} 字，"
                  f"年份提示 {info['year_hint'] or '无'}）-> 岗位聚集.xlsx")
        elif kind == "edu_report":
            print(f"  通道D：培养供给链 LLM 抽取（文本 {info['text_len']} 字）"
                  f" -> 培养供给链.xlsx")
        elif kind == "standard":
            print(f"  通道C：技能结构抽取（岗位：{info['job']}，"
                  f"工作要求章节 {info['chapter_len']} 字，{len(info['slices'])} 片）"
                  f" + 发展路径（申报条件：{'有' if info['has_apply'] else '无'}）")
            for i, s in enumerate(info["slices"], 1):
                print(f"    片{i} 方向[{s['direction'] or '通用'}] "
                      f"等级[{s['level']}] {s['chars']}字 | {s['preview']}…")
        else:
            note = _SKIP_NOTE.get(kind, kind)
            print(f"  {note}" + (f"：{info}" if info else ""))


# ============================================================
# 执行
# ============================================================
def run_pipeline(items, out_dir, args, input_desc):
    run_agg = args.dims in ("all", "agg")
    run_skill = args.dims in ("all", "skill")
    run_edu = args.dims in ("all", "edu")
    details = []
    t0 = time.time()

    for it in items:  # 未执行项也记入明细
        kind = it["kind"]
        if kind in ("skip", "no_text", "error"):
            note = _SKIP_NOTE[kind] + (f"：{it['info']}" if it["info"] else "")
            details.append({"doc": it["path"].name, "type": kind, "dim": "-",
                            "rows": 0, "note": note})
        elif kind in ("jd_csv", "report_agg") and not run_agg:
            details.append({"doc": it["path"].name, "type": kind, "dim": "-",
                            "rows": 0, "note": f"本次未执行（--dims={args.dims}）"})
        elif kind == "standard" and not run_skill:
            details.append({"doc": it["path"].name, "type": kind, "dim": "-",
                            "rows": 0, "note": f"本次未执行（--dims={args.dims}）"})
        elif kind == "edu_report" and not run_edu:
            details.append({"doc": it["path"].name, "type": kind, "dim": "-",
                            "rows": 0, "note": f"本次未执行（--dims={args.dims}）"})

    agg_items = [it for it in items
                 if it["kind"] in ("jd_csv", "report_agg") and run_agg]
    std_items = [it for it in items if it["kind"] == "standard" and run_skill]
    edu_items = [it for it in items if it["kind"] == "edu_report" and run_edu]

    for it in agg_items:
        p = it["path"]
        try:
            sub = ("岗位技能表" if it["kind"] == "jd_csv"
                   else p.stem.rstrip(" ."))
            done = out_dir / sub / "岗位聚集.xlsx"
            if done.exists() and not args.force:
                details.append({"doc": p.name, "type": it["kind"], "dim": "岗位聚集",
                                "rows": _existing_rows(done),
                                "note": "已存在，跳过（断点续跑；--force 重跑）"})
                continue
            if it["kind"] == "jd_csv":
                res = extract_from_jd_csv(p, out_dir / "岗位技能表",
                                          workers=args.llm_workers)
            else:
                res = extract_from_report(p, out_dir / p.stem.rstrip(" ."),
                                          workers=args.llm_workers)
            details.append({"doc": p.name, "type": it["kind"], **res})
            print(f"[岗位聚集] {p.name} -> {res.get('rows', 0)} 行"
                  + (f"（{res['note']}）" if res.get("note") else ""))
        except Exception as exc:  # noqa: BLE001
            details.append({"doc": p.name, "type": it["kind"], "dim": "岗位聚集",
                            "rows": 0, "error": str(exc)})
            print(f"[岗位聚集] {p.name} -> 失败：{exc}")

    def _process_standard(p, doc_out, workers, force):
        """单文档：技能结构（切片LLM）+ 发展路径（申报条件LLM），已有产出跳过。"""
        results = []
        f_skill = doc_out / "技能结构.xlsx"
        if f_skill.exists() and not force:
            results.append({"dim": "技能结构", "doc": p.name,
                            "rows": _existing_rows(f_skill),
                            "note": "已存在，跳过（断点续跑）"})
        else:
            results.append(extract_skill_structure(p, doc_out, workers))
        f_path = doc_out / "发展路径.xlsx"
        if f_path.exists() and not force:
            results.append({"dim": "发展路径", "doc": p.name,
                            "rows": _existing_rows(f_path),
                            "note": "已存在，跳过（断点续跑）"})
        else:
            results.append(extract_development_path(p, doc_out, workers))
        return results

    if std_items:
        print(f"\n技能结构+发展路径：{len(std_items)} 份职业标准文档，"
              f"文档并发 {args.doc_workers} × LLM 并发 {args.llm_workers}")
        with ThreadPoolExecutor(max_workers=args.doc_workers) as pool:
            futs = {pool.submit(_process_standard, it["path"],
                                out_dir / it["path"].stem.rstrip(" ."),
                                args.llm_workers, args.force): it for it in std_items}
            for fut in tqdm(as_completed(futs), total=len(futs),
                            desc="职业标准文档", ncols=100):
                it = futs[fut]
                try:
                    for res in fut.result():
                        details.append({"doc": it["path"].name,
                                        "type": it["kind"], **res})
                except Exception as exc:  # noqa: BLE001
                    details.append({"doc": it["path"].name, "type": "standard",
                                    "dim": "-", "rows": 0,
                                    "error": str(exc)})

    if run_skill:  # 岗位竞合：技能结构表规则推导（不调 LLM）
        try:
            from jinghe_jobs_extract import derive_jinghe
            res = derive_jinghe(out_dir)
            details.append({"doc": res["doc"], "type": "derived", **res})
            print(f"[岗位竞合] 技能结构推导 -> {res.get('rows', 0)} 行")
        except Exception as exc:  # noqa: BLE001
            details.append({"doc": "-", "type": "derived", "dim": "岗位竞合",
                            "rows": 0, "error": str(exc)})
            print(f"[岗位竞合] 推导失败：{exc}")

    if edu_items:
        print(f"\n培养供给链：{len(edu_items)} 份专业/就业报告")
        for it in edu_items:
            p = it["path"]
            try:
                done = out_dir / p.stem.rstrip(" .") / "培养供给链.xlsx"
                if done.exists() and not args.force:
                    details.append({"doc": p.name, "type": it["kind"],
                                    "dim": "培养供给链",
                                    "rows": _existing_rows(done),
                                    "note": "已存在，跳过（断点续跑）"})
                    continue
                res = extract_supply_chain(p, out_dir / p.stem.rstrip(" ."),
                                           workers=args.llm_workers)
                details.append({"doc": p.name, "type": it["kind"], **res})
                print(f"[培养供给链] {p.name} -> {res.get('rows', 0)} 行"
                      + (f"（{res['note']}）" if res.get("note") else ""))
            except Exception as exc:  # noqa: BLE001
                details.append({"doc": p.name, "type": it["kind"],
                                "dim": "培养供给链", "rows": 0, "error": str(exc)})
                print(f"[培养供给链] {p.name} -> 失败：{exc}")

    if args.coverage:
        run_coverage(items, out_dir, args, details)

    # 实体对齐层 + 每源 5 表齐备（设计方案 P2 收尾）
    try:
        from entity_normalizer import apply_normalization, ensure_all_dim_tables
        apply_normalization(out_dir)
        ensure_all_dim_tables(out_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[对齐层] 失败：{exc}")

    summary = _build_summary(details, out_dir, time.time() - t0, input_desc, items)
    print("\n" + "=" * 60)
    print("抽取汇总")
    print("=" * 60)
    print(f"输入：{input_desc}")
    cat = summary["文档分类"]
    print("文档分类：" + "，".join(f"{k}={v}" for k, v in cat.items()))
    if summary["维度产出"]:
        for dim, st in summary["维度产出"].items():
            print(f"  {dim}：{st['docs']} 份文档，{st['rows']} 行")
    else:
        print("维度产出：无")
    print(f"耗时 {summary['耗时秒']}s；明细见 {out_dir / '抽取汇总.json'}")
    return summary


def run_coverage(items, out_dir, args, details):
    """全覆盖通道（--coverage）：每个数据文件 × 各维度逐一尝试。

    打破"源类型->维度"路由：任何数据文件都过一遍 4 个 LLM 维度提取器
    （岗位竞合为全局规则推导，天然覆盖全部文件的技能结构结果，不单跑）。
    数据文件 = 位于 01_~06_ 数据目录下的文件（按目录部件匹配，
    排除 ocr_texts/ocr_work/data_extract 等中间产物目录）+ 白名单报告；
    无文本层/读取失败/JD 聚合 CSV/后缀不支持 不进本通道。
    原文确无某维度事实时提取器优雅返回 0 行；引文回源定位校验兜底，
    定位不到的编造行会被丢弃。已有产出的 文档×维度 跳过（断点续跑）。
    """
    data_prefixes = ("01_", "02_", "03_", "04_", "05_", "06_")
    entries = []
    for it in items:
        k = it["kind"]
        if k in ("no_text", "error", "jd_csv"):
            continue
        if it["path"].suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        in_data_dir = any(str(part).startswith(data_prefixes)
                          for part in it["path"].parts[:-1])
        if k in ("report_agg", "standard", "edu_report") or in_data_dir:
            entries.append(it)
    if not entries:
        print("\n全覆盖通道：无待处理文档")
        return
    print(f"\n全覆盖通道：{len(entries)} 份文档 × [岗位聚集/发展路径/"
          f"培养供给链/技能结构]（文档并发 {args.doc_workers} × "
          f"LLM 并发 {args.llm_workers}）")

    def _one(it):
        p = it["path"]
        doc_out = out_dir / p.stem.rstrip(" .")
        results = []
        for dim, fn in (("岗位聚集", extract_from_report),
                        ("发展路径", extract_development_path),
                        ("培养供给链", extract_supply_chain),
                        ("技能结构", extract_skill_structure)):
            f = doc_out / f"{dim}.xlsx"
            n = _existing_rows(f) if f.exists() else 0
            if n > 0 and not args.force:
                results.append({"dim": dim, "doc": p.name, "type": "coverage",
                                "rows": 0,
                                "note": f"已有产出 {n} 行，跳过（断点续跑）"})
                continue
            try:
                r = fn(p, doc_out, workers=args.llm_workers)
            except Exception as exc:  # noqa: BLE001
                r = {"dim": dim, "doc": p.name, "rows": 0, "error": str(exc)}
            r["type"] = "coverage"
            results.append(r)
        return results

    with ThreadPoolExecutor(max_workers=args.doc_workers) as pool:
        futs = {pool.submit(_one, it): it for it in entries}
        for fut in tqdm(as_completed(futs), total=len(futs),
                        desc="全覆盖抽取", ncols=100):
            it = futs[fut]
            try:
                for r in fut.result():
                    details.append(r)
                got = [r for r in details[-4:]]
                print(f"[覆盖] {it['path'].name}: "
                      + "，".join(f"{r['dim']}={r.get('rows', 0)}" for r in got))
            except Exception as exc:  # noqa: BLE001
                details.append({"doc": it["path"].name, "type": "coverage",
                                "dim": "-", "rows": 0, "error": str(exc)})
                print(f"[覆盖] {it['path'].name} -> 失败：{exc}")


def _existing_rows(f):
    """已落表文件的数据行数（断点续跑时统计用）。"""
    try:
        import pandas as pd
        return len(pd.read_excel(f, dtype=str))
    except Exception:  # noqa: BLE001
        return 0


def _build_summary(details, out_dir, elapsed, input_desc, items):
    cat = {}
    for it in items:
        cat[it["kind"]] = cat.get(it["kind"], 0) + 1
    dims = {}
    for d in details:
        if d.get("rows"):
            dims.setdefault(d.get("dim", "-"), {"docs": 0, "rows": 0})
            dims[d["dim"]]["docs"] += 1
            dims[d["dim"]]["rows"] += d["rows"]
    return {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "输入": input_desc,
        "文档分类": cat,
        "维度产出": dims,
        "明细": details,
        "耗时秒": round(elapsed, 1),
    }


def parse_args():
    ap = argparse.ArgumentParser(
        description="就业职业岗位技能图谱抽取（5 维度：岗位聚集/技能结构/发展路径/"
                    "培养供给链/岗位竞合）")
    ap.add_argument("--dir", help="输入目录（默认 ./pdfs）")
    ap.add_argument("--pdf", help="单个文档路径（优先于 --dir）")
    ap.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="输出目录")
    ap.add_argument("--dims", choices=["all", "agg", "skill", "edu"], default="all",
                    help="跑哪些维度（all=全部；skill 含发展路径与岗位竞合推导）")
    ap.add_argument("--doc-workers", type=int, default=DOC_WORKERS,
                    help="职业标准文档并发数")
    ap.add_argument("--llm-workers", type=int, default=8, help="LLM 并发数")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印抽取计划，不调用 LLM、不产出文件")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有产出强制重抽（默认断点续跑）")
    ap.add_argument("--coverage", action="store_true",
                    help="全覆盖模式：每个数据文件×各维度逐一尝试"
                         "（打破源类型路由，无对应结构的文档返回 0 行）")
    return ap.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.out).resolve()
    if args.pdf:
        paths = [Path(args.pdf).resolve()]
        input_desc = str(paths[0])
    else:
        root = (Path(args.dir).resolve() if args.dir
                else DEFAULT_INPUT_DIR.resolve())
        paths = sorted(p for p in root.rglob("*") if p.is_file())
        input_desc = str(root)
    if not paths:
        print(f"输入为空：{input_desc}")
        return
    items = [{"path": p, "kind": k, "info": i}
             for p in paths for k, i in (classify(p),)]
    if args.dry_run:
        _print_dry_run(items)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = run_pipeline(items, out_dir, args, input_desc)
    (out_dir / "抽取汇总.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
