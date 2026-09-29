# -*- coding: utf-8 -*-
"""原文页码定位：把抽取结果中的"原文支撑内容"映射回 page_json3 的页面序号。

数据结构背景：
  - page_json3/*.json 的 result 为页面文本列表，列表索引即页序（0-based），
    输出页码 = 索引 + 1（物理页序）；
  - mulu/*_章节匹配.json 的章节正文由 toc_extract 从页面文本顺序拼接而来，
    因此严格摘录的原文片段理论上可在页面序列中逐字命中。

定位策略（四级）：
  1. exact ：规范化（去空白）后在单页内精确子串命中；
  2. span  ：跨页命中--片段 = 页 i 的后缀 + 页 i+1 的前缀（章节拼接跨页导致）；
  3. subseq：保序子序列命中--片段的全部字符按原顺序出现在单页紧凑窗口内，
     专用于"字符串化表格单元格被其他列内容交错拆分"的场景
     （如专利名称在页面上被拆成"…方法和装 / 7发明专利ZLxxxx / 置"三行交错）；
  4. fuzzy ：LLM 轻微改写时，用 8-gram shingle 投票选候选页，
     再对候选页做滑动窗口相似度（difflib），比率≥0.80 定位、0.65~0.80 疑似改写仍给页码。

页码输出统一为单页：多处命中取"原文中最紧凑"的一页（并列取先出现）；
跨页/交错命中取片段主体（更多字符）所在页。定位失败的条目由
attach_page_column 直接丢弃，不落表。

用法：
    locator = page_locator.PageLocator(page_locator.load_pages(path))
    page, mode = locator.locate("原文片段")
    rows, stats = page_locator.attach_page_column(rows, locator, quote_idx)
"""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

_WS_RE = re.compile(r"\s+")
# 文档转文本时插入的分页标记（"第3页"/"第 3 页"），不属于正文；
# mulu 章节文本按页拼接带此标记，LLM 摘录的引用随之携带，需在比较两侧统一剔除
_PAGE_MARK_RE = re.compile(r"第\s*\d+\s*页")
# 强标识符：专利号/申请号（ZL201210126482.6 / CN200610055110.3 / CN2016/076139），在文档中唯一性强
_ID_RE = re.compile(r"(?:ZL|CN)\d{7,}(?:\.\d+)?|CN\d{4}/\d{5,}", re.IGNORECASE)

DEFAULT_PAGE_JSON_DIR = Path("./page_json3")


def normalize_text(s):
    """去除全部空白字符与"第N页"分页标记，用于宽松逐字比较
    （原文可能因换行/空格被 LLM 保留，章节文本可能携带页码标记）。"""
    return _WS_RE.sub("", _PAGE_MARK_RE.sub("", s or ""))


def resolve_page_json(source_name, page_json_dir=DEFAULT_PAGE_JSON_DIR):
    """mulu 文件 stem（含 _章节匹配 后缀）-> 对应 page_json3 文件路径。

    文件名可能含不可见字符，直接拼接路径失败时依次退化为：
    目录扫描按 stem 精确相等匹配 -> 按"文件名包含给定名"模糊匹配
    （允许传入"烽火"这类简称，对应"烽火通信科技集团….json"）。
    """
    stem = source_name.replace("_章节匹配", "")
    d = Path(page_json_dir)
    if not d.exists():
        return None
    p = d / (stem + ".json")
    if p.exists():
        return p
    files = list(d.glob("*.json"))
    for f in files:
        if f.stem == stem:
            return f
    hits = [f for f in files if stem in f.stem]
    if len(hits) == 1:
        return hits[0]
    return None


def load_pages(page_json_path):
    """读取页面文本列表；异常或结构不符返回空列表。"""
    if not page_json_path:
        return []
    try:
        data = json.loads(Path(page_json_path).read_text(encoding="utf-8"))
        pages = data.get("result", [])
        return [p if isinstance(p, str) else "" for p in pages]
    except Exception:
        return []


