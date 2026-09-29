# -*- coding: utf-8 -*-
"""
就业岗位技能图谱 5 表契约（P0 冻结）。

列序 = 数据模板列序（下游建图系统按位置读列，任何增删调序都是断约），
表尾追加一列"原文支撑内容"作为内部溯源列：LLM 每行输出原文摘录，
page_locator 用它做页码定位锚点，"页号"列由定位回填；下游可忽略尾列。

行结构（落表后）：[模板业务列..., 数据来源, 页号, 原文支撑内容]
  - 模板前 N 列与 数据模板/ 中对应模板逐列一致；
  - "数据来源"由代码统一生成：《文档名》（来源：机构），LLM 不输出该列；
  - 硬约束：每行必须有"原文支撑内容"且能在页面文本中定位，否则该行丢弃
    （字段级允许空——薪资/证书/院校等原文没有就留空，行不受影响）。
"""
import re
from pathlib import Path

# 5 表模板列序（与 数据模板/ 严格一致；"数据来源""页号"为最后两列）
TEMPLATE_HEADERS = {
    "岗位聚集": ["岗位名称", "岗位类别", "需求年份", "招聘企业", "企业所在城市",
                  "行业领域", "薪资范围", "数据来源", "页号"],
    "技能结构": ["岗位名称", "岗位级别", "核心技能", "技能分类", "技能等级要求",
                  "工具/平台", "认证证书", "技能描述", "学习难度", "替代技能",
                  "数据来源", "页号"],
    "发展路径": ["起始岗位", "起始职级", "目标岗位", "目标职级", "所需新增技能",
                  "转换难度", "平均年限", "职业方向", "数据来源", "页号"],
    "培养供给链": ["专业/课程", "学科门类", "核心知识点", "培养技能", "开设院校",
                    "院校城市", "对口岗位", "匹配度", "数据来源", "页号"],
    "岗位竞合": ["岗位A", "技能组A", "岗位A描述", "技能重叠度", "岗位B", "技能组B",
                  "岗位B描述", "数据来源", "页号"],
}
SUPPORT_COL = "原文支撑内容"


def full_header(dim):
    """落表表头 = 模板列序 + 尾部溯源列（前 N 列与模板逐列一致）。"""
    return TEMPLATE_HEADERS[dim] + [SUPPORT_COL]


def llm_header(dim):
    """LLM 输出列 = 模板业务列（去掉"数据来源""页号"）+ 溯源列。"""
    return TEMPLATE_HEADERS[dim][:-2] + [SUPPORT_COL]


# ---------------- 枚举（LLM 归类限定值，禁止自由发挥） ----------------
SKILL_CATEGORIES = [
    "编程语言", "框架与平台", "算法与模型", "硬件与电路", "数据与数据库",
    "测试与质量", "运维与安全", "工具软件", "管理与流程", "业务与领域知识", "其他",
]
SUBJECT_CATEGORIES = [
    "哲学", "经济学", "法学", "教育学", "文学", "历史学", "理学",
    "工学", "农学", "医学", "管理学", "艺术学", "军事学", "交叉学科",
]
STD_JOB_LEVELS = ["初级", "中级", "高级", "技师", "高级技师"]
DIFFICULTY_LEVELS = ["低", "中", "高"]
MATCH_LEVELS = ["高", "中", "低"]

# ---------------- 数据来源（从文件名解析，LLM 不参与） ----------------
_SOURCE_RE = re.compile(r"（来源：(.+?)）")


def parse_source_org(filename):
    m = _SOURCE_RE.search(Path(filename).stem)
    return m.group(1).strip() if m else ""


def make_source_value(filename):
    """《文档名》（来源：机构）；文件名未标注来源时留"未知机构"。

    文件名已内嵌"（来源：机构）"标注时直接用《文档名》，避免重复拼接。
    """
    stem = Path(filename).stem
    if "（来源：" in stem:
        return f"《{stem}》"
    org = parse_source_org(filename)
    return f"《{stem}》（来源：{org or '未知机构'}）"


def assemble_rows(rows, dim, source_value):
    """把 LLM 输出行重排为落表结构：[业务列..., 数据来源, 页号, 原文支撑内容]。

    输入：rows[0] 为 LLM 表头（业务列+原文支撑内容，可能已由定位器在行尾
    追加"页码"列），rows[1:] 为数据行，行结构 [业务列..., 原文支撑内容, 页号]。
    输出：rows[0] 重置为落表全表头（含"数据来源""页号"），数据行逐行重排。
    """
    n_biz = len(TEMPLATE_HEADERS[dim]) - 2
    out = [full_header(dim)]
    for r in rows[1:]:
        r = list(r)
        support = r[n_biz] if len(r) > n_biz else ""
        page = r[-1] if len(r) > n_biz + 1 else ""
        out.append(r[:n_biz] + [source_value, page, support])
    return out


def assert_header(rows, dim):
    """落表前断言表头与契约一致，不一致 fail-fast（防列序被无意改动）。"""
    expect = full_header(dim)
    got = list(rows[0]) if rows else []
    if got != expect:
        raise ValueError(f"[{dim}] 表头与契约不一致！\n  期望: {expect}\n  实际: {got}")
