# -*- coding: utf-8 -*-
"""文档适配层：把 pdf/html/txt/md/docx/csv/xls/xlsx 统一收口为 {"kind", "pages"}。

pages = 页面文本列表，语义与 pymupdf 逐页文本一致，下游（维度抽取器、
page_locator 页码定位、落表）只依赖 pages 结构，不感知文件格式：
  - pdf           逐物理页；
  - html/txt/md   按段落边界切成 ~3000 字伪页；
  - docx          段落+表格行拼接后切伪页；
  - csv/xls/xlsx  每行一个"页"（页文本 = "列名: 值" 拼接），页号即行号，
    使 JD 结构化数据的溯源页码稳定为 R+行号。
"""
import html as _html
import re
from pathlib import Path

import pandas as pd

PAGE_CHARS = 3000
ROW_TEXT_MAX = 400

SUPPORTED_SUFFIXES = {".pdf", ".html", ".htm", ".txt", ".md",
                      ".docx", ".csv", ".xls", ".xlsx"}


def _decode(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|section|article|li|tr|h[1-6]|table|thead|tbody|ul|ol|br)[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _meta_texts(raw):
    """提取 <meta name/property="description|og:description"> 的 content。

    JS 渲染页（牛客 JD 等）正文为空，但 meta description 常含完整岗位描述，
    是静态可得的唯一文本来源。"""
    out = []
    for m in re.finditer(r"<meta\b[^>]*>", raw, re.I):
        tag = m.group(0)

        def _attr(name):
            am = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
            return am.group(1) if am else ""

        key = (_attr("name") or _attr("property")).lower()
        content = _attr("content")
        if key in ("description", "og:description") and content:
            out.append(_html.unescape(content))
    return out


def _strip_html(raw):
    """剥掉 script/style/注释与标签，块级标签转行，保留正文段落结构。

    正文过薄（JS 渲染页）时前置 meta description，避免只剩 title 残字。"""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _COMMENT_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()
    if len(re.sub(r"\s+", "", text)) < 200:
        meta = _meta_texts(raw)
        if meta:
            text = "\n".join(meta) + "\n" + text
    return text


def _pseudo_pages(text):
    """按段落边界把长文本切成 ~PAGE_CHARS 字的伪页（不在段落中间硬切，利于引文定位）。"""
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    pages, buf, size = [], [], 0
    for p in paras:
        buf.append(p)
        size += len(p)
        if size >= PAGE_CHARS:
            pages.append("\n".join(buf))
            buf, size = [], 0
    if buf:
        pages.append("\n".join(buf))
    return pages


def _row_text(row, max_len=ROW_TEXT_MAX):
    """表格一行 -> 页文本（"列名: 值"拼接），空值列跳过。"""
    parts = []
    for k, v in row.items():
        v = str(v).strip() if v is not None else ""
        if not v or v.lower() in ("nan", "none", "null"):
            continue
        parts.append(f"{k}: {v}")
    return "；".join(parts)[:max_len]


OCR_TEXT_DIR = "_ocr_text"
PDF_TEXT_MIN = 100


def _ocr_sidecar(path):
    """扫描版 PDF 回退：读取旁挂 OCR 文本（\\f 分页），页号对齐原 PDF 物理页。

    约定：OCR 产物放在 <pdfs 根>/_ocr_text/<PDF 文件名>.txt，页间以换页符分隔。"""
    p = Path(path)
    side = p.parent.parent / OCR_TEXT_DIR / f"{p.stem}.txt"
    if not side.exists():
        return None
    pages = [seg.strip() for seg in _decode(side).split("\f") if seg.strip()]
    return pages or None


def _load_pdf(path):
    import pymupdf
    pages = []
    with pymupdf.open(path) as doc:
        for page in doc:
            pages.append(page.get_text("text") or "")
    if sum(len(p.strip()) for p in pages) < PDF_TEXT_MIN:
        side = _ocr_sidecar(path)
        if side:
            return "pdf", side
    return "pdf", pages


def _load_docx(path):
    from docx import Document
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        for row in t.rows:
            line = "；".join(c.text.strip() for c in row.cells if c.text.strip())
            if line:
                parts.append(line)
    return "docx", _pseudo_pages("\n".join(parts))


def _load_table(path, kind):
    if kind == "csv":
        try:
            df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        except UnicodeDecodeError:
            df = pd.read_csv(path, encoding="gbk", dtype=str)
    else:
        df = pd.read_excel(path, dtype=str)
    df = df.fillna("")
    pages = [_row_text(r) for _, r in df.iterrows()]
    return kind, [p for p in pages if p]


def load_any(path):
    """任意支持格式 -> {"kind": 类型, "pages": [页文本, ...]}。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        kind, pages = _load_pdf(p)
    elif suffix in (".html", ".htm"):
        kind, pages = "html", _pseudo_pages(_strip_html(_decode(p)))
    elif suffix in (".txt", ".md"):
        text = _decode(p)
        if "\f" in text:
            # OCR 工具（Tesseract/Adobe 等）常用换页符分页：保留物理页边界，
            # 页号列可精确对应原 PDF 页码；无换页符则退回伪页切分。
            pages = [seg.strip() for seg in text.split("\f") if seg.strip()]
        else:
            pages = _pseudo_pages(text)
        kind, pages = suffix.lstrip("."), pages
    elif suffix == ".docx":
        kind, pages = _load_docx(p)
    elif suffix in (".csv", ".xls", ".xlsx"):
        kind, pages = _load_table(p, "csv" if suffix == ".csv" else "excel")
    else:
        raise ValueError(f"不支持的文件格式: {p.name}")
    return {"kind": kind, "pages": pages}