class PageLocator:
    """页面序列上的原文片段定位器。"""

    def __init__(self, pages):
        self.pages = list(pages or [])
        self.norm = [normalize_text(p) for p in self.pages]

    # ---------- 对外主入口 ----------
    def locate(self, quote):
        """定位原文片段。

        返回 (页码字符串, 模式)。页码为 1-based，任何命中均只输出一个页码：
          多页精确命中 -> 取原文中最紧凑的一页（并列取先出现）；
          跨页/交错命中 -> 取片段主体（更多字符）所在页；找不到返回 ""。
        模式: exact / multi / patent_id / span / subseq / fuzzy / fuzzy_low / none / short
        """
        q = normalize_text(quote)
        if len(q) < 5:
            return "", "short"
        hits = [i for i, t in enumerate(self.norm) if q in t]
        if len(hits) == 1:
            return str(hits[0] + 1), "exact"
        if len(hits) > 1:
            return self._best_hit_page(q, hits), "multi"
        page = self._locate_id(q)
        if page is not None:
            return page, "patent_id"
        page = self._locate_span(q)
        if page is not None:
            return page, "span"
        page = self._locate_subseq(q)
        if page is not None:
            return page, "subseq"
        page, ratio = self._locate_fuzzy(q)
        if page is not None and ratio >= 0.80:
            return page, "fuzzy"
        if page is not None and ratio >= 0.65:
            return page, "fuzzy_low"
        return "", "none"

    # ---------- 多处命中择优 ----------
    def _best_hit_page(self, q, hits):
        """多页均含该片段时，选原文中最紧凑的一页（并列取先出现的页）。

        成因：同一句话在文档多个章节真实重复（正文/综述各一次），均为精确
        命中、相似度并列；用"容空白匹配的最小窗口跨度"打分可优先选出
        连续摘录（≈1.0）而非被换行打散的一页（略低）。
        """
        best_page, best_score = hits[0], -1.0
        for i in hits:
            score = self._raw_match_score(q, i)
            if score > best_score:
                best_page, best_score = i, score
        return str(best_page + 1)

    def _raw_match_score(self, q, i, max_tries=200):
        """页 i 原始文本中容空白匹配 q 的最紧凑窗口得分 = len(q)/跨度，∈(0,1]。"""
        text = self.pages[i]
        best = 0.0
        pos, tries = 0, 0
        while tries < max_tries:
            tries += 1
            start = text.find(q[0], pos)
            if start < 0:
                break
            pos = start + 1
            j, k = start + 1, 1
            nq = len(q)
            while k < nq and j < len(text):
                c = text[j]
                if c == q[k]:
                    k += 1
                elif not c.isspace():
                    k = -1
                    break
                j += 1
            if k == nq:
                score = nq / (j - start)
                if score > best:
                    best = score
        return best

    # ---------- 强标识符（专利号等） ----------
    def _locate_id(self, q):
        """引文中含专利号/申请号（ZL/CN 开头）且该号在文档中唯一出现时，直接返回所在页。

        LLM 常把"名称+类型+专利号"拼接为引文（三者原文不连续），此时精确/子序列
        均会失败，但专利号本身唯一性强，可直接锚定页码。
        """
        ids = list(dict.fromkeys(m.group(0).upper() for m in _ID_RE.finditer(q)))
        for pid in ids:
            hits = [i for i, t in enumerate(self.norm) if pid in t.upper()]
            if len(hits) == 1:
                return str(hits[0] + 1)
        return None

    # ---------- 跨页 ----------
    def _locate_span(self, q):
        """片段由页 i 后缀 + 页 i+1 前缀组成时，返回主体（更多字符）所在页。"""
        for i in range(len(self.norm) - 1):
            a, b = self.norm[i], self.norm[i + 1]
            if not a or not b:
                continue
            kmax = min(len(q) - 8, len(a))
            kmin = max(8, kmax - 150)
            for k in range(kmin, kmax + 1):
                if a.endswith(q[:k]) and b.startswith(q[k:]):
                    return str(i + 1) if k >= len(q) - k else str(i + 2)
        return None

    # ---------- 保序子序列（交错表格） ----------
    @staticmethod
    def _match_from(page, start, q):
        """从 page[start] 起两指针贪心匹配 q；全部命中返回终点索引，否则 None。"""
        i, j, n, m = start, 0, len(page), len(q)
        while i < n and j < m:
            if page[i] == q[j]:
                j += 1
            i += 1
        return i - 1 if j == m else None

    def _locate_subseq(self, q, min_len=8):
        """q 的全部字符按顺序出现在紧凑窗口内（跨度<=3倍长度+40）时返回页码。

        用于字符串化表格：单元格内容被其他列（专利号/日期等）交错拆分，
        LLM 还原出的完整名称不是连续子串，但字符保持原顺序。
        min_len=8：容纳"专业名+数字"类短引文（如"电子科学与技术7752"，
        原文为"电子科学与技术以7752元"，被虚词隔断 11 字符）。
        同时尝试"页i"与"页i+页i+1拼接"两种文本，覆盖跨页列表
        （如页尾"如华为、中兴、"+页首"烽火、诺基亚…"）；拼接命中时
        返回匹配字符主体所在页（并列取前页）。
        """
        if len(q) < min_len:
            return None
        max_span = 3 * len(q) + 40

        def _find_in(txt):
            pos, tries = 0, 0
            while tries < 60:
                tries += 1
                start = txt.find(q[0], pos)
                if start < 0:
                    return None
                pos = start + 1
                end = self._match_from(txt, start, q)
                if end is not None and end - start + 1 <= max_span:
                    return start, end
            return None

        # 8-gram 无命中（引文被虚词/换行打散，如"专业名以7752元"类新闻体）
        # 时退化为全页顺序扫描，保证短引文的 subseq 兜底可达
        cand = self._candidate_pages(q, top=5) or list(range(len(self.norm)))
        for i in cand:
            if _find_in(self.norm[i]) is not None:
                return str(i + 1)
            if i + 1 < len(self.norm):
                len_i = len(self.norm[i])
                hit = _find_in(self.norm[i] + self.norm[i + 1])
                if hit is not None:
                    start, end = hit
                    n_first = max(0, min(end, len_i - 1) - start + 1)
                    n_second = max(0, end - max(start, len_i) + 1)
                    return str(i + 1) if n_first >= n_second else str(i + 2)
        return None

    # ---------- 模糊 ----------
    def _candidate_pages(self, q, top=3):
        """8-gram shingle 投票：出现在哪些页。"""
        votes = {}
        step = 8
        for off in range(0, max(1, len(q) - 8), step):
            g = q[off:off + 8]
            if len(g) < 8:
                break
            for i, t in enumerate(self.norm):
                if g in t:
                    votes[i] = votes.get(i, 0) + 1
        return [i for i, _ in sorted(votes.items(), key=lambda kv: -kv[1])[:top]]

    def _window_ratio(self, q, page):
        """页内滑动窗口与片段的最佳相似度。"""
        L = len(q)
        if len(page) <= L + 40:
            return SequenceMatcher(None, q, page, autojunk=False).ratio()
        best = 0.0
        step = max(30, L // 3)
        for start in range(0, len(page) - 40, step):
            win = page[start:start + L + 40]
            r = SequenceMatcher(None, q, win, autojunk=False).quick_ratio()
            if r > best:
                r2 = SequenceMatcher(None, q, win, autojunk=False).ratio()
                if r2 > best:
                    best = r2
        return best

    def _locate_fuzzy(self, q):
        best_page, best_ratio = None, 0.0
        for i in self._candidate_pages(q):
            r = self._window_ratio(q, self.norm[i])
            if r > best_ratio:
                best_page, best_ratio = i, r
        if best_page is None:
            return None, 0.0
        return str(best_page + 1), best_ratio


def attach_page_column(rows, locator, quote_idx, col_name="页码"):
    """给 markdown_table_to_rows 的结果（[表头]+数据行）追加页码列。

    页码定位失败的行（引文过短/无法匹配/无 page_json）直接丢弃，
    保证落表条目页码可靠；所有命中均只输出一个页码（多处命中择优、
    跨页/交错取主体页）。返回 (新 rows, 统计 dict)。
    """
    header = rows[0] + [col_name]
    out = [header]
    stats = {}
    for r in rows[1:]:
        quote = r[quote_idx] if len(r) > quote_idx else ""
        if locator is None:
            page, mode = "", "no_locator"
        else:
            page, mode = locator.locate(quote)
        stats[mode] = stats.get(mode, 0) + 1
        if not page:
            continue
        out.append(r + [page])
    return out, stats


def stats_summary(stats):
    """统计转一行可读文本。"""
    exact = stats.get("exact", 0)
    multi = stats.get("multi", 0)
    span = stats.get("span", 0)
    subseq = stats.get("subseq", 0)
    pid = stats.get("patent_id", 0)
    fuzzy = stats.get("fuzzy", 0)
    fuzzy_low = stats.get("fuzzy_low", 0)
    bad = stats.get("none", 0) + stats.get("short", 0) + stats.get("no_locator", 0)
    total = sum(stats.values())
    ok = exact + multi + span + subseq + pid + fuzzy + fuzzy_low
    rate = f"{ok}/{total}={ok/total:.0%}" if total else "-"
    return (f"定位率 {rate} | 精确{exact} 多处择优{multi} 跨页{span} 交错{subseq} 专利号{pid} "
            f"模糊{fuzzy} 疑似改写{fuzzy_low} 未定位并丢弃{bad}")


def dedup_rows(rows, key_cols, quote_idx, min_quote=4):
    """按实体字段键去重 + 丢弃空引文行（防 LLM 行级复读退化）。

    背景：LLM 偶发"行级重复+渐进漂移"退化（同一实体对反复输出、引文逐行微改），
    或把一句话里的多家企业拆成多行后引文完全相同。二者都不该进知识图谱：
      - 实体键（如 企业1+关系+企业2）完全相同的行只保留第一条；
      - 引文为空/过短的行直接丢弃（多为截断残行）。
    同一句原文支撑多家不同实体（键不同）的合法拆分行不受影响。

    参数：
        rows      : markdown_table_to_rows 的输出（首行为表头）
        key_cols  : 实体键所在列索引元组，如竞合维度 (0, 3, 4)
        quote_idx : 原文支撑内容列索引
    返回：(新 rows, 丢弃条数)
    """
    if not rows:
        return rows, 0
    header, data = rows[0], rows[1:]
    ncol = len(header)
    out, seen, dropped = [], set(), 0
    for r in data:
        r = list(r) + [""] * (ncol - len(r))  # 补齐截断行
        q = normalize_text(r[quote_idx])
        if len(q) < min_quote:
            dropped += 1
            continue
        key = tuple(str(r[c]) for c in key_cols)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(r)
    return [header] + out, dropped
