"""
知识图谱展示系统 - Flask后端
功能：读取Excel文件、SQLite持久化、节点/边CRUD操作
"""

import os
import json
import hashlib
import re
import uuid
import shutil
import sqlite3
import time
import threading
import traceback
import html as html_module
from datetime import datetime
import requests
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response, session
from openpyxl import Workbook, load_workbook
import db as db_module

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# 会话签名密钥（本地单机部署，固定密钥即可；重启后会话仍有效）
app.secret_key = 'kg-graph-system-local-secret-2026'

# 默认图谱文件根目录（原始资料库）
GRAPH_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'raw')
# 上传的图谱文件夹目录（根=公共资料库；users/<uid>/=各用户个人资料区）
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'graphs')
PERSONAL_UPLOAD_DIR = os.path.join(UPLOAD_DIR, 'users')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 当前激活的文件夹路径：按用户隔离（登录用户与游客互不干扰）
_user_current_folder = {}
_FOLDER_KEY_ANON = '__anon__'

# OCR 引擎懒加载单例（图片通道）
_ocr_engine = None
# 待确认的图片材料：token -> 原图临时路径（确认落盘时拷入文件夹供溯源，未确认定时清理）
_PENDING_IMAGES = {}


def _folder_key():
    uid = session.get('user_id')
    return f'u{uid}' if uid else _FOLDER_KEY_ANON


def _get_current_folder():
    return _user_current_folder.get(_folder_key())


def _set_current_folder(path):
    _user_current_folder[_folder_key()] = path


def _current_user():
    """当前登录用户（会话），未登录返回 None"""
    uid = session.get('user_id')
    if not uid:
        return None
    return db_module.get_user_by_id(uid)


def _current_profile_db():
    """当前用户的画像库路径：登录→个人库，游客→全局库"""
    return db_module.get_profile_db_path(session.get('user_id'))


def _user_upload_dir(user_id):
    """用户个人资料区根目录"""
    return os.path.join(PERSONAL_UPLOAD_DIR, f'u{user_id}')


def _is_allowed_folder(path):
    """文件夹选择白名单：仅允许 data/raw/ 与 data/graphs/（含个人区）内的目录"""
    real = os.path.realpath(path)
    for root in (GRAPH_BASE_DIR, UPLOAD_DIR):
        if real.startswith(os.path.realpath(root) + os.sep):
            return True
    return False


def _get_ocr():
    """懒加载 RapidOCR 单例（首次调用会加载模型，约数秒）"""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine

# 六个维度的文件名映射
DIMENSION_FILES = {
    '岗位聚集': '岗位聚集.xlsx',
    '技能结构': '技能结构.xlsx',
    '发展路径': '发展路径.xlsx',
    '课程体系': '课程体系.xlsx',
    '岗位竞合': '岗位竞合.xlsx',
    '竞赛实践': '竞赛实践.xlsx',
}

# 维度中文名 -> 英文key
DIMENSION_KEYS = {
    '岗位聚集': 'job_cluster',
    '技能结构': 'skill_structure',
    '发展路径': 'career_path',
    '课程体系': 'course_system',
    '岗位竞合': 'job_coopetition',
    '竞赛实践': 'competition_practice',
}

# 网页导入通道（A/B）与AI智能分析共用：天大校园LLM端点（tju-llm/qwen3.6，vLLM后端）
LLM_API_URL = 'https://ai.tju.edu.cn/api/v3/chat/completions'

def _load_llm_key():
    """API Key 不入库：优先环境变量 TUJU_API_KEY，其次根目录 api_key.txt（已被 .gitignore 排除）"""
    key = os.environ.get('TUJU_API_KEY', '').strip()
    if key:
        return key
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api_key.txt')
    if os.path.exists(key_file):
        with open(key_file, 'r', encoding='utf-8-sig') as f:
            return f.read().strip()
    return ''

LLM_API_KEY = _load_llm_key()
LLM_MODEL = 'tju-llm'
WEB_EXTRACT_MAX_CHARS = 30000
WEB_TABLE_HEADERS = {
    '岗位聚集': ['岗位名称', '岗位类别', '需求年份', '招聘企业', '企业所在城市', '行业领域', '薪资范围', '数据来源', '页号'],
    '技能结构': ['岗位名称', '岗位级别', '核心技能', '技能分类', '技能等级要求', '工具/平台', '认证证书', '技能描述', '学习难度', '替代技能', '数据来源', '页号'],
    '发展路径': ['起始岗位', '起始职级', '目标岗位', '目标职级', '所需新增技能', '转换难度', '平均年限', '职业方向', '数据来源', '页号'],
    # 课程体系末列"学分"为P0-1新增（索引11），历史11列Excel可兼容（缺失按空处理）
    '课程体系': ['专业名称', '学科门类', '课程名称', '课程性质', '开课学年', '开课学期', '核心知识点', '培养技能', '开设学院', '数据来源', '页号', '学分'],
    '岗位竞合': ['岗位A', '技能组A', '岗位A描述', '技能重叠度', '岗位B', '技能组B', '岗位B描述', '数据来源', '页号'],
    '竞赛实践': ['竞赛名称', '竞赛级别', '主办单位', '时间节点', '面向专业', '锻炼技能', '竞赛描述', '数据来源', '页号'],
}

# ============ P0-1：四类核心文档抽取模板（宁空勿滥，文档里没有的维度绝不强行抽取） ============
DOC_TYPE_TEMPLATES = {
    'standard': {
        'label': '职业标准类',
        'focus': '岗位分级与技能要求（国家职业技能标准、行业任职规范）',
        'dim_hints': {
            '岗位聚集': '抽取标准中出现的职业/工种全称，行业领域填标准所属职业门类',
            '技能结构': '重点表：岗位名称、岗位级别、核心技能、技能分类、技能等级要求、认证证书逐项对应抽取',
            '发展路径': '禁抽',
            '课程体系': '禁抽',
            '岗位竞合': '禁抽',
            '竞赛实践': '禁抽',
        },
        'extra_rules': [
            '岗位级别按标准中的职业等级映射：五级/初级工→初级，四级/中级工→中级，三级/高级工→高级，二级/技师与一级/高级技师→高级',
            '核心技能从标准的"职业功能/工作内容/技能要求"拆分为可学习的能力点，技能等级要求照抄标准原文要求程度（如"能够独立完成"）',
            '材料中出现的"适用专业/相关专业"追加写入技能描述末尾，格式"适用专业：xxx"',
            '标准中的鉴定方式、证书信息填入认证证书列',
        ],
        'forbidden': '发展路径、薪资范围（留空）、岗位竞合——职业标准本身不含这些内容，对应表格输出空数组 []',
    },
    'curriculum': {
        'label': '培养方案类',
        'focus': '四年课程体系与教学进度（高校本科培养方案、教学计划）',
        'dim_hints': {
            '课程体系': '重点表：课程名称、课程性质、开课学年、开课学期、学分、核心知识点、培养技能（对应能力）逐项抽取',
            '技能结构': '仅从课程大纲明确写出的"培养能力/课程目标"中抽取技能，模糊表述不抽',
            '竞赛实践': '仅当材料明确列出可参加的竞赛/集中实践环节时抽取',
            '岗位聚集': '禁抽',
            '发展路径': '禁抽',
            '岗位竞合': '禁抽',
        },
        'extra_rules': [
            '开课学年填"大一/大二/大三/大四"（第1-2学期→大一，第3-4学期→大二，第5-6学期→大三，第7-8学期→大四）',
            '开课学期填"秋/春/夏"（第1、3、5、7学期→秋，第2、4、6、8学期→春，小学期/暑期→夏）',
            '教学计划表中每行课程通常有第1~8学期共8个连续列位，用√（或学期数字）标记开课学期：从该行勾选区第1列起从左往右数，√落在第N个学期列即"第N学期"，按上面两条规则换算开课学年与开课学期，逐行认真定位，不可整列留空',
            '课程性质填"必修/选修/实践"；学分填数字（如"2.5"），无法确定留空',
            '培养技能填课程培养的可学习、可验证的能力点（多个用"；"分隔），不要照抄课程目标原句；材料未写出的能力点留空，不要编造',
        ],
        'forbidden': '泛泛的就业方向、深造建议、招聘企业、薪资范围——培养方案不含这些内容，不编造',
    },
    'policy': {
        'label': '政策/行业报告类',
        'focus': '行业趋势与人才需求（政府政策网页、行业人才报告）',
        'dim_hints': {
            '岗位聚集': '抽取报告中点名的需求岗位（行业通用全称），行业领域填报告所属行业方向，需求年份取报告发布年份',
            '技能结构': '抽取报告强调的紧缺技能/关键能力，政策导向与趋势要点写入技能描述，格式"政策导向：xxx"',
            '发展路径': '仅当报告明确描述岗位晋升/转换路径时抽取',
            '竞赛实践': '仅当报告明确提到具体竞赛/认证项目时抽取',
            '课程体系': '禁抽',
            '岗位竞合': '禁抽',
        },
        'extra_rules': [
            '需求岗位用行业通用全称，不用报告中的绰号或内部缩写',
            '紧缺技能填可学习的能力点，多个用"；"分隔',
        ],
        'forbidden': '具体企业名单（招聘企业列留空）、精确薪资数字（薪资范围列留空或填"未公布"）',
    },
    'competition': {
        'label': '竞赛认证类',
        'focus': '能力匹配与时间节点（竞赛简章、证书考试大纲）',
        'dim_hints': {
            '竞赛实践': '重点表：竞赛名称、竞赛级别、主办单位、时间节点、面向专业（含参赛年级要求）、锻炼技能逐项抽取',
            '技能结构': '从竞赛考核内容/考试大纲中抽取对应技能',
            '课程体系': '仅当简章明确列出先修课程/建议课程时抽取',
            '岗位聚集': '禁抽',
            '发展路径': '禁抽',
            '岗位竞合': '禁抽',
        },
        'extra_rules': [
            '竞赛级别填"国际/国家级/省级/市级/校级"',
            '时间节点保留原文日期（报名截止、初赛、决赛、考试时间），多个用"；"分隔',
            '面向专业列填专业与年级要求，格式"专业；年级：大二及以上"',
        ],
        'forbidden': '获奖比例、奖金金额、主办方的长篇介绍（主办单位列只填名称）',
    },
    'auto': {
        'label': '自动识别',
        'focus': '先判断材料属于 职业标准/培养方案/政策行业报告/竞赛认证 中的哪一类，再按该类侧重抽取',
        'dim_hints': {},
        'extra_rules': [
            '先判断材料类型：职业标准（含"职业功能/技能要求/等级"）→侧重技能结构表；培养方案（含"学期/学分/课程设置"）→侧重课程体系表；政策/行业报告（含"趋势/需求/规划"）→侧重岗位聚集与技能结构；竞赛/认证（含"报名/赛项/考试大纲"）→侧重竞赛实践表',
            '与材料类型无关的维度宁空勿滥，输出空数组 []',
        ],
        'forbidden': '材料中不存在的维度绝不强行抽取，宁空勿滥',
    },
}

# 关键词打分式文档自动分类（确定性优先，标题命中权重×2）
DOC_TYPE_KEYWORDS = {
    'standard': ['职业技能标准', '国家职业标准', '职业标准', '任职规范', '职业功能', '职业等级', '鉴定要求', '职业概况', '职业编码'],
    'curriculum': ['培养方案', '教学计划', '课程设置', '学分', '学时', '开课学期', '教学进程', '毕业要求', '课程结构', '专业培养'],
    'policy': ['发展规划', '白皮书', '行业报告', '人才报告', '指导意见', '紧缺人才', '产业政策', '十四五', '十五五', '人才供需'],
    'competition': ['竞赛', '大赛', '考试大纲', '参赛', '赛项', '报名截止', '锦标赛', '命题比赛', '资格认证'],
}


def _detect_doc_type(title, text):
    """文档自动分类：关键词打分，得分≥3且明显领先者胜出，否则返回auto"""
    sample = f'{title or ""}\n{(text or "")[:3000]}'
    scores = {}
    for dt, kws in DOC_TYPE_KEYWORDS.items():
        s = 0
        for kw in kws:
            cnt = sample.count(kw)
            if cnt:
                s += cnt * (2 if kw in (title or '') else 1)
        scores[dt] = s
    ranked = sorted(scores.values(), reverse=True)
    best = max(scores, key=lambda k: scores[k])
    if ranked[0] >= 3 and (len(ranked) < 2 or ranked[0] >= 2 * ranked[1]):
        return best
    return 'auto'


# ============ 反馈：三中心内容质量闸门（通识课/无门槛岗位/软技能不得进入图谱） ============
# 通识/公共课程关键词：课程名含任一关键词即剔除（思政/心理健康/就业指导等公共必修课）
# 注：体育/军事系关键词已移出闸门（用户要求体育课进入图谱，由个性培养过滤器控制默认隐藏）
PE_MILITARY_KEYWORDS = ('军事', '体育', '军训', '国防教育')
GENERIC_COURSE_KEYWORDS = (
    '健康教育', '心理', '形势与政策', '思想政治', '思想道德',
    '马克思主义', '毛泽东思想', '中国特色社会主义', '近代史', '近现代史', '习近平', '党史',
    '择业', '就业指导', '职业生涯', '职业发展', '创业基础', '创新创业基础',
    '大学语文', '劳动教育', '安全教育', '法制', '美育', '通识', '公共选修',
    '文献检索',
)
# 无专业门槛岗位关键词：岗位名含任一关键词即剔除（通用服务/体力类岗位）
NO_THRESHOLD_JOB_KEYWORDS = (
    '咖啡', '保姆', '月嫂', '育婴', '保洁', '保安', '家政', '快递', '外卖', '送餐',
    '服务员', '收银', '营业员', '导购', '迎宾', '传菜', '厨师', '洗碗', '配菜',
    '普工', '搬运', '装卸', '分拣', '打包', '理货', '促销', '导游', '理发', '美容',
    '美发', '按摩', '网约车', '代驾', '清洁', '绿化工', '环卫', '门卫', '前台',
    '话务', '呼叫中心', '客服', '文员', '接待',
)
# 通用软技能精确词表：技能名完全匹配即剔除（长短不限）
SOFT_SKILL_EXACT = frozenset({
    '团队协作', '团队合作', '沟通', '沟通能力', '沟通协调', '协调能力', '组织能力', '组织协调',
    '领导力', '管理能力', '执行能力', '执行力', '创新能力', '创新思维', '创新意识', '创新实践',
    '学习能力', '学习力', '自主学习', '自学能力', '抗压能力', '抗压力', '抗压',
    '责任心', '责任感', '职业素养', '职业道德', '职业操守', '时间管理', '情绪管理',
    '表达能力', '语言表达', '语言表达能力', '演讲能力', '外语能力', '英语能力',
    '逻辑思维能力', '逻辑思维', '解决问题能力', '问题解决能力', '分析问题能力',
    '创新创业实践', '创新研究', '创业精神', '创业能力', '团队精神', '合作精神', '协作精神',
    '合作能力', '协作能力', '服务意识', '安全意识', '质量意识', '环保意识',
    '成本意识', '规则意识', '全局观念', '敬业精神', '敬业', '吃苦耐劳',
    '上进心', '进取心', '人际交往', '人际沟通', '情商', '同理心', '适应能力',
    '适应力', '应变能力', '决策能力', '传统应用', '实践能力', '动手能力',
    '综合能力', '综合素养', '综合素质', '终身学习', '持续学习', '信息素养',
    '数字素养', '人文素养', '美学修养', '创新精神',
})
# 短名软技能子串：技能名较短（≤8字）时含子串即剔除；长任务型技能（如"协作机器人示教编程"）不受影响
SOFT_SKILL_SHORT_SUBSTR = (
    '团队协作', '团队合作', '团队精神', '协作精神', '沟通协调', '组织协调',
    '学习能力', '抗压能力', '责任心', '职业素养', '职业道德', '时间管理',
    '创新能力', '创新思维', '表达能力', '演讲能力', '执行力', '领导力',
    '合作精神', '沟通能力', '协调能力', '组织能力', '管理能力', '应变能力',
    '适应能力', '解决问题', '独立思考', '终身学习', '持续学习', '创新创业',
)


def _is_quality_node(node_type, name):
    """三中心内容质量闸门：剔除与专业无关的通识课、无专业门槛岗位、通用软技能。
    返回 True 表示保留；仅约束 课程/岗位/技能 三类节点，其余类型放行。"""
    t = str(node_type or '').strip()
    if t not in ('课程', '岗位', '技能'):
        return True
    text = str(name or '').strip().lower()
    if not text:
        return False
    if t == '课程':
        return not any(kw in text for kw in GENERIC_COURSE_KEYWORDS)
    if t == '岗位':
        return not any(kw in text for kw in NO_THRESHOLD_JOB_KEYWORDS)
    if text in SOFT_SKILL_EXACT:
        return False
    if len(text) <= 8:
        return not any(kw in text for kw in SOFT_SKILL_SHORT_SUBSTR)
    return True


def _filter_graph_quality(nodes, edges):
    """builder 出口统一质量过滤：剔除低质量节点及两端任一为低质量节点的边"""
    kept = [n for n in (nodes or [])
            if _is_quality_node(n.get('type') or n.get('category') or '', n.get('name'))]
    if len(kept) == len(nodes or []):
        return nodes, edges
    kept_ids = {n['id'] for n in kept}
    return kept, [e for e in (edges or [])
                  if e.get('source') in kept_ids and e.get('target') in kept_ids]


# 节点类型颜色配置（科技风配色）
NODE_COLORS = {
    '岗位': '#3b82f6',
    '技能': '#ef4444',
    '企业': '#eab308',
    '城市': '#0ea5e9',
    '行业': '#16a34a',
    '岗位类别': '#64748b',
    '技能分类': '#f97316',
    '工具/平台': '#14b8a6',
    '认证证书': '#8b5cf6',
    '职业方向': '#ec4899',
    '专业': '#32871c',
    '课程': '#6366f1',
    '学院': '#a3e635',
    '院校': '#1e3a8a',
    '竞赛': '#d946ef',
}


def read_excel_rows(filepath):
    """读取Excel文件，返回表头和数据行"""
    wb = load_workbook(filepath, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(h) if h else f'col_{i}' for i, h in enumerate(rows[0])]
    # 补齐所有行到表头长度，防止索引越界
    col_count = len(headers)
    data = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        # 补齐或截断
        row_list = list(row)
        while len(row_list) < col_count:
            row_list.append(None)
        data.append(tuple(row_list[:col_count]))
    return headers, data


def safe_get(row, index, default=None):
    """安全获取行中指定索引的值，防止索引越界"""
    if index < len(row):
        val = row[index]
        return val if val is not None else default
    return default


# ============ 培养方案格式识别与标准化 ============

def _is_training_plan_workbook(filepath):
    """检测Excel是否为高校培养方案格式（标题区+两级表头，表头含'课程代码'+'课程名称'；标准课程体系模板无'课程代码'列）"""
    try:
        wb = load_workbook(filepath, data_only=True, read_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
            if len(rows) >= 8:
                break
        wb.close()
    except Exception:
        return False
    if len(rows) < 6:
        return False
    for row in rows:
        texts = [str(v).strip() for v in row if v is not None]
        if '课程代码' in texts and '课程名称' in texts:
            return True
    return False


def _convert_training_plan(filepath, target_dir):
    """培养方案 -> 标准课程体系表（12列+课程类别），保存为 课程体系.xlsx；返回文件名或None
    解析规则:
      1) 前8行定位含'课程代码'/'课程名称'的一级表头，结合下一行二级表头（总课时细分/修读学期1-8）拼接列名
      2) 专业/学院取自表头前的标题行（格式如 本科 本科生 XX学院 XX专业），年份取'专业培养计划(2024)'
      3) 数据行首列为分类（合并单元格需向下填充），分类即课程性质/课程类别
      4) 课程名剥离序号前缀与不间断空格；'全程总计'/'备注'行跳过；同名课程跨学期行合并开课学期
    """
    try:
        wb = load_workbook(filepath, data_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    except Exception:
        return None
    if len(rows) < 8:
        return None
    head_idx = None
    for i, row in enumerate(rows[:8]):
        texts = [str(v).strip() if v is not None else '' for v in row]
        if '课程代码' in texts and '课程名称' in texts:
            head_idx = i
            break
    if head_idx is None or head_idx + 1 >= len(rows):
        return None
    h1 = [str(v).strip() if v is not None else '' for v in rows[head_idx]]
    h2 = [str(v).strip() if v is not None else '' for v in rows[head_idx + 1]]
    label = [a or b for a, b in zip(h1, h2)]

    def col_of(exact=None, contains=None):
        for i, t in enumerate(label):
            if exact is not None and t == exact:
                return i
            if contains is not None and contains in t:
                return i
        return None

    cat_col = col_of(exact='分类')
    code_col = col_of(exact='课程代码')
    name_col = col_of(exact='课程名称')
    credit_col = col_of(exact='学分')
    dept_col = col_of(contains='开课院系')
    if dept_col is None:
        dept_col = col_of(contains='开课单位')
    sem_cols = []
    for i, t in enumerate(h2):
        if t.isdigit() and 1 <= int(t) <= 8:
            sem_cols.append((i, t))
    if cat_col is None or name_col is None or code_col is None:
        return None

    plan_tokens = []
    plan_year = ''
    for row in rows[:head_idx]:
        for v in row:
            if v is None:
                continue
            s = str(v).replace('\xa0', ' ').strip()
            if not s:
                continue
            plan_tokens.extend([t for t in s.split() if t])
            m = re.search(r'(19|20)\d{2}', s)
            if m and not plan_year:
                plan_year = m.group(0)
    major_name, college_name = '', ''
    for idx, t in enumerate(plan_tokens):
        if '学院' in t and idx + 1 < len(plan_tokens):
            college_name = t
            major_name = plan_tokens[idx + 1]
            break
    if not major_name:
        major_name = plan_tokens[-1] if plan_tokens else '未知专业'

    out_rows = []
    current_cat = ''
    for row in rows[head_idx + 2:]:
        def g(i):
            if i is not None and i < len(row) and row[i] is not None:
                return str(row[i]).replace('\xa0', ' ').strip()
            return ''
        cat = g(cat_col)
        if cat:
            current_cat = cat
        if current_cat in ('', '全程总计', '备注'):
            continue
        raw_course = g(name_col)
        course = re.sub(r'^[\s\d]+[\s.、．]+', '', raw_course).strip()
        course = re.sub(r'^[（(]\d+[）)]\s*', '', course).strip()
        if not course or '总计' in course or '合计' in course:
            continue
        if not g(code_col):
            continue
        sems = [sn for i, sn in sem_cols if g(i) and g(i) != '0']
        out_rows.append((
            major_name, '', course, current_cat, '', '、'.join(sems),
            '', '', g(dept_col), f'培养方案{plan_year}'.strip(), '', g(credit_col), current_cat
        ))
    if not out_rows:
        return None
    # 同名同分类课程合并开课学期（培养方案常按学期分多行）
    agg = {}
    for r in out_rows:
        k = (r[0], r[2], r[3])
        if k in agg:
            old = list(agg[k])
            sem_set = set(s for s in old[5].split('、') if s) | set(s for s in r[5].split('、') if s)
            old[5] = '、'.join(sorted(sem_set, key=lambda x: int(x) if x.isdigit() else 99))
            for i in (1, 8, 11):
                if not old[i]:
                    old[i] = r[i]
            agg[k] = tuple(old)
        else:
            agg[k] = r
    headers_out = list(WEB_TABLE_HEADERS['课程体系']) + ['课程类别']
    out_wb = Workbook()
    out_ws = out_wb.active
    out_ws.append(headers_out)
    for r in agg.values():
        out_ws.append(list(r))
    out_name = DIMENSION_FILES['课程体系']
    out_wb.save(os.path.join(target_dir, out_name))
    return out_name


def _ensure_training_plan_standard(target_dir):
    """目录内若存在培养方案格式Excel则转换为标准课程体系表；返回来源文件名或None
    优先检查标准名文件本身是否为培养方案格式（用户可能直接以 课程体系.xlsx 命名上传），
    其次扫描目录内其他xlsx（排除Excel临时锁文件~$开头）；已存在标准格式表时不覆盖"""
    standard_name = DIMENSION_FILES['课程体系']
    standard_path = os.path.join(target_dir, standard_name)
    try:
        if os.path.exists(standard_path):
            if _is_training_plan_workbook(standard_path):
                return _convert_training_plan(standard_path, target_dir)
            return None
        for fn in sorted(os.listdir(target_dir)):
            if not fn.lower().endswith('.xlsx') or fn.startswith('~$'):
                continue
            fp = os.path.join(target_dir, fn)
            if _is_training_plan_workbook(fp):
                return _convert_training_plan(fp, target_dir)
    except Exception:
        return None
    return None


# ============ 数据容错辅助函数 ============

def _split_multi(value):
    """按顿号/逗号/分号拆分多值文本"""
    if not value:
        return []
    parts = re.split(r'[、，,;；]', str(value))
    return [p.strip() for p in parts if p.strip()]


def _parse_overlap(value):
    """解析技能重叠度为数值：支持'35%'、'35'、'高/中/低'"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    text_map = {'高': 80, '中': 45, '低': 15}
    if s in text_map:
        return text_map[s]
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def _judge_overlap(pct):
    """按重叠度数值自动判定关系：>=60 高重叠·可互转；30-60 部分互补；<30 差异化互补"""
    if pct is None:
        return None
    if pct >= 60:
        return '高重叠·可互转'
    if pct >= 30:
        return '部分互补'
    return '差异化互补'


def _clean_attrs(attrs):
    """去掉边属性字典中的空值，并规范整型浮点数（2023.0 -> 2023）"""
    result = {}
    for k, v in attrs.items():
        if v is None or v == '':
            continue
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        result[k] = v
    return result


def build_job_cluster_graph(headers, data):
    """岗位聚集图谱
    Excel列: 岗位名称, 岗位类别, 需求年份, 招聘企业, 企业所在城市, 行业领域, 薪资范围, 数据来源, 页号
    规则:
      1) 岗位名称、招聘企业、企业所在城市、行业领域均为节点
      2) 岗位类别是岗位节点的属性
      3) 招聘岗位是招聘企业节点的属性（可多条）
      4) 岗位 -> 招聘企业 有向边，标签"招聘于"，边属性含需求年份、薪资范围
      5) 企业 -> 城市 有向边，标签"位于"
      6) 企业 -> 行业 有向边，标签"属于"
    """
    nodes = {}
    edges = []
    edge_set = set()

    def add_node(name, node_type, properties=None):
        if not name or str(name).strip() == '':
            return None
        name = str(name).strip()
        key = f"{node_type}:{name}"
        if key not in nodes:
            nodes[key] = {
                'id': key,
                'name': name,
                'type': node_type,
                'category': node_type,
                'properties': {}
            }
        if properties:
            for k, v in properties.items():
                if v is None or v == '':
                    continue
                # "招聘岗位"属性支持多条：用列表存储
                if k == '招聘岗位':
                    existing = nodes[key]['properties'].get('招聘岗位')
                    if existing is None:
                        nodes[key]['properties']['招聘岗位'] = [v]
                    elif isinstance(existing, list):
                        if v not in existing:
                            existing.append(v)
                    else:
                        if v != existing:
                            nodes[key]['properties']['招聘岗位'] = [existing, v]
                else:
                    nodes[key]['properties'].setdefault(k, v)
        return key

    def add_edge(source, target, relation, support=None, page=None, attrs=None):
        if not source or not target:
            return
        edge_key = f"{source}->{target}:{relation}"
        if edge_key not in edge_set:
            edge_set.add(edge_key)
            edges.append({
                'source': source,
                'target': target,
                'relation': relation,
                'label': relation,
                'support': support,
                'page': page,
                'attrs': attrs
            })

    for row in data:
        job = safe_get(row, 0)      # 岗位名称
        category = safe_get(row, 1) # 岗位类别
        year = safe_get(row, 2)     # 需求年份
        company = safe_get(row, 3)  # 招聘企业
        city = safe_get(row, 4)     # 企业所在城市
        industry = safe_get(row, 5) # 行业领域
        salary = safe_get(row, 6)   # 薪资范围
        support = safe_get(row, 7)  # 数据来源
        page = safe_get(row, 8)     # 页号

        # 岗位节点：岗位类别为属性
        j_id = add_node(job, '岗位', {'岗位类别': category})
        # 企业节点：招聘岗位为属性（可多条）
        c_id = add_node(company, '企业', {
            '招聘岗位': str(job).strip() if job else None
        })
        # 城市、行业节点
        city_id = add_node(city, '城市')
        ind_id = add_node(industry, '行业')

        # 岗位 -> 企业，标签"招聘于"，边属性：需求年份、薪资范围
        if j_id and c_id:
            add_edge(j_id, c_id, '招聘于', support, page, _clean_attrs({
                '需求年份': year,
                '薪资范围': salary
            }))
        # 企业 -> 城市，标签"位于"
        if c_id and city_id:
            add_edge(c_id, city_id, '位于', support, page)
        # 企业 -> 行业，标签"属于"
        if c_id and ind_id:
            add_edge(c_id, ind_id, '属于', support, page)

    return list(nodes.values()), edges


def build_skill_structure_graph(headers, data):
    """技能结构图谱
    Excel列: 岗位名称, 岗位级别, 核心技能, 技能分类, 技能等级要求, 工具/平台, 认证证书, 技能描述, 学习难度, 替代技能, 数据来源, 页号
    规则（v2.0 边方向反转）:
      1) 岗位名称、核心技能、技能分类、工具/平台、认证证书、替代技能均为节点
      2) 岗位 -> 核心技能 有向边，标签"要求"，边属性含岗位级别、技能等级要求、学习难度、技能描述
      3) 技能 -> 技能分类 有向边，标签"属于"
      4) 技能 -> 工具/平台 有向边，标签"使用"（工具可多个，按顿号/逗号拆分）
      5) 技能 -> 认证证书 有向边，标签"可考取"
      6) 替代技能 -> 核心技能 有向边，标签"可替代"
    """
    nodes = {}
    edges = []
    edge_set = set()

    def add_node(name, node_type, properties=None):
        if not name or str(name).strip() == '':
            return None
        name = str(name).strip()
        key = f"{node_type}:{name}"
        if key not in nodes:
            nodes[key] = {
                'id': key,
                'name': name,
                'type': node_type,
                'category': node_type,
                'properties': {}
            }
        if properties:
            for k, v in properties.items():
                if v is None or v == '':
                    continue
                nodes[key]['properties'].setdefault(k, v)
        return key

    def add_edge(source, target, relation, support=None, page=None, attrs=None):
        if not source or not target:
            return
        edge_key = f"{source}->{target}:{relation}"
        if edge_key not in edge_set:
            edge_set.add(edge_key)
            edges.append({
                'source': source,
                'target': target,
                'relation': relation,
                'label': relation,
                'support': support,
                'page': page,
                'attrs': attrs
            })

    for row in data:
        job = safe_get(row, 0)        # 岗位名称
        level = safe_get(row, 1)      # 岗位级别
        skill = safe_get(row, 2)      # 核心技能
        s_category = safe_get(row, 3) # 技能分类
        s_level = safe_get(row, 4)    # 技能等级要求
        tools = safe_get(row, 5)      # 工具/平台
        cert = safe_get(row, 6)       # 认证证书
        s_desc = safe_get(row, 7)     # 技能描述
        difficulty = safe_get(row, 8) # 学习难度
        alt_skill = safe_get(row, 9)  # 替代技能
        support = safe_get(row, 10)   # 数据来源
        page = safe_get(row, 11)      # 页号

        # 岗位节点
        j_id = add_node(job, '岗位')
        # 技能要求边属性：岗位级别、技能等级要求、学习难度、技能描述
        edge_attrs = _clean_attrs({
            '岗位级别': level,
            '技能等级要求': s_level,
            '学习难度': difficulty,
            '技能描述': s_desc
        })

        # 核心技能可能一格多值：拆分为多个技能节点
        for sk in _split_multi(skill):
            sk_id = add_node(sk, '技能')
            # 岗位 -> 技能，标签"要求"（v2.0 方向反转）
            if j_id and sk_id:
                add_edge(j_id, sk_id, '要求', support, page, edge_attrs)
            # 技能 -> 技能分类，标签"属于"
            cat_id = add_node(s_category, '技能分类')
            if sk_id and cat_id:
                add_edge(sk_id, cat_id, '属于', support, page)
            # 技能 -> 工具/平台，标签"使用"
            for tool in _split_multi(tools):
                t_id = add_node(tool, '工具/平台')
                if sk_id and t_id:
                    add_edge(sk_id, t_id, '使用', support, page)
            # 技能 -> 认证证书，标签"可考取"
            for c_name in _split_multi(cert):
                c_id = add_node(c_name, '认证证书')
                if sk_id and c_id:
                    add_edge(sk_id, c_id, '可考取', support, page)
            # 替代技能 -> 核心技能，标签"可替代"
            for alt in _split_multi(alt_skill):
                a_id = add_node(alt, '技能')
                if a_id and sk_id:
                    add_edge(a_id, sk_id, '可替代', support, page)

    return list(nodes.values()), edges


def build_career_path_graph(headers, data):
    """发展路径图谱
    Excel列: 起始岗位, 起始职级, 目标岗位, 目标职级, 所需新增技能, 转换难度, 平均年限, 职业方向, 数据来源, 页号
    规则:
      1) 起始岗位、目标岗位、所需新增技能、职业方向均为节点
      2) 起始岗位 -> 目标岗位 有向边，标签"晋升/转岗"，边属性含起始职级、目标职级、转换难度、平均年限
      3) 目标岗位 -> 职业方向 有向边，标签"属于"
      4) 起始岗位 -> 技能 有向边，标签"需补充技能"（技能可多个，按顿号/逗号拆分）
    """
    nodes = {}
    edges = []
    edge_set = set()

    def add_node(name, node_type, properties=None):
        if not name or str(name).strip() == '':
            return None
        name = str(name).strip()
        key = f"{node_type}:{name}"
        if key not in nodes:
            nodes[key] = {
                'id': key,
                'name': name,
                'type': node_type,
                'category': node_type,
                'properties': {}
            }
        if properties:
            for k, v in properties.items():
                if v is None or v == '':
                    continue
                nodes[key]['properties'].setdefault(k, v)
        return key

    def add_edge(source, target, relation, support=None, page=None, attrs=None):
        if not source or not target:
            return
        edge_key = f"{source}->{target}:{relation}"
        if edge_key not in edge_set:
            edge_set.add(edge_key)
            edges.append({
                'source': source,
                'target': target,
                'relation': relation,
                'label': relation,
                'support': support,
                'page': page,
                'attrs': attrs
            })

    for row in data:
        from_job = safe_get(row, 0)   # 起始岗位
        from_level = safe_get(row, 1) # 起始职级
        to_job = safe_get(row, 2)     # 目标岗位
        to_level = safe_get(row, 3)   # 目标职级
        new_skills = safe_get(row, 4) # 所需新增技能
        difficulty = safe_get(row, 5) # 转换难度
        years = safe_get(row, 6)      # 平均年限
        direction = safe_get(row, 7)  # 职业方向
        support = safe_get(row, 8)    # 数据来源
        page = safe_get(row, 9)       # 页号

        # 岗位、职业方向节点
        f_id = add_node(from_job, '岗位')
        t_id = add_node(to_job, '岗位')
        d_id = add_node(direction, '职业方向')

        # 起始岗位 -> 目标岗位，标签"晋升/转岗"，边属性：起始职级、目标职级、转换难度、平均年限
        if f_id and t_id:
            add_edge(f_id, t_id, '晋升/转岗', support, page, _clean_attrs({
                '起始职级': from_level,
                '目标职级': to_level,
                '转换难度': difficulty,
                '平均年限': years
            }))
        # 目标岗位 -> 职业方向，标签"属于"
        if t_id and d_id:
            add_edge(t_id, d_id, '属于', support, page)
        # 起始岗位 -> 技能，标签"需补充技能"
        for sk in _split_multi(new_skills):
            s_id = add_node(sk, '技能')
            if f_id and s_id:
                add_edge(f_id, s_id, '需补充技能', support, page)

    return list(nodes.values()), edges


def build_course_system_graph(headers, data):
    """课程体系图谱（v2.0 重造）
    Excel列: 专业名称, 学科门类, 课程名称, 课程性质, 开课学年, 开课学期, 核心知识点, 培养技能, 开设学院, 数据来源, 页号, 学分(P0-1新增，可空)
    规则:
      1) 专业、课程、技能、学院均为节点；课程性质/开课学年/开课学期/核心知识点/学分是课程节点属性
      2) 专业 -> 课程 有向边，标签"包含"
      3) 课程 -> 技能 有向边，标签"培养"（培养技能按顿号/逗号拆分）
      4) 学院 -> 课程 有向边，标签"开设"（开设学院按顿号/逗号拆分，可空）
      5) 系统固定生成 院校:天津大学 -> 专业 有向边，标签"开设"
    """
    nodes = {}
    edges = []
    edge_set = set()

    def add_node(name, node_type, properties=None):
        if not name or str(name).strip() == '':
            return None
        name = str(name).strip()
        key = f"{node_type}:{name}"
        if key not in nodes:
            nodes[key] = {
                'id': key,
                'name': name,
                'type': node_type,
                'category': node_type,
                'properties': {}
            }
        if properties:
            for k, v in properties.items():
                if v is None or v == '':
                    continue
                nodes[key]['properties'].setdefault(k, v)
        return key

    def add_edge(source, target, relation, support=None, page=None, attrs=None):
        if not source or not target:
            return
        edge_key = f"{source}->{target}:{relation}"
        if edge_key not in edge_set:
            edge_set.add(edge_key)
            edges.append({
                'source': source,
                'target': target,
                'relation': relation,
                'label': relation,
                'support': support,
                'page': page,
                'attrs': attrs
            })

    for row in data:
        major = safe_get(row, 0)       # 专业名称
        subject = safe_get(row, 1)     # 学科门类
        course = safe_get(row, 2)      # 课程名称
        c_nature = safe_get(row, 3)    # 课程性质
        c_year = safe_get(row, 4)      # 开课学年
        c_sem = safe_get(row, 5)       # 开课学期
        knowledge = safe_get(row, 6)   # 核心知识点
        skill = safe_get(row, 7)       # 培养技能
        college = safe_get(row, 8)     # 开设学院
        support = safe_get(row, 9)     # 数据来源
        page = safe_get(row, 10)       # 页号
        credit = safe_get(row, 11)     # 学分（P0-1新增，历史文件缺失按空处理）
        c_cat = safe_get(row, 12)      # 课程类别（培养方案转换产物附带第13列，标准表无此列按空处理）

        # 专业节点：学科门类为属性
        m_id = add_node(major, '专业', {'学科门类': subject})
        # 课程节点：课程性质、开课学年、开课学期、核心知识点、学分为属性
        c_id = add_node(course, '课程', {
            '课程性质': c_nature,
            '课程类别': c_cat,
            '开课学年': c_year,
            '开课学期': c_sem,
            '核心知识点': knowledge,
            '学分': credit
        })

        # 专业 -> 课程，标签"包含"
        if m_id and c_id:
            add_edge(m_id, c_id, '包含', support, page)
        # 课程 -> 技能，标签"培养"
        for sk in _split_multi(skill):
            sk_id = add_node(sk, '技能')
            if c_id and sk_id:
                add_edge(c_id, sk_id, '培养', support, page)
        # 学院 -> 课程，标签"开设"
        for col in _split_multi(college):
            col_id = add_node(col, '学院')
            if col_id and c_id:
                add_edge(col_id, c_id, '开设', support, page)

    # 系统固定：天津大学 -> 专业，标签"开设"
    for key, node in list(nodes.items()):
        if node['type'] == '专业':
            u_id = add_node('天津大学', '院校', {'院校城市': '天津'})
            add_edge(u_id, key, '开设')

    return list(nodes.values()), edges


def build_competition_practice_graph(headers, data):
    """竞赛实践图谱（v2.0 新增）
    Excel列: 竞赛名称, 竞赛级别, 主办单位, 时间节点, 面向专业, 锻炼技能, 竞赛描述, 数据来源, 页号
    规则:
      1) 竞赛、专业、技能均为节点；竞赛级别/主办单位/时间节点/竞赛描述是竞赛节点属性
      2) 竞赛 -> 技能 有向边，标签"锻炼"（锻炼技能按顿号/逗号拆分）
      3) 竞赛 -> 专业 有向边，标签"面向"（面向专业按顿号/逗号拆分）
    """
    nodes = {}
    edges = []
    edge_set = set()

    def add_node(name, node_type, properties=None):
        if not name or str(name).strip() == '':
            return None
        name = str(name).strip()
        key = f"{node_type}:{name}"
        if key not in nodes:
            nodes[key] = {
                'id': key,
                'name': name,
                'type': node_type,
                'category': node_type,
                'properties': {}
            }
        if properties:
            for k, v in properties.items():
                if v is None or v == '':
                    continue
                nodes[key]['properties'].setdefault(k, v)
        return key

    def add_edge(source, target, relation, support=None, page=None, attrs=None):
        if not source or not target:
            return
        edge_key = f"{source}->{target}:{relation}"
        if edge_key not in edge_set:
            edge_set.add(edge_key)
            edges.append({
                'source': source,
                'target': target,
                'relation': relation,
                'label': relation,
                'support': support,
                'page': page,
                'attrs': attrs
            })

    for row in data:
        comp = safe_get(row, 0)      # 竞赛名称
        level = safe_get(row, 1)     # 竞赛级别
        organizer = safe_get(row, 2) # 主办单位
        timing = safe_get(row, 3)    # 时间节点
        majors = safe_get(row, 4)    # 面向专业
        skills = safe_get(row, 5)    # 锻炼技能
        desc = safe_get(row, 6)      # 竞赛描述
        support = safe_get(row, 7)   # 数据来源
        page = safe_get(row, 8)      # 页号

        # 竞赛节点：竞赛级别、主办单位、时间节点、竞赛描述为属性
        cp_id = add_node(comp, '竞赛', {
            '竞赛级别': level,
            '主办单位': organizer,
            '时间节点': timing,
            '竞赛描述': desc
        })

        # 竞赛 -> 技能，标签"锻炼"
        for sk in _split_multi(skills):
            sk_id = add_node(sk, '技能')
            if cp_id and sk_id:
                add_edge(cp_id, sk_id, '锻炼', support, page)
        # 竞赛 -> 专业，标签"面向"
        for mj in _split_multi(majors):
            m_id = add_node(mj, '专业')
            if cp_id and m_id:
                add_edge(cp_id, m_id, '面向', support, page)

    return list(nodes.values()), edges


def build_job_coopetition_graph(headers, data):
    """岗位竞合图谱
    Excel列: 岗位A, 技能组A, 岗位A描述, 技能重叠度, 岗位B, 技能组B, 岗位B描述, 数据来源, 页号
    规则:
      1) 岗位A、岗位B、技能均为节点，岗位描述是岗位节点的属性
      2) 岗位A -- 岗位B 边标签"技能重叠"，边属性含技能重叠度、关系判定（按重叠度自动分档）
      3) 岗位 -> 技能 有向边，标签"核心技能"（技能组可多个，按顿号/逗号拆分）
    """
    nodes = {}
    edges = []
    edge_set = set()

    def add_node(name, node_type, properties=None):
        if not name or str(name).strip() == '':
            return None
        name = str(name).strip()
        key = f"{node_type}:{name}"
        if key not in nodes:
            nodes[key] = {
                'id': key,
                'name': name,
                'type': node_type,
                'category': node_type,
                'properties': {}
            }
        if properties:
            for k, v in properties.items():
                if v is None or v == '':
                    continue
                nodes[key]['properties'].setdefault(k, v)
        return key

    def add_edge(source, target, relation, support=None, page=None, attrs=None):
        if not source or not target:
            return
        edge_key = f"{source}->{target}:{relation}"
        if edge_key not in edge_set:
            edge_set.add(edge_key)
            edges.append({
                'source': source,
                'target': target,
                'relation': relation,
                'label': relation,
                'support': support,
                'page': page,
                'attrs': attrs
            })

    for row in data:
        job_a = safe_get(row, 0)    # 岗位A
        skills_a = safe_get(row, 1) # 技能组A
        desc_a = safe_get(row, 2)   # 岗位A描述
        overlap = safe_get(row, 3)  # 技能重叠度
        job_b = safe_get(row, 4)    # 岗位B
        skills_b = safe_get(row, 5) # 技能组B
        desc_b = safe_get(row, 6)   # 岗位B描述
        support = safe_get(row, 7)  # 数据来源
        page = safe_get(row, 8)     # 页号

        # 岗位节点：岗位描述为属性
        a_id = add_node(job_a, '岗位', {'岗位描述': desc_a})
        b_id = add_node(job_b, '岗位', {'岗位描述': desc_b})

        # 岗位A -- 岗位B，标签"技能重叠"，边属性：技能重叠度、关系判定
        if a_id and b_id:
            pct = _parse_overlap(overlap)
            add_edge(a_id, b_id, '技能重叠', support, page, _clean_attrs({
                '技能重叠度': overlap,
                '重叠度数值': pct,
                '关系判定': _judge_overlap(pct)
            }))
        # 岗位A -> 技能，标签"核心技能"
        for sk in _split_multi(skills_a):
            s_id = add_node(sk, '技能')
            if a_id and s_id:
                add_edge(a_id, s_id, '核心技能', support, page)
        # 岗位B -> 技能，标签"核心技能"
        for sk in _split_multi(skills_b):
            s_id = add_node(sk, '技能')
            if b_id and s_id:
                add_edge(b_id, s_id, '核心技能', support, page)

    return list(nodes.values()), edges


def _quality_guarded_builder(builder):
    """builder 包装：出口统一执行三中心内容质量过滤（Excel懒导入/网页导入/reimport/rebuild 四条路径全覆盖）"""
    def _wrapped(headers, data):
        return _filter_graph_quality(*builder(headers, data))
    return _wrapped


DIMENSION_BUILDERS = {
    '岗位聚集': _quality_guarded_builder(build_job_cluster_graph),
    '技能结构': _quality_guarded_builder(build_skill_structure_graph),
    '发展路径': _quality_guarded_builder(build_career_path_graph),
    '课程体系': _quality_guarded_builder(build_course_system_graph),
    '岗位竞合': _quality_guarded_builder(build_job_coopetition_graph),
    '竞赛实践': _quality_guarded_builder(build_competition_practice_graph),
}


# ============ 辅助函数 ============

def get_dim_name(dimension_key):
    """英文key -> 中文维度名"""
    for k, v in DIMENSION_KEYS.items():
        if v == dimension_key:
            return k
    return None


def ensure_imported(folder_path, dimension_key):
    """确保某维度的Excel数据已导入SQLite（懒导入），并同步到全局库"""
    dim_name = get_dim_name(dimension_key)
    if not dim_name:
        return False
    db_path = db_module.get_db_path(folder_path, dimension_key)
    db_module.init_db(db_path)
    if not db_module.is_imported(db_path):
        # 需要从Excel导入
        filename = DIMENSION_FILES[dim_name]
        filepath = os.path.join(folder_path, filename)
        if not os.path.exists(filepath):
            return False
        headers, data = read_excel_rows(filepath)
        builder = DIMENSION_BUILDERS[dim_name]
        nodes, edges = builder(headers, data)
        # 防护：Excel为空模板时builder返回空，若库中已有数据则只补标记不覆盖，
        # 防止空模板触发覆盖式导入摧毁已有富库
        if not nodes:
            if db_module.count_nodes(db_path) > 0:
                db_module.mark_imported(db_path)
            return True
        db_module.import_graph_data(db_path, nodes, edges)
        # 同步到全局库
        folder_name = os.path.basename(folder_path)
        global_db = db_module.get_global_db_path(dimension_key)
        db_module.init_global_db(global_db)
        db_module.sync_folder_to_global(global_db, folder_name, nodes, edges)
    return True


# ============ 网页导入通道（A/B）辅助函数 ============

def _llm_chat(messages, max_tokens=16384, temperature=0.2, timeout=600):
    """调用天大校园LLM端点抽取（tju-llm，vLLM后端；enable_thinking=False关闭思考，
    避免思考token挤占输出预算导致正文输出0字符）"""
    headers = {'Authorization': f'Bearer {LLM_API_KEY}', 'Content-Type': 'application/json'}
    payload = {
        'model': LLM_MODEL,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'chat_template_kwargs': {'enable_thinking': False}
    }
    total_in = sum(len(m.get('content') or '') for m in messages)
    print(f'[web-import] 调用LLM: model={LLM_MODEL} max_tokens={max_tokens} 输入长度={total_in}', flush=True)
    for attempt in range(1, 4):
        try:
            resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get('usage') or {}
                choice = (data.get('choices') or [{}])[0]
                content = choice.get('message', {}).get('content')
                print(f'[web-import] LLM成功: finish_reason={choice.get("finish_reason")} '
                      f'completion={usage.get("completion_tokens")} 输出长度={len(content or "")}', flush=True)
                if choice.get('finish_reason') == 'length':
                    print('[web-import] 警告: LLM输出被max_tokens截断，结果可能不完整', flush=True)
                return content
            print(f'[web-import] LLM第{attempt}次请求失败: HTTP {resp.status_code} {resp.text[:200]}', flush=True)
        except Exception as e:
            print(f'[web-import] LLM第{attempt}次请求异常: {e}', flush=True)
        time.sleep(3 * attempt)
    return None


def _fetch_url_text(url):
    """通道A：requests抓取网页并提取纯文本（静态页面可用；JS渲染页面会正文过短而降级提示）"""
    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    resp = requests.get(url, headers=req_headers, timeout=30)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
        resp.encoding = resp.apparent_encoding or 'utf-8'
    raw = resp.text
    title_m = re.search(r'<title[^>]*>([\s\S]*?)</title>', raw, re.I)
    title = html_module.unescape(title_m.group(1)).strip() if title_m else ''
    body = re.sub(r'<(script|style|noscript|svg|iframe)[^>]*>[\s\S]*?</\1>', ' ', raw, flags=re.I)
    body = re.sub(r'<br\s*/?>', '\n', body, flags=re.I)
    body = re.sub(r'</t[dh]>', ' | ', body, flags=re.I)
    body = re.sub(r'</(p|div|tr|li|h[1-6]|table|thead|tbody|section|article|ul|ol|dl|dd|dt)>', '\n', body, flags=re.I)
    body = re.sub(r'<[^>]+>', '', body)
    body = html_module.unescape(body)
    lines = []
    for ln in body.split('\n'):
        ln = re.sub(r'\s+', ' ', ln).strip()
        if ln:
            lines.append(ln)
    return title, '\n'.join(lines)


def _build_extract_messages(title, text, doc_type='auto'):
    """按文档类型模板构建抽取提示词（六表契约固定，模板只改变抽取侧重/附加规则/禁抽边界；
    数据来源/页号两列由服务端统一覆写，不依赖LLM）"""
    template = DOC_TYPE_TEMPLATES.get(doc_type) or DOC_TYPE_TEMPLATES['auto']
    contract_lines = []
    for dim_name, headers in WEB_TABLE_HEADERS.items():
        line = f'- {dim_name}（列名与顺序固定）: {"|".join(headers)}'
        hint = template['dim_hints'].get(dim_name)
        if hint == '禁抽':
            line += '｜本类文档通常不含此维度，预期为空'
        elif hint:
            line += f'｜抽取侧重：{hint}'
        contract_lines.append(line)
    contract = '\n'.join(contract_lines)
    extra_rules = '\n'.join(f'{i}. {r}' for i, r in enumerate(template['extra_rules'], 1))
    system_prompt = f"""你是就业知识图谱的数据抽取引擎。从给定材料中抽取结构化数据，严格按以下六表契约输出JSON。

当前材料类型：{template['label']}
抽取目标：{template['focus']}

六表契约：
{contract}

通用规则：
1. 只抽取材料中明确出现的信息，禁止编造；材料未覆盖的维度输出空数组 []
2. "数据来源"和"页号"两列统一留空字符串，系统会自动填写
3. 一个单元格含多个值时用中文分号"；"分隔（如多个技能、多门课程、多个证书）
4. 命名规范：岗位用行业通用全称（如"电气工程师"）；技能为可学习的能力点；课程用培养方案中的课程全称；竞赛用官方全称
5. "技能重叠度"填0-100的整数；"转换难度"和"学习难度"填"低/中/高"；"岗位级别"填"初级/中级/高级"
6. "竞赛级别"填"国家级/省级/市级/校级"；"课程性质"填"必修/选修/实践"
7. 薪资范围尽量保留原文数字（如"10k-15k"）；需求年份取招聘年份
8. 输出纯JSON，不要markdown代码块，不要任何解释文字。JSON格式：
{{"标题": "<材料标题>", "表格": {{"岗位聚集": [["值", "..."]], "技能结构": [], "发展路径": [], "课程体系": [], "岗位竞合": [], "竞赛实践": []}}}}

质量红线（硬约束，违反视为错误抽取）：
- 课程体系表：只抽与材料主体专业相关的专业基础课/专业核心课/专业选修课/专业实践环节；军事理论、体育、心理健康、形势与政策、思想政治类、择业/就业指导、安全教育、大学语文等公共通识课一律不抽
- 岗位聚集/发展路径表：只抽需要专业技能门槛且与材料主体专业/行业对口的岗位；服务员、保安、保洁、快递、外卖、收银、普工、家政、客服话务、文员等无专业门槛的通用岗位一律不抽
- 技能类内容（技能结构表"核心技能"、课程体系"培养技能"、竞赛实践"锻炼技能"）：必须是具体、可学习、可验证的专业技术能力点；"团队协作""沟通能力""创新能力""学习能力""责任心"等通用软技能一律不抽

本类型附加规则：
{extra_rules}

禁止抽取（宁空勿滥）：{template['forbidden']}"""
    user_prompt = f"【材料标题】{title}\n【材料正文】\n{text}"
    return [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt}
    ]


def _parse_extract_json(raw):
    """健壮解析LLM返回的抽取JSON，失败返回None"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get('表格'), dict):
        return None
    return data


def _run_web_extract(title, text, source_label, doc_type='auto'):
    """执行抽取并按六表契约规整行（覆写数据来源/页号、清洗空行），失败返回None；
    doc_type='auto'时先做关键词分类，返回解析出的文档类型供前端展示"""
    resolved = doc_type if doc_type in DOC_TYPE_TEMPLATES and doc_type != 'auto' else None
    if resolved is None:
        resolved = _detect_doc_type(title, text)
    messages = _build_extract_messages(title, text, resolved)
    print(f'[web-import] 开始抽取: doc_type={resolved} 标题={title[:40]} 正文长度={len(text)}', flush=True)
    raw = _llm_chat(messages)
    print(f'[web-import] 首次抽取返回: {"None" if raw is None else f"长度{len(raw)}"}', flush=True)
    parsed = _parse_extract_json(raw)
    if parsed is None:
        print(f'[web-import] 首次JSON解析失败, 原始输出前200字符: {(raw or "")[:200]!r}', flush=True)
        retry_raw = _llm_chat([
            {'role': 'system', 'content': '你是JSON修复器。只输出修复后的纯JSON对象，不要任何解释或代码块。'},
            {'role': 'user', 'content': f'以下内容应为一个JSON对象（含"标题"与"表格"字段），请修复并输出：\n\n{(raw or "")[:3000]}'}
        ], max_tokens=8192)
        parsed = _parse_extract_json(retry_raw)
        if parsed is None:
            print(f'[web-import] JSON修复器仍失败, 修复器输出前200字符: {(retry_raw or "")[:200]!r}', flush=True)
            return None
    result_title = str(parsed.get('标题') or title).strip()[:60]
    tables_out = {}
    for dim_name, headers in WEB_TABLE_HEADERS.items():
        rows = parsed['表格'].get(dim_name) or []
        src_idx = headers.index('数据来源')
        page_idx = headers.index('页号')
        clean_rows = []
        for row in rows:
            if not isinstance(row, list):
                continue
            row = [str(cell).strip() if cell is not None else '' for cell in row[:len(headers)]]
            row += [''] * (len(headers) - len(row))
            biz = [v for i, v in enumerate(row) if i not in (src_idx, page_idx)]
            if not any(biz):
                continue
            row[src_idx] = source_label
            row[page_idx] = title
            clean_rows.append(row)
        tables_out[dim_name] = {'headers': headers, 'rows': clean_rows}
    return {
        'title': result_title,
        'tables': tables_out,
        'doc_type': resolved,
        'doc_type_label': DOC_TYPE_TEMPLATES[resolved]['label']
    }


# ============ 路由 ============

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/folders', methods=['GET'])
def list_folders():
    """列出所有可用的图谱文件夹"""
    folders = []
    # 默认文件夹
    if os.path.exists(GRAPH_BASE_DIR):
        for name in os.listdir(GRAPH_BASE_DIR):
            path = os.path.join(GRAPH_BASE_DIR, name)
            if os.path.isdir(path):
                files = os.listdir(path)
                xlsx_count = sum(1 for f in files if f.endswith('.xlsx'))
                folders.append({
                    'name': name,
                    'path': path,
                    'source': 'default',
                    'file_count': xlsx_count
                })
    # 上传文件夹（根=公共资料库；users/ 为个人资料区根，跳过）
    if os.path.exists(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if name == 'users':
                continue
            if os.path.isdir(path):
                files = os.listdir(path)
                xlsx_count = sum(1 for f in files if f.endswith('.xlsx'))
                folders.append({
                    'name': name,
                    'path': path,
                    'source': 'uploaded',
                    'file_count': xlsx_count
                })
    # 登录用户追加个人资料区
    user = _current_user()
    if user:
        personal_root = _user_upload_dir(user['id'])
        if os.path.isdir(personal_root):
            for name in os.listdir(personal_root):
                path = os.path.join(personal_root, name)
                if os.path.isdir(path):
                    files = os.listdir(path)
                    xlsx_count = sum(1 for f in files if f.endswith('.xlsx'))
                    folders.append({
                        'name': name,
                        'path': path,
                        'source': 'personal',
                        'file_count': xlsx_count
                    })
    return jsonify(folders)


@app.route('/api/folder/select', methods=['POST'])
def select_folder():
    """选择当前激活的文件夹（按用户隔离）"""
    data = request.json
    folder_path = data.get('path')
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({'error': '文件夹不存在'}), 400
    if not _is_allowed_folder(folder_path):
        return jsonify({'error': '无权访问该文件夹'}), 403
    _set_current_folder(folder_path)
    # 培养方案格式识别：用户手动放入的培养方案Excel在此自动转换为标准课程体系表
    _ensure_training_plan_standard(folder_path)
    # 返回文件夹中可用的维度
    available = []
    for dim_name, filename in DIMENSION_FILES.items():
        filepath = os.path.join(folder_path, filename)
        if os.path.exists(filepath):
            available.append({
                'dimension': dim_name,
                'key': DIMENSION_KEYS[dim_name],
                'filename': filename
            })
    return jsonify({'success': True, 'dimensions': available, 'folder': folder_path})


@app.route('/api/graph/<dimension>', methods=['GET'])
def get_graph(dimension):
    """获取指定维度的图谱数据（从SQLite读取）
    查询参数 scope=global 时返回全局汇总数据
    """
    scope = request.args.get('scope', 'folder')

    # 全局视图
    if scope == 'global':
        dim_name = get_dim_name(dimension)
        if not dim_name:
            return jsonify({'error': '未知维度'}), 400
        global_db = db_module.get_global_db_path(dimension)
        db_module.init_global_db(global_db)
        nodes, edges = _filter_graph_quality(*db_module.get_global_graph_data(global_db))
        return jsonify({
            'dimension': dim_name,
            'dimension_key': dimension,
            'scope': 'global',
            'nodes': nodes,
            'edges': edges,
            'headers': [],
            'raw_data': []
        })

    # 文件夹视图（原有逻辑）
    current_folder = _get_current_folder()
    if not current_folder:
        if os.path.exists(GRAPH_BASE_DIR):
            for name in os.listdir(GRAPH_BASE_DIR):
                path = os.path.join(GRAPH_BASE_DIR, name)
                if os.path.isdir(path):
                    current_folder = path
                    _set_current_folder(path)
                    break
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400

    dim_name = get_dim_name(dimension)
    if not dim_name:
        return jsonify({'error': '未知维度'}), 400

    filename = DIMENSION_FILES[dim_name]
    filepath = os.path.join(current_folder, filename)

    # 确保数据已导入SQLite；Excel缺失但库中已有数据时跳过导入直接读库
    db_path = db_module.get_db_path(current_folder, dimension)
    db_module.init_db(db_path)
    if not ensure_imported(current_folder, dimension):
        if not os.path.exists(filepath) and db_module.count_nodes(db_path) == 0:
            return jsonify({'error': f'文件不存在: {filename}'}), 404

    nodes, edges = db_module.get_graph_data(db_path)

    # 同时读Excel表头和原始数据（用于前端展示），Excel缺失时留空
    if os.path.exists(filepath):
        headers, raw_data = read_excel_rows(filepath)
    else:
        headers, raw_data = [], []

    return jsonify({
        'dimension': dim_name,
        'dimension_key': dimension,
        'nodes': nodes,
        'edges': edges,
        'headers': headers,
        'raw_data': [list(row) for row in raw_data]
    })


@app.route('/api/graph/<dimension>/node', methods=['POST'])
def add_node(dimension):
    """添加新节点（持久化到SQLite）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    db_path = db_module.get_db_path(current_folder, dimension)
    db_module.init_db(db_path)

    data = request.json
    node = {
        'id': data.get('id') or f"{data.get('type', 'custom')}:{data.get('name')}",
        'name': data.get('name', ''),
        'type': data.get('type', '自定义'),
        'category': data.get('type', '自定义'),
        'properties': data.get('properties', {})
    }
    db_module.add_node(db_path, node)
    return jsonify({'success': True, 'node': node})


@app.route('/api/graph/<dimension>/node/<node_id>', methods=['PUT'])
def update_node(dimension, node_id):
    """更新节点信息（持久化到SQLite）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    db_path = db_module.get_db_path(current_folder, dimension)

    data = request.json
    updated_node = {
        'id': node_id,
        'name': data.get('name', ''),
        'type': data.get('type', ''),
        'category': data.get('type', ''),
        'properties': data.get('properties', {})
    }
    db_module.update_node(db_path, node_id, updated_node)
    return jsonify({'success': True, 'node': updated_node})


@app.route('/api/graph/<dimension>/node/<node_id>', methods=['DELETE'])
def delete_node(dimension, node_id):
    """删除节点及其关联边（持久化到SQLite）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    db_path = db_module.get_db_path(current_folder, dimension)
    db_module.delete_node(db_path, node_id)
    return jsonify({'success': True, 'deleted_id': node_id})


@app.route('/api/graph/<dimension>/edge', methods=['POST'])
def add_edge(dimension):
    """添加新边（持久化到SQLite）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    db_path = db_module.get_db_path(current_folder, dimension)
    db_module.init_db(db_path)

    data = request.json
    edge = {
        'source': data.get('source'),
        'target': data.get('target'),
        'relation': data.get('relation', '关联'),
        'label': data.get('label', data.get('relation', '关联')),
        'support': data.get('support', ''),
        'page': data.get('page', ''),
        'product': data.get('product', '')
    }
    saved_edge = db_module.add_edge(db_path, edge)
    return jsonify({'success': True, 'edge': saved_edge})


@app.route('/api/graph/<dimension>/edge/<int:edge_id>', methods=['PUT'])
def update_edge(dimension, edge_id):
    """更新边（持久化到SQLite）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    db_path = db_module.get_db_path(current_folder, dimension)

    data = request.json
    edge = {
        'source': data.get('source'),
        'target': data.get('target'),
        'relation': data.get('relation', '关联'),
        'label': data.get('label', data.get('relation', '关联')),
        'support': data.get('support', ''),
        'page': data.get('page', ''),
        'product': data.get('product', '')
    }
    db_module.update_edge(db_path, edge_id, edge)
    return jsonify({'success': True, 'edge': edge})


@app.route('/api/graph/<dimension>/edge/<int:edge_id>', methods=['DELETE'])
def delete_edge(dimension, edge_id):
    """删除边（持久化到SQLite）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    db_path = db_module.get_db_path(current_folder, dimension)
    db_module.delete_edge(db_path, edge_id)
    return jsonify({'success': True, 'deleted_id': edge_id})


@app.route('/api/graph/<dimension>/reimport', methods=['POST'])
def reimport_graph(dimension):
    """重新从Excel导入某维度（清空DB后重新导入）"""
    current_folder = _get_current_folder()
    if not current_folder:
        return jsonify({'error': '未选择文件夹'}), 400
    dim_name = get_dim_name(dimension)
    if not dim_name:
        return jsonify({'error': '未知维度'}), 400

    filename = DIMENSION_FILES[dim_name]
    filepath = os.path.join(current_folder, filename)
    if not os.path.exists(filepath):
        return jsonify({'error': f'文件不存在: {filename}'}), 404

    headers, data = read_excel_rows(filepath)
    builder = DIMENSION_BUILDERS[dim_name]
    try:
        nodes, edges = builder(headers, data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'构建图谱失败: {str(e)}'}), 500

    db_path = db_module.get_db_path(current_folder, dimension)
    db_module.import_graph_data(db_path, nodes, edges)
    # 同步到全局库
    folder_name = os.path.basename(current_folder)
    global_db = db_module.get_global_db_path(dimension)
    db_module.init_global_db(global_db)
    db_module.sync_folder_to_global(global_db, folder_name, nodes, edges)
    return jsonify({'success': True, 'nodes': len(nodes), 'edges': len(edges)})


@app.route('/api/graph/<dimension>/rebuild-global', methods=['POST'])
def rebuild_global(dimension):
    """重建全局库：扫描所有文件夹，重新汇总到全局库"""
    dim_name = get_dim_name(dimension)
    if not dim_name:
        return jsonify({'error': '未知维度'}), 400

    global_db = db_module.get_global_db_path(dimension)
    db_module.init_global_db(global_db)
    db_module.rebuild_global_db(global_db)

    # 遍历所有文件夹
    all_folders = []
    if os.path.exists(GRAPH_BASE_DIR):
        for name in os.listdir(GRAPH_BASE_DIR):
            path = os.path.join(GRAPH_BASE_DIR, name)
            if os.path.isdir(path):
                all_folders.append((name, path))
    if os.path.exists(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if os.path.isdir(path):
                all_folders.append((name, path))
    # 同步顺序：新培养方案（文件夹名含年份）先同步，学期字段先到先得=以最新为准
    all_folders.sort(key=lambda t: _folder_sync_sort_key(t[0]))

    total_nodes = 0
    total_edges = 0
    for folder_name, folder_path in all_folders:
        filename = DIMENSION_FILES[dim_name]
        filepath = os.path.join(folder_path, filename)
        if not os.path.exists(filepath):
            continue
        headers, data = read_excel_rows(filepath)
        builder = DIMENSION_BUILDERS[dim_name]
        try:
            nodes, edges = builder(headers, data)
        except Exception:
            continue
        db_module.sync_folder_to_global(global_db, folder_name, nodes, edges)
        total_nodes += len(nodes)
        total_edges += len(edges)

    return jsonify({'success': True, 'folders': len(all_folders), 'nodes': total_nodes, 'edges': total_edges})


@app.route('/api/upload', methods=['POST'])
def upload_folder():
    """上传Excel资料（通道D）：登录用户默认存入个人资料区，勾选共享则存入公共资料库"""
    files = request.files.getlist('files')
    folder_name = os.path.basename(request.form.get('folder_name', 'uploaded_' + uuid.uuid4().hex[:8]))
    share = request.form.get('share') == '1'

    if not files:
        return jsonify({'error': '未上传文件'}), 400

    user = _current_user()
    if share or not user:
        target_dir = os.path.join(UPLOAD_DIR, folder_name)
    else:
        target_dir = os.path.join(_user_upload_dir(user['id']), folder_name)
    os.makedirs(target_dir, exist_ok=True)

    for f in files:
        filename = f.filename
        filename = os.path.basename(filename)
        f.save(os.path.join(target_dir, filename))

    # 培养方案格式识别：自动转换为标准课程体系表（打通维度识别与懒导入）
    plan_source = _ensure_training_plan_standard(target_dir)

    _set_current_folder(target_dir)

    available = []
    for dim_name, filename in DIMENSION_FILES.items():
        filepath = os.path.join(target_dir, filename)
        if os.path.exists(filepath):
            available.append({
                'dimension': dim_name,
                'key': DIMENSION_KEYS[dim_name],
                'filename': filename
            })

    return jsonify({
        'success': True,
        'folder': target_dir,
        'folder_name': folder_name,
        'dimensions': available,
        'training_plan_source': plan_source
    })


@app.route('/api/node-types', methods=['GET'])
def get_node_types():
    """获取所有节点类型及其颜色"""
    return jsonify(NODE_COLORS)


@app.route('/api/catalog', methods=['GET'])
def catalog():
    """实体目录：汇总全部维度全局库中指定类型的节点及其关联边数（供三步选择器第二步下拉）
    查询参数: type=节点类型(如 技能/岗位/专业)，可选 keyword=名称过滤, limit=返回上限(默认200)
    不传 type 时返回所有类型及实体/关联数量（供前端第一步下拉）
    """
    node_type = (request.args.get('type') or request.args.get('dimension') or '').strip()
    keyword = request.args.get('keyword', '').strip().lower()
    try:
        limit = int(request.args.get('limit', '200'))
        limit = max(1, min(limit, 1000))
    except ValueError:
        limit = 200

    # 实体跨维度聚合：同一 id（类型:名称）可能存在于多个维度库，关联数跨库累加
    entities = {}  # id -> {id, label, type, edge_count, source_dimensions}
    type_stats = {}  # type -> {node_count, edge_count}

    for dim_label, dim_key in DIMENSION_KEYS.items():
        global_db = db_module.get_global_db_path(dim_key)
        if not os.path.exists(global_db):
            continue
        db_module.init_global_db(global_db)
        conn = sqlite3.connect(global_db)
        try:
            c = conn.cursor()
            if node_type:
                c.execute("SELECT id, name FROM nodes WHERE type=?", (node_type,))
                rows = [(nid, nm) for nid, nm in c.fetchall() if _is_quality_node(node_type, nm)]
                if not rows:
                    continue
                target_ids = {r[0] for r in rows}
                edge_counts = {r[0]: 0 for r in rows}
                c.execute("SELECT source, target FROM edges")
                for source, target in c.fetchall():
                    if source in target_ids:
                        edge_counts[source] += 1
                    if target in target_ids:
                        edge_counts[target] += 1
                for nid, name in rows:
                    ec = edge_counts.get(nid, 0)
                    if ec <= 0:
                        continue  # 孤立节点不进目录
                    if nid in entities:
                        entities[nid]['edge_count'] += ec
                        if dim_label not in entities[nid]['source_dimensions']:
                            entities[nid]['source_dimensions'].append(dim_label)
                    else:
                        entities[nid] = {
                            'id': nid,
                            'label': name,
                            'type': node_type,
                            'edge_count': ec,
                            'source_dimensions': [dim_label]
                        }
            else:
                # 第一步：按类型统计实体数与关联边数（一条边计入其两端类型各一次，与实体级计数口径一致；统计口径同样过质量闸门）
                c.execute("SELECT id, type, name FROM nodes")
                id_type = {r[0]: r[1] for r in c.fetchall() if _is_quality_node(r[1], r[2])}
                type_nodes = {}
                for _t in id_type.values():
                    type_nodes[_t] = type_nodes.get(_t, 0) + 1
                c.execute("SELECT source, target FROM edges")
                edge_rows = c.fetchall()
                type_edges = {}
                for source, target in edge_rows:
                    ts = id_type.get(source)
                    if ts:
                        type_edges[ts] = type_edges.get(ts, 0) + 1
                    tt = id_type.get(target)
                    if tt:
                        type_edges[tt] = type_edges.get(tt, 0) + 1
                for t, nc in type_nodes.items():
                    if t in type_stats:
                        type_stats[t]['node_count'] += nc
                        type_stats[t]['edge_count'] += type_edges.get(t, 0)
                    else:
                        type_stats[t] = {'type': t, 'node_count': nc, 'edge_count': type_edges.get(t, 0)}
        finally:
            conn.close()

    if not node_type:
        # 系统只面向天津大学，"院校"类型（仅"天津大学"枢纽节点）不作为检索入口展示
        types_list = sorted(type_stats.values(), key=lambda x: -x['node_count'])
        types_list = [t for t in types_list if t['type'] != '院校']
        return jsonify({'success': True, 'types': types_list})

    result = list(entities.values())
    if keyword:
        result = [e for e in result if keyword in e['label'].lower()]
    result.sort(key=lambda x: (-x['edge_count'], x['label']))
    return jsonify({
        'success': True,
        'type': node_type,
        'total': len(result),
        'entities': result[:limit]
    })


# 专业 → 推荐就业岗位（跨库桥接）
# 六个全局库之间不存在 专业→岗位 的关联通路（三套技能词表互不相交），搜索专业时岗位无法
# 经 BFS 可达。此处注入人工梳理的推荐映射（source_level=L3，仅作推荐，不代表数据来源）。
# 岗位名必须与 job_cluster 全局库的岗位节点名完全一致（节点 id = 岗位:名称）。
MAJOR_JOB_BRIDGES = {
    '电气工程及其自动化': [
        '电气工程技术人员', '发电工程技术人员', '变电工程技术人员', '供用电工程技术人员',
        '电力工程安装工程技术人员', '自动控制工程技术人员', '硬件研发工程师',
        '嵌入式硬件开发工程师', '机器人电气工程师', '新能源电控工程师'
    ],
    '智能电网信息工程': [
        '电气工程技术人员', '变电工程技术人员', '供用电工程技术人员', '发电工程技术人员',
        '电力可靠性管理员', '电力聚合运营员', '电力工程安装工程技术人员', '计算机网络工程技术人员'
    ],
    '电子信息工程': [
        '电子工程技术人员', '电子元器件工程技术人员', '电子仪器与电子测量工程技术人员',
        '集成电路工程技术人员', '集成电路设计工程师', '芯片工程师', '硬件研发工程师',
        '嵌入式硬件开发工程师', '汽车电子工程师', '电子电路设计师'
    ],
    '通信工程': [
        '通信工程技术人员', '信息和通信工程技术人员', '计算机网络工程技术人员',
        '信息通信网络机务员', '信息通信网络运行管理人员', '信息安全工程技术人员',
        '电缆光缆工程技术人员', '信息系统运行维护工程技术人员'
    ],
    '自动化': [
        '自动控制工程技术人员', '电气工程技术人员', '智能制造工程技术人员',
        '机器人电气工程师', '飞行器自动控制算法工程师', '自动化测试工程师',
        '嵌入式系统设计工程技术人员', '智能硬件装调员'
    ],
    '物联网工程': [
        '物联网工程技术人员', '嵌入式软件开发工程师', '嵌入式硬件开发工程师',
        '计算机网络工程技术人员', '网络安全工程师', '信息通信网络运行管理员',
        '智能硬件装调员', '工业互联网嵌入式开发工程师'
    ]
}


def _build_subgraph(keyword, center, depth, dim_filter):
    """跨维度子图构建（/api/search-cross-dimension 与 /api/sources 共用）
    节点 id 全局统一（类型:名称），同名同类型实体跨库自动归并；
    先把各维度合并为一张联合图，再在联合图上做 N 跳 BFS——跨库链路（如 竞赛→专业→课程）
    经共享节点自然连通，另注入 专业→推荐岗位 桥接边（MAJOR_JOB_BRIDGES）。
    返回 (nodes, edges, matched_count)
    """
    keywords = None
    if not center:
        keywords = [k.lower() for k in keyword.split() if k.strip()]
        if not keywords:
            return [], [], 0

    dim_labels = {
        'job_cluster': '岗位聚集',
        'skill_structure': '技能结构',
        'career_path': '发展路径',
        'course_system': '课程体系',
        'job_coopetition': '岗位竞合',
        'competition_practice': '竞赛实践'
    }

    # —— 第一阶段：合并各维度为联合图（跨库同实体节点按 id 归并，属性/来源维度合并）——
    union_nodes = {}   # id -> node（含 source_dimensions）
    union_adj = {}     # id -> [neighbor_id]（按维度顺序+入库顺序，结果稳定）
    edge_by_key = {}   # "src->tgt:rel" -> (edge, dim_label) 跨库去重
    for dim_key, dim_label in dim_labels.items():
        if dim_filter != 'all' and dim_key != dim_filter:
            continue
        global_db = db_module.get_global_db_path(dim_key)
        if not os.path.exists(global_db):
            continue
        db_module.init_global_db(global_db)
        nodes, edges = db_module.get_global_graph_data(global_db)

        for node in nodes:
            nid = node['id']
            if nid in union_nodes:
                existing = union_nodes[nid]
                existing['properties'] = db_module._merge_properties(
                    existing.get('properties', {}), node.get('properties', {}))
                if dim_label not in existing['source_dimensions']:
                    existing['source_dimensions'].append(dim_label)
            else:
                node['source_dimensions'] = [dim_label]
                union_nodes[nid] = node
        for edge in edges:
            ekey = f"{edge['source']}->{edge['target']}:{edge.get('relation', '')}"
            if ekey not in edge_by_key:
                edge_by_key[ekey] = (edge, dim_label)
                union_adj.setdefault(edge['source'], []).append(edge['target'])
                union_adj.setdefault(edge['target'], []).append(edge['source'])

    # —— 三中心内容质量过滤（反馈：通识课/无门槛岗位/软技能不进入图谱展示与BFS，存量库数据在展示层拦截）——
    drop_ids = {nid for nid, node in union_nodes.items()
                if not _is_quality_node(node.get('type') or node.get('category') or '', node.get('name'))}
    if drop_ids:
        for nid in drop_ids:
            union_nodes.pop(nid, None)
            union_adj.pop(nid, None)
        for src in list(union_adj.keys()):
            remain = [t for t in union_adj[src] if t not in drop_ids]
            if remain:
                union_adj[src] = remain
            else:
                union_adj.pop(src)
        for ekey in list(edge_by_key.keys()):
            e0 = edge_by_key[ekey][0]
            if e0['source'] in drop_ids or e0['target'] in drop_ids:
                edge_by_key.pop(ekey)

    # —— 跨库桥接边：专业 → 推荐就业岗位 ——
    for major, job_names in MAJOR_JOB_BRIDGES.items():
        major_id = f'专业:{major}'
        if major_id not in union_nodes:
            continue
        for jname in job_names:
            job_id = f'岗位:{jname}'
            if job_id not in union_nodes:
                continue
            ekey = f'{major_id}->{job_id}:就业方向'
            if ekey not in edge_by_key:
                edge_by_key[ekey] = ({
                    'source': major_id, 'target': job_id, 'relation': '就业方向',
                    'label': '推荐就业方向', 'support': [], 'page': [],
                    'source_level': 'L3', 'attrs': {}
                }, '岗位聚集')
                union_adj.setdefault(major_id, []).append(job_id)
                union_adj.setdefault(job_id, []).append(major_id)

    # —— 第二阶段：匹配种子（center 按 id 精确取中心；keyword 按名称+类型 AND 匹配）——
    matched_ids = set()
    for nid, node in union_nodes.items():
        if center:
            if nid == center:
                matched_ids.add(nid)
            continue
        name_lower = node['name'].lower()
        type_lower = (node.get('type') or '').lower()
        if all(kw in name_lower or kw in type_lower for kw in keywords):
            matched_ids.add(nid)

    if not matched_ids:
        return [], [], 0

    # —— 第三阶段：联合图 N跳 BFS（跨库链路自然连通）——
    # level 0 = 匹配节点, level 1 = 一跳邻居, level 2 = 二跳, level 3 = 三跳
    # 二跳及以上限制每个父节点的扇出，防止枢纽节点把大量无关节点拉入子图
    MAX_FANOUT = 25
    current_level = set(matched_ids)
    expanded_ids = set(matched_ids)
    node_level = {}  # node_id -> 层级(0=匹配, 1=一跳, 2=二跳, 3=三跳)
    for nid in matched_ids:
        node_level[nid] = 0

    for level in range(1, depth + 1):
        next_level = set()
        for nid in current_level:
            neighbors = union_adj.get(nid, [])
            if level >= 2 and len(neighbors) > MAX_FANOUT:
                neighbors = neighbors[:MAX_FANOUT]
            for neighbor in neighbors:
                # 邻居必须是联合图中真实存在的节点，避免悬空边
                if neighbor in union_nodes and neighbor not in expanded_ids:
                    expanded_ids.add(neighbor)
                    node_level[neighbor] = level
                    next_level.add(neighbor)
        current_level = next_level
        if not current_level:
            break

    # —— 第四阶段：输出子图 ——
    all_nodes = []
    for nid, node in union_nodes.items():
        if nid not in expanded_ids:
            continue
        node['matched'] = nid in matched_ids
        node['level'] = node_level.get(nid, 0)
        all_nodes.append(node)

    all_edges = []
    for edge, dim_label in edge_by_key.values():
        if edge['source'] in expanded_ids and edge['target'] in expanded_ids:
            edge['source_dimension'] = dim_label
            all_edges.append(edge)

    return all_nodes, all_edges, len(matched_ids)


@app.route('/api/search-cross-dimension', methods=['GET'])
def search_cross_dimension():
    """跨维度综合检索：搜索全部或指定维度的全局库，匹配关键词的节点及其N跳关联节点边合并为一个图
    查询参数: keyword=关键词 或 center=中心节点id(类型:名称), depth=展开层数(1/2/3, 默认1), dim=维度key(all/job_cluster/...)
    center 模式（D7）：用户在目录中选定的实体即搜索中心（level 0），与边存储方向解耦（BFS双向遍历）
    """
    keyword = request.args.get('keyword', '').strip()
    center = request.args.get('center', '').strip()
    if not keyword and not center:
        return jsonify({'error': '请输入搜索关键词或指定中心节点'}), 400

    try:
        depth = int(request.args.get('depth', '1'))
        depth = max(1, min(depth, 3))
    except ValueError:
        depth = 1

    dim_filter = request.args.get('dim', 'all')

    nodes, edges, matched_count = _build_subgraph(keyword, center, depth, dim_filter)

    return jsonify({
        'keyword': keyword,
        'center': center,
        'depth': depth,
        'dim': dim_filter,
        'nodes': nodes,
        'edges': edges,
        'matched_count': matched_count
    })


@app.route('/api/search', methods=['GET'])
def unified_search():
    """统一搜索（三步搜索的定向扩展子图接口）
    查询参数:
        seed=类型:名称    实体级精确命中（/api/catalog 返回的实体 id），如 专业:电气工程及其自动化
        q=关键词          高级·自由词模式（子串匹配），与 seed 二选一
        depth=1..4        跳数（默认3）
        focus_jobs=A,B    目标岗位实体 id 列表（可选叠加，逗号分隔多选）
        focus_cities=C,D  意向城市实体 id 列表（可选叠加，逗号分隔多选）
    focus 叠加语义：命中的目标岗位/意向城市在子图中标记 focus=true（前端高亮）；
    若其不在 seed 扩展子图内（超出扇出被裁剪），则并入该 focus 节点及其一跳邻域，保证用户所选目标一定可见。
    """
    seed = request.args.get('seed', '').strip()
    keyword = request.args.get('q', '').strip()
    if not seed and not keyword:
        return jsonify({'error': '请指定 seed 实体或输入自由关键词'}), 400

    try:
        depth = int(request.args.get('depth', '3'))
        depth = max(1, min(depth, 4))
    except ValueError:
        depth = 3

    def _split_ids(raw):
        ids = []
        for part in (raw or '').split(','):
            part = part.strip()
            if part:
                ids.append(part)
        return ids

    focus_jobs = _split_ids(request.args.get('focus_jobs', ''))
    focus_cities = _split_ids(request.args.get('focus_cities', ''))

    nodes, edges, matched_count = _build_subgraph(keyword, seed, depth, 'all')

    if focus_jobs or focus_cities:
        node_by_id = {n['id']: n for n in nodes}
        edge_keys = {f"{e['source']}->{e['target']}:{e.get('relation', '')}" for e in edges}
        for fid in focus_jobs + focus_cities:
            if fid in node_by_id:
                node_by_id[fid]['focus'] = True
                continue
            f_nodes, f_edges, _ = _build_subgraph('', fid, 1, 'all')
            if not f_nodes:
                continue
            for n in f_nodes:
                if n['id'] == fid:
                    n['focus'] = True
                if n['id'] in node_by_id:
                    existing = node_by_id[n['id']]
                    for d in n.get('source_dimensions', []):
                        if d not in existing.get('source_dimensions', []):
                            existing.setdefault('source_dimensions', []).append(d)
                else:
                    node_by_id[n['id']] = n
                    nodes.append(n)
            for e in f_edges:
                ekey = f"{e['source']}->{e['target']}:{e.get('relation', '')}"
                if ekey not in edge_keys:
                    edge_keys.add(ekey)
                    edges.append(e)
        matched_count = sum(1 for n in nodes if n.get('matched') or n.get('focus'))

    return jsonify({
        'seed': seed,
        'keyword': keyword,
        'depth': depth,
        'focus_jobs': focus_jobs,
        'focus_cities': focus_cities,
        'nodes': nodes,
        'edges': edges,
        'matched_count': matched_count,
        'focus_matched': sum(1 for n in nodes if n.get('focus'))
    })


def _unwrap_source_field(v):
    """兼容双重JSON编码：字段值可能被多次 json.dumps 成字符串，逐层解包到非字符串为止"""
    while isinstance(v, str):
        s = v.strip()
        if not s or s[0] not in '[{':
            break
        try:
            v = json.loads(s)
        except Exception:
            break
    if v is None or v == '':
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for it in v:
            out.extend(_unwrap_source_field(it))
        return out
    return [v]


@app.route('/api/sources', methods=['GET'])
def get_sources():
    """来源溯源（P4 双层接地）：返回当前子图去重来源列表
    查询参数与统一搜索一致：q=关键词 或 center=中心节点id(类型:名称), depth(1..4)/dim
    每条来源：name=数据来源名；type=url|doc；pages=页码列表；url=网址；edge_count=支撑边数；entities=关联实体名
    """
    keyword = request.args.get('keyword', '').strip()
    center = request.args.get('center', '').strip()
    if not keyword and not center:
        return jsonify({'error': '请输入搜索关键词或指定中心节点'}), 400

    try:
        depth = int(request.args.get('depth', '1'))
        depth = max(1, min(depth, 4))
    except ValueError:
        depth = 1

    dim_filter = request.args.get('dim', 'all')

    nodes, edges, matched_count = _build_subgraph(keyword, center, depth, dim_filter)

    node_names = {n['id']: n.get('name', '') for n in nodes}
    sources = {}

    def _touch(key, patch):
        if key not in sources:
            sources[key] = {'name': patch.get('name', key), 'type': patch.get('type', 'doc'),
                            'pages': [], 'urls': [], 'edge_count': 0, 'entities': []}
        s = sources[key]
        for f in ('pages', 'urls'):
            for v in patch.get(f, []):
                if v and v not in s[f]:
                    s[f].append(v)
        s['edge_count'] += 1
        for ent in patch.get('entities', []):
            if ent and ent not in s['entities'] and len(s['entities']) < 12:
                s['entities'].append(ent)

    for e in edges:
        folders = _unwrap_source_field(e.get('source_folders'))
        supports = _unwrap_source_field(e.get('support'))
        pages = _unwrap_source_field(e.get('page'))
        ents = [node_names.get(e.get('source'), ''), node_names.get(e.get('target'), '')]

        def _touch_support(sup, page):
            sup = str(sup).strip()
            if not sup:
                return
            if sup.startswith('http'):
                _touch(sup, {'name': sup, 'type': 'url', 'urls': [sup], 'entities': ents})
            else:
                _touch(sup, {'name': sup, 'type': 'doc', 'pages': [page] if page else [], 'entities': ents})

        if folders:
            for f in folders:
                fname = str(f).strip()
                if not fname:
                    continue
                if fname.startswith('http'):
                    _touch(fname, {'name': fname, 'type': 'url', 'urls': [fname], 'entities': ents})
                    continue
                if supports:
                    for i, sup in enumerate(supports):
                        page = str(pages[i]).strip() if i < len(pages) else ''
                        _touch_support(sup, page)
                else:
                    _touch(fname, {'name': fname, 'type': 'doc', 'pages': [], 'entities': ents})
        else:
            for i, sup in enumerate(supports):
                page = str(pages[i]).strip() if i < len(pages) else ''
                _touch_support(sup, page)

    src_list = sorted(sources.values(), key=lambda x: -x['edge_count'])
    return jsonify({'keyword': keyword, 'center': center, 'depth': depth, 'dim': dim_filter,
                    'matched_count': matched_count, 'node_count': len(nodes), 'sources': src_list})


_SOURCE_FILE_INDEX = None
_SOURCE_EXTS = ('.pdf', '.html', '.htm', '.xlsx', '.xlsm', '.doc', '.docx', '.png', '.jpg', '.jpeg', '.txt')


def _norm_source_name(name):
    n = str(name).strip()
    n = re.sub(r'（来源：[^）]*）', '', n)
    n = re.sub(r'[《》\s]', '', n)
    return n


def _build_source_file_index():
    """扫描允许的资料目录，建立 归一化名 -> 本地文件/目录 的索引（一次性，进程内缓存）"""
    global _SOURCE_FILE_INDEX
    if _SOURCE_FILE_INDEX is not None:
        return _SOURCE_FILE_INDEX
    index = {'files': {}, 'dirs': {}}
    app_root = os.path.dirname(os.path.abspath(__file__))
    base_dirs = [
        os.path.join(app_root, 'data', 'raw'),
        os.path.join(app_root, 'data', 'extracted'),
        os.path.join(app_root, 'data', 'graphs'),
    ]
    for bd in base_dirs:
        if not os.path.isdir(bd):
            continue
        for root, dirs, files in os.walk(bd):
            for fn in files:
                stem, ext = os.path.splitext(fn)
                if ext.lower() not in _SOURCE_EXTS:
                    continue
                index['files'][_norm_source_name(stem)] = os.path.join(root, fn)
            for d in dirs:
                index['dirs'].setdefault(_norm_source_name(d), os.path.join(root, d))
    _SOURCE_FILE_INDEX = index
    return index


@app.route('/api/source_file', methods=['GET'])
def serve_source_file():
    """按来源名定位并返回本地原件（PDF/网页存档），供溯源面板在新窗口打开（PDF 可加 #page=页码）
    查询参数：name=数据来源名（服务端在允许目录内检索，不接受客户端路径）
    """
    global _SOURCE_FILE_INDEX
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'error': '缺少来源名'}), 400
    if request.args.get('refresh') == '1':
        _SOURCE_FILE_INDEX = None
    index = _build_source_file_index()
    norm = _norm_source_name(name)
    cands = [norm]
    for m in re.findall(r'《([^》]+)》', str(name)):
        c = _norm_source_name(m)
        if c and c not in cands:
            cands.append(c)

    path = None
    for cand in cands:
        dir_path = index['dirs'].get(cand)
        if dir_path:
            for root, dirs, files in os.walk(dir_path):
                for fn in sorted(files):
                    if fn.lower().endswith(_SOURCE_EXTS):
                        path = os.path.join(root, fn)
                        break
                if path:
                    break
        if path:
            break
    if not path:
        for cand in cands:
            path = index['files'].get(cand)
            if path:
                break
    if not path:
        best = None
        for cand in cands:
            if len(cand) < 6:
                continue
            hits = [k for k in index['files'] if (k in cand or cand in k)]
            if hits:
                p = index['files'][max(hits, key=len)]
                if best is None or len(os.path.basename(p)) > len(os.path.basename(best)):
                    best = p
        path = best
    if not path or not os.path.exists(path):
        friendly = (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<title>未找到本地原件</title>'
            '<style>body{font-family:"Microsoft YaHei",sans-serif;background:#f5f7fa;display:flex;'
            'align-items:center;justify-content:center;height:100vh;margin:0}'
            '.card{background:#fff;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.08);'
            'padding:36px 44px;max-width:560px;text-align:center}'
            'h1{font-size:20px;color:#1f2d3d;margin:0 0 12px}'
            'p{font-size:14px;color:#5e6d82;line-height:1.8;margin:6px 0}'
            '.src{color:#2c6ecb;word-break:break-all}</style></head><body>'
            '<div class="card"><h1>📄 未找到该来源的本地原件</h1>'
            '<p>来源名称：<span class="src">' + html_module.escape(str(name)) + '</span></p>'
            '<p>系统在本地资料目录中未能匹配到该文档，可能原因：</p>'
            '<p>① 文档未被导入本地资料目录；② 来源名称与文件名差异较大；③ 该来源为多文档合并标注。</p>'
            '<p>可尝试在左侧「资料导入」面板重新提交该材料的链接或正文。</p>'
            '</div></body></html>'
        )
        return Response(friendly, status=404, mimetype='text/html')
    return send_file(path, conditional=True)


# ===== AI 智能分析 =====
# （LLM_API_URL / LLM_API_KEY / LLM_MODEL 统一在文件顶部配置区定义）

DIM_ANALYSIS_PROMPT = {
    'job_cluster': """你是就业市场与岗位需求分析专家。你的分析目标是帮助求职者（以在校大学生为主）回答以下核心问题：
1. 目标岗位在哪些城市/地区需求最集中？各地区的招聘企业数量和需求热度如何？
2. 哪些行业和企业在大量招聘该岗位？行业分布是否集中？
3. 不同城市/行业的薪资水位差异如何？高薪机会分布在哪里？
4. 岗位需求随年份的变化趋势如何？哪些地区/行业的机会在增长？

你的路径分析应侧重：
- 城市机会分布路径：岗位->招聘于->企业->位于->城市，揭示岗位的地理聚集
- 行业机会分布路径：岗位->招聘于->企业->属于->行业领域，揭示岗位的行业聚集
- 薪资对比路径：同一岗位在不同城市/行业的招聘关系（含需求年份与薪资范围）对比
- 区域就业生态路径：某城市/行业->企业->岗位，揭示区域就业生态全貌

表格建议：岗位机会分布对比表（城市/行业、招聘企业数、代表企业、薪资范围、需求趋势）""",

    'skill_structure': """你是岗位技能画像分析专家。你的分析目标是帮助求职者回答以下核心问题：
1. 目标岗位需要哪些必备技能？核心技能清单是什么？
2. 这些技能属于什么分类（技能分类/工具平台/认证证书）？各技能的等级要求如何？
3. 哪些技能是高优先级必须掌握的？哪些是加分项？学习难度如何？
4. 掌握某技能后还能胜任哪些相关岗位？技能的可迁移性如何？

你的路径分析应侧重：
- 岗位技能全景路径：岗位->要求->技能（按技能分类分组，分支结构），揭示岗位技能画像
- 技能分级路径：核心技能->等级要求/学习难度，识别学习优先级
- 技能生态路径：技能->使用->工具/平台、技能->可考取->认证证书，构建学习配套
- 技能迁移路径：技能<-要求-多个岗位，揭示一技多岗的可能性

表格建议：岗位技能清单表（技能名称、技能分类、等级要求、学习难度、关联工具/证书）""",

    'career_path': """你是职业发展路径规划专家。你的分析目标是帮助求职者回答以下核心问题：
1. 从起始岗位出发，有哪些晋升/转岗方向？每个方向的职级跃迁如何？
2. 各条路径的转换难度和平均年限是多少？哪条路径性价比最高？
3. 要完成某条路径，需要补充哪些技能？差距有多大？
4. 目标岗位最终通向哪些职业方向？长期发展空间如何？

你的路径分析应侧重：
- 晋升链路路径：起始岗位->晋升/转岗->目标岗位->属于->职业方向，揭示3~5年发展路线
- 难度对比路径：同一岗位的多条出路，按转换难度/平均年限对比排序
- 补技能路径：起始岗位->需补充技能->目标岗位，揭示能力差距与弥补方案
- 方向汇聚路径：多个岗位晋升后汇聚到同一职业方向（分支汇聚结构）

表格建议：发展路径对比表（起始岗位、目标岗位、转换难度、平均年限、需补充技能、目标职业方向）""",

    'course_system': """你是专业培养与就业衔接分析专家。你的分析目标是帮助在校大学生回答以下核心问题：
1. 我的专业包含哪些课程？哪些是核心课程？按学年/学期如何分布？
2. 每门课程培养哪些技能？这些技能被哪些目标岗位要求？
3. 专业课程与目标岗位要求之间的差距如何？哪些技能是课程没覆盖、需要自学或竞赛补充的？
4. 从专业到岗位的最短衔接路径是什么？应该优先补哪些课程/技能？

你的路径分析应侧重：
- 培养链主路径：院校->开设->专业->包含->课程->培养->技能，揭示从课堂到岗位的完整链路
- 岗位对接路径：岗位->要求->技能<-培养-课程，对比课程培养与岗位要求的匹配与缺口
- 时序规划路径：按课程的"开课学年/开课学期"属性给出四年修读建议
- 补短板路径：目标岗位要求但专业课程未培养的技能，识别自学清单与可参加的竞赛

表格建议：专业-岗位衔接分析表（技能名称、培养课程、要求岗位、覆盖情况、是否需自学/竞赛补充）""",

    'competition_practice': """你是竞赛实践与能力提升规划专家。你的分析目标是帮助在校大学生回答以下核心问题：
1. 哪些竞赛/实践活动适合我的专业与年级？其级别、主办方、时间节点如何？
2. 参加某项竞赛能锻炼哪些技能？这些技能对应哪些岗位要求？
3. 面向目标岗位，应该规划怎样的竞赛路线图（按时间节点与技能缺口排序）？

你的路径分析应侧重：
- 锻炼路径：竞赛->锻炼->技能->要求<-岗位，揭示竞赛经历如何转化为岗位竞争力
- 专业适配路径：竞赛->面向->专业，筛选与本专业匹配的竞赛清单
- 时间规划路径：按竞赛"时间节点"属性与学年安排给出参赛时间表

表格建议：竞赛规划表（竞赛名称、级别、时间节点、锻炼技能、关联岗位、建议参赛时间）""",

    'job_coopetition': """你是岗位竞争与合作格局分析专家。你的分析目标是帮助求职者回答以下核心问题：
1. 目标岗位与哪些岗位技能高度重叠？各岗位的重叠度数值和关系判定（高重叠·可互转/部分互补/差异化互补）如何？
2. 哪些岗位与目标岗位可互转？转岗依托的技能基础是什么？
3. 目标岗位的核心技能与相邻岗位有何差异？差异化竞争优势在哪里？
4. 与目标岗位共享同一核心技能的岗位群有哪些？多手准备的可能性如何？

你的路径分析应侧重：
- 可互转路径：岗位A->技能重叠->岗位B（优先选取关系判定为"高重叠·可互转"的边），揭示转岗可行性
- 技能底座路径：岗位A/岗位B->核心技能->同一技能，揭示岗位间的共同技能底座
- 差异化路径：对比相邻岗位的技能构成差异，识别目标岗位的独有竞争力
- 岗位群路径：某技能->被多个岗位共享（分支结构），评估技能的岗位覆盖面

表格建议：岗位竞合关系表（岗位A、岗位B、技能重叠度、重叠度数值、关系判定、共同核心技能）""",

    'all': """你是职业规划综合分析专家。你的分析目标是帮助求职者（以在校大学生为主）基于知识图谱数据制定个性化职业规划，分析必须覆盖以下六个部分：
1.【技能盘点】从用户输入中提取已掌握的技能与背景（专业/课程/竞赛/证书），并在图谱数据中核实这些技能是否存在、关联哪些岗位
2.【适配岗位清单】列出用户技能可胜任的岗位，标注岗位的招聘城市分布与薪资范围
3.【能力差距分析】对比目标岗位的技能要求与用户已有技能，明确缺失项（优先参考"要求""需补充技能"关系）
4.【学习路径建议】针对差距技能，给出可培养该技能的课程/专业/认证证书建议
5.【3~5年发展路线】基于"晋升/转岗"关系给出起始岗位->目标岗位->职业方向的发展链，标注转换难度与平均年限
6.【依据来源】每条结论标注其依据的数据来源（数据行中的"支撑"字段），保证可追溯

这六个部分融入下方JSON结构：summary承担第1部分（技能盘点与总体结论），paths的标题与内容按第2~5部分组织（适配岗位、能力差距、学习路径、发展路线各至少一条），table承担第6部分对应的个人行动清单。

你的路径分析应侧重：
- 人岗匹配路径：用户技能->胜任->岗位->招聘于->企业->位于->城市（分支结构），揭示适配面
- 差距弥补路径：目标岗位->要求->缺失技能<-培养-课程/竞赛-锻炼
- 发展路线路径：起始岗位->晋升/转岗->目标岗位->属于->职业方向
- 多手准备路径：用户核心技能->多个可胜任岗位（备选方案）

表格建议：个人职业行动清单表（行动项、针对的差距技能、学习资源（课程/证书）、目标岗位、优先级）"""
}


REPORT_MODE_PROMPTS = {
    'm1': """本次生成【岗位对标·培养路径报告】（M1）：target_profile 突出该岗位的职责、技能等级要求、需求城市与薪资水位；four_year_plan 围绕"达到该岗位要求"组织课程/技能/竞赛条目；gap_next 对照岗位要求给出补齐动作。""",

    'm2': """本次生成【专业出口·培养路径报告】（M2）：four_year_plan 是报告核心，直接采用输入数据中课程的开课学年+开课学期属性，按"大一秋→大四春"归位（必修课优先），并给每学期挂上该阶段应掌握的技能与适合的竞赛；target_profile 改写为"专业概览+主要出口岗位"。""",

    'm3': """本次生成【技能评估·培养路径报告】（M3）：target_profile 改写为"技能现状核实"（目标技能在图谱中的等级要求与关联岗位）；four_year_plan 保持完整时序但突出该技能相关的课程与竞赛；gap_next 是重点，给出把该技能补齐到目标水平的具体动作。""",

    'm4': """本次生成【个性培养·思维导图报告】（M4）：核心交付是顶层附加字段 mindmap（培养方案思维导图），按"大类通识课/专业必修课/专业选修课/选修课/竞赛实践"五大分支组织，每个分支下按大一~大四分年列出课程与竞赛时间节点；四段式结构（target_profile/four_year_plan/action_list/table/gap_next）仍须完整输出，four_year_plan 与 mindmap 的课程归位保持一致；target_profile 改写为"个人培养画像"（当前年级、目标岗位、培养方案课程量与竞赛节奏）。学期归位铁律：课程只按节点属性"开课学年/开课学期"归位（逗号多值=多学期都上，逐值归位），禁止猜测学期、禁止把课程默认堆到同一学期。竞赛实践分支禁止整体留空：数据中的竞赛/实践节点优先采用，四年合计不足4条时用该专业广为人知的竞赛/实践建议补足并逐条尾注"（AI建议）"。若用户给定目标岗位，专业选修课分支必须从数据中真实存在的选修课里给出"建议优先"标记与选择理由，禁止编造不存在的课程名。""",

    'm5': """本次生成【职业方向·多路径报告】（M5）：核心交付是顶层附加字段 career_paths（职业方向路径集），覆盖就业（走向不同岗位）、升学读研、直博、考研、选调、支教保研、干部保研等路径类型（按数据与常识取5-8条，与数据明显无关的路径省略）；每条路径给出分阶段清晰路线（period+action步骤），事实步骤基于图谱数据并标注来源，规划性步骤开放生成；四段式结构仍须完整输出，four_year_plan 保持时序主干，gap_next.alternatives 与 career_paths 的路径名呼应；target_profile 改写为"个人现状与方向盘点"。"""
}

GROUNDING_RULES = """

【分层接地规则（违反即为不合格输出）】
- 事实条目（target_profile 全部条目，及 four_year_plan/action_list 中 tag=course/skill/comp 的条目）：凡复述图谱内数据（课程、技能要求、竞赛信息、薪资、年限、级别、城市分布等），只能基于送入的子图数据；句尾标注 [来源：数据来源名 页号/URL]，来源名与页号逐字取自关联关系描述中的"来源"和"页"字段；来源本身没有页号/网址时只写来源名（如 [来源：天大电自培养方案2025]），页号位置禁止写"无""未知"或留空占位；禁止增加图谱外事实；图谱未覆盖的事实保持沉默，交给 tag=advice 的AI建议处理，不要写"图谱暂未覆盖"之类的声明。
- AI建议（tag=advice 条目、timepoints、pitfalls、top3、alternatives）：开放生成、无需来源标注；具体资源名放开——竞赛名、证书名、工具名、书籍名、课程类型名，只要该领域真实广为人知即可写具体名称（如"全国大学生电子设计竞赛""蓝桥杯""LeetCode""全国计算机等级考试二级"），不限白名单；但禁止编造具体企业名（统一用"行业头部企业"）与具体薪资/比例数字（统一用"行业平均薪资水平"）；时间节点可写"一般3月报名、以官网通知为准"式表述，不编造具体网址。
- 三层来源分级（渲染层自动标注，无需你关心）：官方培养方案/国家职业标准类来源为权威层，其他文档来源为多源抽取层，AI建议为建议层；三者不得混排——事实条目不得伪装成AI建议，AI建议不得伪装成事实条目。"""

MODE_LABELS = {'m1': '岗位对标', 'm2': '专业出口', 'm3': '技能评估', 'm4': '个性培养', 'm5': '职业方向'}


def detect_report_mode(mode, keyword, nodes, center_id):
    # 报告模式收敛为两个：m4个性培养 / m5职业方向
    # auto 判定规则：搜索对象是岗位 → 职业方向(m5)；专业/技能/其他 → 个性培养(m4)
    if mode == 'm5':
        return 'm5'
    if mode == 'm4':
        return 'm4'
    if mode in MODE_LABELS:
        # 兼容旧模式显式传入（UI已不提供）：岗位对标→职业方向，专业出口/技能评估→个性培养
        return 'm5' if mode == 'm1' else 'm4'

    def _is_job_id(nid):
        return str(nid or '').split(':', 1)[0] == '岗位'

    if center_id and _is_job_id(center_id):
        return 'm5'
    if keyword:
        for n in nodes:
            if n.get('name') == keyword and _is_job_id(n.get('id')):
                return 'm5'
        partial = [n for n in nodes
                   if keyword in (n.get('name') or '') and _is_job_id(n.get('id'))]
        if len(partial) == 1:
            return 'm5'
    return 'm4'


_GENERIC_WORDS = set("""目标 相关 对应 核心 必备 加分 缺失 差距 匹配 适配 综合 通用 其他 特定 专项 特色 主要 常见 热门 基础 进阶 高级 初级 中级
学习 掌握 具备 拥有 需要 需要|所需 提升 增强 发展 未来 现有 已有 这些 那些 哪些 什么 以上 以下 上述 不同 各类 多个 多种 单个 一些 某些
软 硬 技术 业务 管理 语言 工具 平台 系统 方向 领域 类别 类型 级别 等级 层次 阶段 过程 内容 要求 标准 能力 素养 经验 背景 条件 优势 劣势
一门 每门 同 该 此 各 每 类 个 人 年 月 日 今 明 后 新 旧 大 中 小 高 低 快 慢 好 强 弱 多 少 全 部 分 组""".split())

_SUGGEST_PREFIX_RE = re.compile(r'^\s*(建议|推荐|应该|应当|可以|不妨|优先|最好|尝试|考虑|规划|宜|可先|建议您|你可以|您可以|最好能|不妨先)')
_ENTITY_SUFFIX_RE = re.compile(r'([A-Za-z0-9+#./\-]{2,30}|[\u4e00-\u9fa5A-Za-z0-9+#./\-]{2,25}?)(?:技能|课程|岗位|专业|竞赛|大赛|比赛|证书|职业方向)')
_ADVICE_ENTITY_RE = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9+#./\-]{2,25}?)(?:课程|证书|认证|培训班)')
_ADVICE_NUM_RE = re.compile(r'\d+(?:\.\d+)?\s*[kKwW万千]\s*元?(/月)?|\d+\s*%|年薪\s*\d+|月薪\s*\d+')
_KNOWN_PUBLIC_RESOURCES = (
    '全国大学生电子设计竞赛', '电子设计竞赛', '电赛', '蓝桥杯',
    '全国大学生数学建模竞赛', '数学建模竞赛', '互联网+大学生创新创业大赛', '互联网+', '挑战杯',
    '全国大学生信息安全竞赛', '全国大学生智能汽车竞赛', '智能车竞赛', 'RoboMaster',
    '全国大学生工程训练综合能力竞赛', '全国大学生集成电路创新创业大赛',
    'ACM-ICPC', 'ACM', 'ICPC', 'LeetCode', '力扣', 'GitHub', 'Kaggle',
    '注册电气工程师', '大学英语四六级', 'CET-4', 'CET-6', '雅思', '托福',
    'MATLAB', 'Python', 'C语言', 'STM32', 'FPGA', 'Keil', 'Altium Designer', 'AutoCAD', 'Vue', 'Spring',
)
_LEADING_NOISE_RE = re.compile(r'^(学习|掌握|具备|拥有|相关|核心|必备|主要|常见|热门|对应|目标|所需|该|此|各类|某些|这些|那些)+')
_AGG_COUNT_RE = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9+#.]{2,25}?)\s*[（(]\s*(\d+)\s*条关联\s*[)）]')


def _split_sentences(text):
    parts = re.split(r'(?<=[。！？；\n])', text)
    return [p for p in parts if p.strip()]


def _norm_field(v):
    if isinstance(v, list):
        return [x for x in v if x]
    return [v] if v else []


def _clean_source_tag(s):
    return re.sub(r'（来源：[^）]*）', '', str(s)).strip(' -–—_')


def _edge_source_label(e):
    folders = _norm_field(e.get('source_folders'))
    pages = [str(p).strip("\"' \t\u201c\u201d\u2018\u2019") for p in _norm_field(e.get('page'))]
    pages = [p for p in pages if p and re.search(r'\d', p)]
    support = e.get('support') or ''
    if isinstance(support, list):
        support = '；'.join([s for s in support if s])
    if folders:
        src = _clean_source_tag(folders[0])
        if pages:
            return f'[来源：{src} P{pages[0]}]'
        if support.startswith('http'):
            return f'[来源：{src} URL]'
        return f'[来源：{src}]'
    if support.startswith('http'):
        return f'[来源：{str(support)[:60]}]'
    return None


_ENTITY_FUNC_CHARS = set('的了与和或等及在对从向以是有不都很更最该此又就地把被让使并而则即也就否太还较于其之所需类供者')


def _check_entity_whitelist(sentence, node_names):
    cands = _ENTITY_SUFFIX_RE.findall(sentence)
    for cand in cands:
        if any(ch in _ENTITY_FUNC_CHARS for ch in cand):
            continue
        cand = cand.strip('的了与和或等及其')
        cand = _LEADING_NOISE_RE.sub('', cand)
        if not cand or len(cand) < 2:
            continue
        if cand in node_names or cand in _GENERIC_WORDS:
            continue
        if any(cand in name or name in cand for name in node_names):
            continue
        return cand
    return None


def _advice_violation(item, node_names):
    """建议层轻校验：拦截编造的具体课程/证书名与薪资、百分比数字；公认竞赛/工具/书籍名放行。"""
    for cand in _ADVICE_ENTITY_RE.findall(item):
        c = _LEADING_NOISE_RE.sub('', cand.strip('的了与和或等及其'))
        if not c or len(c) < 2 or c in _GENERIC_WORDS:
            continue
        if any(c in r or r in c for r in _KNOWN_PUBLIC_RESOURCES):
            continue
        if c in node_names:
            continue
        if any(c in name or name in c for name in node_names):
            continue
        return c
    if _ADVICE_NUM_RE.search(item):
        return '编造数字'
    return None


_ADVICE_CLICHE_WORDS = ('扎实掌握', '积极参与', '提升综合素质', '要努力学习', '认真学习', '打好坚实基础')


def _strip_cliche_sentences(item):
    """建议层句级去套话：删除含空话套话的句子，返回(清理后文本, 删除句数)。"""
    kept, removed = [], 0
    for s in _split_sentences(item):
        if any(w in s for w in _ADVICE_CLICHE_WORDS):
            removed += 1
            continue
        kept.append(s)
    return ''.join(kept), removed


def _process_sentence(s, node_names, node_map, edge_by_node, stats):
    def _edge_count(nid):
        return len(edge_by_node.get(nid, []))

    def _fix_agg(match):
        name, num = match.group(1), match.group(2)
        n = node_map.get(name)
        if not n:
            real_counts = [len(edge_by_node.get(node_map[m]['id'], [])) for m in node_map if name in m]
            if not real_counts:
                return match.group(0)
            real = max(real_counts)
        else:
            real = _edge_count(n['id'])
        if int(num) != real:
            stats['fixed_counts'] += 1
            return f'{name}（{real} 条关联）'
        return match.group(0)

    s2 = _AGG_COUNT_RE.sub(_fix_agg, s)

    viol = _check_entity_whitelist(s2, node_names)
    if viol:
        stats['deleted_samples'].append({'reason': 'whitelist', 'violation': viol, 'sentence': s[:100]})
        return None, viol

    names_in_s = [name for name in node_names if name in s2]
    if names_in_s and '来源' not in s2:
        src = None
        for name in names_in_s:
            n = node_map.get(name)
            if not n:
                continue
            for e in edge_by_node.get(n['id'], []):
                src = _edge_source_label(e)
                if src:
                    break
            if src:
                break
        if src:
            s2 = s2.rstrip() + src
            stats['patched_sources'] += 1
        else:
            # 兜底：边来源缺失时改用节点的数据来源文件夹标注（仍可追溯），避免误删含真实节点名的句子
            for name in names_in_s:
                n = node_map.get(name)
                if not n:
                    continue
                folders = n.get('source_folders') or []
                if isinstance(folders, str):
                    folders = [folders]
                if folders:
                    src = f'[来源：{_clean_source_tag(folders[0])}]'
                    break
            if src:
                s2 = s2.rstrip() + src
                stats['patched_sources'] += 1
            else:
                stats['deleted_sentences'] += 1
                stats['deleted_samples'].append({'reason': 'no_source', 'violation': '', 'sentence': s[:100]})
                return None, None

    if _SUGGEST_PREFIX_RE.match(s2) and 'AI建议' not in s2:
        s2 = '「AI建议」' + s2.lstrip()
        stats['patched_ai_tags'] += 1

    return s2, None


def validate_report(analysis, node_map, edges, llm_rewrite=None, user_grade=None):
    stats = {'invalid_items_removed': 0, 'deleted_sentences': 0, 'patched_sources': 0, 'patched_ai_tags': 0,
             'fixed_counts': 0, 'rewritten_sentences': 0, 'advice_items_removed': 0,
             'cliche_sentences_removed': 0, 'length_trimmed': 0, 'replaced_cells': 0,
             'course_downgraded': 0, 'bad_tags': 0, 'bad_stages': 0, 'missing_semesters': [],
             'action_backfilled': 0, 'deleted_samples': []}

    node_names = sorted({n['name'] for n in node_map.values() if n.get('name') and len(n.get('name')) >= 2},
                        key=len, reverse=True)
    name_map = {}
    course_names = set()
    for n in node_map.values():
        nm = n.get('name')
        if not nm:
            continue
        if nm not in name_map:
            name_map[nm] = n
        if '课程' in str(n.get('type') or ''):
            course_names.add(nm)
    edge_by_node = {}
    for e in edges:
        for k in (e.get('source'), e.get('target')):
            edge_by_node.setdefault(k, []).append(e)

    _SEM_RE = re.compile(r'^\s*(大一|大二|大三|大四)\s*[·•・.、\-\s]*\s*(秋|春|夏|短[^，。；\s]{0,3})\s*$')
    _YEAR_RE = re.compile(r'^\s*(大一|大二|大三|大四)\s*$')
    _BOOK_RE = re.compile(r'《([^》]{2,30})》')

    def normalize_stage(stage):
        s = str(stage or '').strip()
        m = _SEM_RE.match(s)
        if m:
            sem = m.group(2)
            return f'{m.group(1)}·{"短" if sem.startswith("短") else sem}'
        if _YEAR_RE.match(s):
            return s
        return None

    def check_course_entry(text):
        if not course_names:
            return True
        for c in _BOOK_RE.findall(text or ''):
            if c in course_names or any(c in nm2 or nm2 in c for nm2 in course_names):
                return True
        return any(nm2 in (text or '') for nm2 in course_names)

    def process_text(text):
        if not text or not isinstance(text, str):
            return text, []
        out, removed = [], []
        for s in _split_sentences(text):
            s2, viol = _process_sentence(s, node_names, name_map, edge_by_node, stats)
            if viol is not None or s2 is None:
                if s2 is None and viol is None:
                    removed.append(s)
                elif viol is not None:
                    removed.append(s)
            else:
                out.append(s2)
        return ''.join(out), removed

    def process_table(table):
        if not isinstance(table, dict):
            return []
        removed = []
        rows = table.get('rows') or []
        new_rows = []
        for row in rows:
            new_row = []
            for cell in row:
                cell_text = str(cell)
                c2, removed_cells = process_text(cell_text)
                if removed_cells and c2.strip() == '':
                    c2 = '——'
                    stats['replaced_cells'] += 1
                new_row.append(c2 if c2.strip() else cell_text)
            new_rows.append(new_row)
        table['rows'] = new_rows
        return removed

    all_removed = []
    def protect(text_kept, orig):
        if len(text_kept.strip()) < 0.4 * len(orig.strip()) and len(orig.strip()) >= 100:
            return orig, True
        return text_kept, False
    if analysis.get('summary'):
        orig = analysis['summary']
        analysis['summary'], rm = process_text(analysis['summary'])
        restored, fellback = protect(analysis['summary'], orig)
        if fellback:
            analysis['summary'] = restored
            analysis['summary_fallback'] = True
        else:
            all_removed += [('summary', None, s) for s in rm]

    def process_advice_item(it):
        if not isinstance(it, str) or not it.strip():
            return None
        if _advice_violation(it, node_names):
            stats['advice_items_removed'] += 1
            return None
        cleaned_it, n_cliche = _strip_cliche_sentences(it)
        if n_cliche:
            stats['cliche_sentences_removed'] += n_cliche
        return cleaned_it.strip() or None

    tp = analysis.get('target_profile')
    if isinstance(tp, dict) and isinstance(tp.get('items'), list):
        kept_items = []
        for it in tp['items']:
            if not isinstance(it, str) or not it.strip():
                continue
            orig = it
            new_text, rm = process_text(orig)
            restored, fellback = protect(new_text, orig)
            if fellback:
                kept_items.append(restored)
                continue
            all_removed += [('target_profile', None, s) for s in rm]
            if new_text.strip():
                kept_items.append(new_text)
            else:
                stats['invalid_items_removed'] += 1
        tp['items'] = kept_items

    plan = analysis.get('four_year_plan')
    if isinstance(plan, list):
        new_plan = []
        for it in plan:
            if not isinstance(it, dict) or not str(it.get('text') or '').strip():
                stats['invalid_items_removed'] += 1
                continue
            tag = str(it.get('tag') or 'advice').strip().lower()
            if tag not in ('course', 'skill', 'comp', 'advice'):
                tag = 'advice'
                stats['bad_tags'] += 1
            stage = normalize_stage(it.get('stage'))
            if stage is None:
                stats['bad_stages'] += 1
                stage = str(it.get('stage') or '未标注').strip()
            it['stage'] = stage
            it['tag'] = tag
            if tag == 'course' and not check_course_entry(it['text']):
                it['tag'] = 'advice'
                tag = 'advice'
                stats['course_downgraded'] += 1
            if tag == 'advice':
                cleaned = process_advice_item(it['text'])
                if cleaned is None:
                    stats['invalid_items_removed'] += 1
                    stats['deleted_samples'].append({'reason': 'plan_advice', 'sentence': str(it.get('text'))[:100]})
                    continue
                it['text'] = cleaned
            else:
                orig = it['text']
                new_text, rm = process_text(orig)
                restored, fellback = protect(new_text, orig)
                if fellback:
                    it['text'] = restored
                else:
                    all_removed += [('four_year_plan', it, s) for s in rm]
                    if not new_text.strip():
                        stats['invalid_items_removed'] += 1
                        continue
                    it['text'] = new_text
            new_plan.append(it)
        analysis['four_year_plan'] = new_plan
        covered = {it2.get('stage') for it2 in new_plan}
        _scope_years = ('大一', '大二', '大三', '大四')[('大一', '大二', '大三', '大四').index(user_grade):] if user_grade in ('大一', '大二', '大三', '大四') else ('大一', '大二', '大三', '大四')
        for y in _scope_years:
            for sm in ('秋', '春'):
                if f'{y}·{sm}' not in covered:
                    stats['missing_semesters'].append(f'{y}·{sm}')

    al = analysis.get('action_list')
    new_al = []
    if isinstance(al, list):
        for sec in al:
            if not isinstance(sec, dict):
                continue
            new_sec = {'stage': str(sec.get('stage') or '未标注').strip()}
            for key in ('goals', 'courses', 'skills', 'comps', 'pitfalls'):
                kept = []
                for it3 in (sec.get(key) or []):
                    cleaned = process_advice_item(it3)
                    if cleaned is not None:
                        kept.append(cleaned)
                new_sec[key] = kept
            new_al.append(new_sec)

    # 反馈5：行动项被清洗为空时从four_year_plan同学年条目兜底回填；action_list整体缺失时按学年合成
    _plan_items = [it6 for it6 in (analysis.get('four_year_plan') or [])
                   if isinstance(it6, dict) and str(it6.get('text') or '').strip()]

    def _plan_bucket(year):
        same = sorted([it6 for it6 in _plan_items if str(it6.get('stage') or '').startswith(year)],
                      key=lambda x: str(x.get('stage') or ''))
        courses = [str(it6['text']).strip() for it6 in same if str(it6.get('tag') or '') == 'course']
        advices = [str(it6['text']).strip() for it6 in same if str(it6.get('tag') or '') != 'course']
        return courses, advices

    for sec in new_al:
        m_y = re.search(r'大一|大二|大三|大四', str(sec.get('stage') or ''))
        if not m_y:
            continue
        courses, advices = _plan_bucket(m_y.group(0))
        _filled = False
        for key in ('goals', 'skills', 'comps', 'pitfalls'):
            if not sec.get(key) and advices:
                sec[key] = [advices.pop(0)]
                _filled = True
        if not sec.get('courses') and courses:
            sec['courses'] = courses[:2]
            _filled = True
        if _filled:
            stats['action_backfilled'] += 1
    if not new_al and _plan_items:
        for y in ('大一', '大二', '大三', '大四'):
            courses, advices = _plan_bucket(y)
            if not courses and not advices:
                continue
            syn = {'stage': y, 'goals': [], 'courses': [], 'skills': [], 'comps': [], 'pitfalls': []}
            for key in ('goals', 'skills', 'comps', 'pitfalls'):
                if advices:
                    syn[key] = [advices.pop(0)]
            syn['courses'] = courses[:2]
            new_al.append(syn)
            stats['action_backfilled'] += 1
    if new_al:
        new_al.sort(key=lambda s: {'大一': 0, '大二': 1, '大三': 2, '大四': 3}.get(str(s.get('stage') or '')[:2], 9))
    analysis['action_list'] = new_al

    gn = analysis.get('gap_next')
    if isinstance(gn, dict):
        if isinstance(gn.get('status'), str) and gn['status'].strip():
            orig = gn['status']
            new_text, rm = process_text(orig)
            restored, fellback = protect(new_text, orig)
            gn['status'] = restored if fellback else new_text
            if not fellback:
                all_removed += [('gap_status', None, s) for s in rm]
        for key in ('top3', 'alternatives'):
            kept = []
            for it4 in (gn.get(key) or []):
                cleaned = process_advice_item(it4)
                if cleaned is not None:
                    kept.append(cleaned)
            gn[key] = kept
        if isinstance(gn.get('top3'), list) and len(gn['top3']) > 3:
            gn['top3'] = gn['top3'][:3]

    if isinstance(analysis.get('table'), dict):
        rm = process_table(analysis['table'])
        all_removed += [('table', analysis['table'], s) for s in rm]

        # 反馈5：行动清单表"——"占位单元格兜底回填（行动项/差距技能/学习资源）
        _tbl = analysis['table']
        _hs = [str(h) for h in (_tbl.get('headers') or [])]
        _i_act = next((i for i, h in enumerate(_hs) if '行动' in h), -1)
        _i_gap = next((i for i, h in enumerate(_hs) if '差距' in h), -1)
        _i_res = next((i for i, h in enumerate(_hs) if ('资源' in h or '课程' in h)), -1)
        if _i_act >= 0 or _i_gap >= 0 or _i_res >= 0:
            _pool_adv = [str(it6['text']).strip() for it6 in _plan_items
                         if str(it6.get('tag') or '') != 'course' and str(it6['text']).strip()]
            _pool_crs = [str(it6['text']).strip() for it6 in _plan_items
                         if str(it6.get('tag') or '') == 'course' and str(it6['text']).strip()]

            def _derive_gap_skill(txt):
                t = re.sub(r'^(?:掌握|完成|参加|参与|学习|提升|考取|获得|加入|熟悉|了解|精通|夯实|强化|培养|练习|阅读|研读|选修|修读)+', '', str(txt).strip())
                t = t.strip('　。；;，,')
                if not t or '技能' in t or '能力' in t:
                    return t
                suffix = '能力' if re.search(r'竞赛|实践|协作|沟通|表达|汇报|组织|领导|素养', t) else '技能'
                return t + suffix

            for row2 in (_tbl.get('rows') or []):
                if not isinstance(row2, list):
                    continue
                if _i_act >= 0 and _i_act < len(row2) and str(row2[_i_act]).strip() in ('——', '—', ''):
                    if _pool_adv:
                        row2[_i_act] = _pool_adv.pop(0)
                        stats['action_backfilled'] += 1
                if _i_gap >= 0 and _i_gap < len(row2) and str(row2[_i_gap]).strip() in ('——', '—', '', '无', '暂无'):
                    _act_txt = str(row2[_i_act]).strip() if (_i_act >= 0 and _i_act < len(row2)) else ''
                    if _act_txt and _act_txt not in ('——', '—'):
                        row2[_i_gap] = _derive_gap_skill(_act_txt)
                        stats['action_backfilled'] += 1
                if _i_res >= 0 and _i_res < len(row2) and str(row2[_i_res]).strip() in ('——', '—', ''):
                    if _pool_crs:
                        row2[_i_res] = _pool_crs.pop(0)
                        stats['action_backfilled'] += 1

    def _trim_to_limit(text, limit):
        """剥除[来源：]标注后若超limit，从尾部删句直到达标；返回(新文本, 是否裁剪)。"""
        if not text or not isinstance(text, str):
            return text, False
        body = re.sub(r'\[来源：[^\]]*\]', '', text)
        if len(body) <= limit:
            return text, False
        sents = _split_sentences(text)
        while len(sents) > 1 and len(re.sub(r'\[来源：[^\]]*\]', '', ''.join(sents))) > limit:
            sents.pop()
        return ''.join(sents), True

    trimmed = 0
    if analysis.get('summary'):
        new_text, did = _trim_to_limit(analysis['summary'], 260)
        if did:
            analysis['summary'] = new_text
            trimmed += 1
    for it5 in (analysis.get('four_year_plan') or []):
        if isinstance(it5, dict) and it5.get('tag') != 'advice' and it5.get('text'):
            new_text, did = _trim_to_limit(it5['text'], 120)
            if did:
                it5['text'] = new_text
                trimmed += 1
    if trimmed:
        stats['length_trimmed'] = trimmed

    analysis.pop('paths', None)
    analysis.pop('advice_sections', None)

    if all_removed and llm_rewrite:
        removed_sents = [h[2] for h in all_removed if isinstance(h[2], str) and h[2].strip()]
        if removed_sents:
            try:
                rewritten = llm_rewrite(removed_sents, node_names[:80])
                cleaned = []
                for rs in (rewritten if isinstance(rewritten, list) else []):
                    if not isinstance(rs, str) or not rs.strip():
                        continue
                    if _check_entity_whitelist(rs, node_names):
                        continue
                    s2, _ = _process_sentence(rs, node_names, name_map, edge_by_node, stats)
                    if s2:
                        cleaned.append(s2)
                if cleaned:
                    stats['rewritten_sentences'] = len(cleaned)
                    first = all_removed[0]
                    if first[0] == 'target_profile' and isinstance(analysis.get('target_profile'), dict):
                        analysis['target_profile']['items'] = (analysis['target_profile'].get('items') or []) + [''.join(cleaned)]
                    elif first[0] == 'four_year_plan' and isinstance(first[1], dict):
                        first[1]['text'] = (first[1].get('text') or '') + ''.join(cleaned)
                    else:
                        analysis['summary'] = (analysis.get('summary') or '') + ''.join(cleaned)
            except Exception:
                pass

    return stats


def build_html_tree(node_id, adj, node_map, visited):
    """递归构建树形路径HTML"""
    if node_id in visited:
        return ''
    visited.add(node_id)
    node = node_map.get(node_id)
    name = node['name'] if node else node_id
    children = adj.get(node_id, [])
    html = '<div class="ai-tree-node">'
    html += '<div class="ai-tree-node-content">'
    html += f'<span class="ai-tree-node-label">{html_module.escape(name)}</span>'
    html += '</div>'
    if children:
        html += '<div class="ai-tree-children">'
        for child in children:
            child_node = node_map.get(child['to'])
            child_name = child_node['name'] if child_node else child['to']
            html += '<div class="ai-tree-node">'
            html += '<div class="ai-tree-node-content">'
            html += f'<span class="ai-tree-edge-label">--{html_module.escape(child["rel"])}--></span>'
            html += f'<span class="ai-tree-node-label">{html_module.escape(child_name)}</span>'
            html += '</div>'
            grand_children = adj.get(child['to'], [])
            if grand_children:
                html += '<div class="ai-tree-children">'
                for gc in grand_children:
                    html += build_html_tree(gc['to'], adj, node_map, visited)
                html += '</div>'
            html += '</div>'
        html += '</div>'
    html += '</div>'
    return html


def _repair_truncated_json(s):
    """修复被 max_tokens 截断的 LLM JSON 输出：
    1) 若存在已完整闭合的顶层前缀（如 title/summary + 部分paths），截到最后的平衡点；
    2) 否则从尾部向前寻找结构边界（逗号/开始括号），逐个剪掉残缺尾部并补齐括号栈，返回最长可解析前缀；
    3) 兜底按未闭合的括号栈补齐收尾。"""
    def _scan(text):
        stack = []
        in_str = False
        esc = False
        last_complete = -1
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in '{[':
                    stack.append(ch)
                elif ch in '}]':
                    if stack:
                        stack.pop()
                    if not stack:
                        last_complete = i
        return stack, in_str, last_complete

    def _close(cand):
        cand = cand.rstrip()
        if cand.endswith(','):
            cand = cand[:-1].rstrip()
        stack2, in_str2, last2 = _scan(cand)
        if not in_str2 and not stack2:
            return cand if last2 < 0 else cand[:last2 + 1]
        out = cand
        if in_str2:
            out += '"'
        out += ''.join(']' if c == '[' else '}' for c in reversed(stack2))
        return out

    stack, in_str, last_complete = _scan(s)
    if last_complete >= 0 and stack:
        return s[:last_complete + 1]
    if not stack and not in_str:
        return s

    boundaries = []
    st = []
    ins = False
    esc2 = False
    for i, ch in enumerate(s):
        if ins:
            if esc2:
                esc2 = False
            elif ch == '\\':
                esc2 = True
            elif ch == '"':
                ins = False
        else:
            if ch == '"':
                ins = True
            elif ch in '{[':
                st.append(ch)
            elif ch in '}]':
                if st:
                    st.pop()
            if not ins and ch in ',[{':
                boundaries.append(i)
    for pos in reversed(boundaries[-40:]):
        out = _close(s[:pos + 1])
        try:
            json.loads(out)
            return out
        except (json.JSONDecodeError, ValueError):
            continue
    return _close(s)


def _build_graph_digest(nodes, edges):
    """预计算图谱数据锚点：把子图中的实体清单与边属性硬数据整理成清单，注入prompt供LLM直接引用。"""
    buckets = {}
    for n in nodes:
        buckets.setdefault(n.get('type') or '其他', []).append(n)

    def _attr_str(v):
        if isinstance(v, list):
            return '、'.join([str(x) for x in v])
        return str(v)

    name_by_id = {n['id']: n.get('name', '') for n in nodes}
    dims = []
    for n in nodes:
        for d in (n.get('source_dimensions') or []):
            if d not in dims:
                dims.append(d)

    srcs = []
    for it in list(nodes) + list(edges):
        v = it.get('source_folders') or []
        if isinstance(v, str):
            v = [v]
        for f in v:
            f = _clean_source_tag(f)
            if f and f not in srcs:
                srcs.append(f)

    lines = [f'- 覆盖维度：{"、".join(dims) if dims else "综合"}',
             f'- 来源文档（{len(srcs)}份）：{"、".join(srcs[:10]) if srcs else "无"}']
    for label, t, limit in [('岗位实体', '岗位', 15), ('企业实体', '企业', 12), ('技能实体', '技能', 15),
                            ('认证证书', '认证证书', 8), ('竞赛', '竞赛', 8), ('职业方向', '职业方向', 8),
                            ('城市', '城市', 8), ('专业', '专业', 6), ('工具/平台', '工具/平台', 10)]:
        ns = [n['name'] for n in buckets.get(t, [])][:limit]
        if ns:
            lines.append(f'- {label}（共{len(buckets.get(t, []))}个）：{"、".join(ns)}')

    course_briefs = []
    for n in buckets.get('课程', [])[:12]:
        props = n.get('properties') or {}
        brief = '；'.join([f'{k}:{v}' for k, v in props.items() if v])
        course_briefs.append(n['name'] + (f'（{brief}）' if brief else ''))
    if course_briefs:
        lines.append(f'- 课程实体（共{len(buckets.get("课程", []))}门，含开课信息）：{"；".join(course_briefs)}')

    salary_samples, promo_samples, overlap_samples = [], [], []
    for e in edges:
        rel = e.get('relation') or ''
        attrs = e.get('attrs') or {}
        s_name = name_by_id.get(e.get('source'), str(e.get('source', '')))
        t_name = name_by_id.get(e.get('target'), str(e.get('target', '')))
        if rel == '招聘于' and attrs.get('薪资范围') and len(salary_samples) < 8:
            salary_samples.append(f'{s_name}→{t_name}：{_attr_str(attrs["薪资范围"])}')
        elif rel == '晋升/转岗' and len(promo_samples) < 8:
            parts = [f'{k}:{_attr_str(attrs[k])}' for k in ('起始职级', '目标职级', '转换难度', '平均年限') if attrs.get(k)]
            promo_samples.append(f'{s_name}→{t_name}' + (f'（{"；".join(parts)}）' if parts else ''))
        elif rel == '技能重叠' and len(overlap_samples) < 8:
            parts = [f'{k}:{_attr_str(attrs[k])}' for k in ('技能重叠度', '关系判定') if attrs.get(k)]
            overlap_samples.append(f'{s_name}~{t_name}' + (f'（{"；".join(parts)}）' if parts else ''))
    if salary_samples:
        lines.append('- 招聘薪资样例（岗位→企业：薪资范围）：' + '；'.join(salary_samples))
    if promo_samples:
        lines.append('- 晋升/转岗链样例（岗位→岗位：职级/难度/年限）：' + '；'.join(promo_samples))
    if overlap_samples:
        lines.append('- 技能重叠样例（岗位A~岗位B：重叠度/判定）：' + '；'.join(overlap_samples))
    return chr(10).join(lines)


def _course_timeline_html(path, node_map):
    """m2模式：路径中带开课学年的课程≥2门时，按学年×学期渲染纵向时间轴HTML，否则返回空串"""
    ids = []
    if path.get('node_ids'):
        ids = [str(x) for x in path['node_ids']]
    elif path.get('nodes'):
        for n in path['nodes']:
            ids.append(str(n.get('id') or '') if isinstance(n, dict) else str(n))
    courses = []
    for nid in ids:
        node = node_map.get(nid)
        if not node:
            continue
        props = node.get('properties') or {}
        y = props.get('开课学年')
        if not y:
            continue
        courses.append((str(y), str(props.get('开课学期') or ''),
                        str(node.get('name') or nid), str(props.get('课程性质') or '')))
    if len(courses) < 2:
        return ''
    year_order = {'大一': 1, '大二': 2, '大三': 3, '大四': 4}
    sem_order = {'秋': 1, '春': 2, '短学期': 3}
    courses.sort(key=lambda c: (year_order.get(c[0], 9), sem_order.get(c[1], 0)))
    groups = []
    for c in courses:
        if not groups or (groups[-1]['year'], groups[-1]['sem']) != (c[0], c[1]):
            groups.append({'year': c[0], 'sem': c[1], 'courses': []})
        groups[-1]['courses'].append(c)
    parts = ['<div class="ai-timeline">']
    for g in groups:
        term = g['year'] + ((' · ' + g['sem']) if g['sem'] else '')
        parts.append('<div class="ai-tl-item">')
        parts.append(f'<div class="ai-tl-term">{html_module.escape(term)}</div>')
        parts.append('<div class="ai-tl-courses">')
        for _, _, name, nature in g['courses']:
            cls = 'ai-tl-elective' if '选' in nature else 'ai-tl-compulsory'
            parts.append(f'<span class="ai-tl-course {cls}" title="{html_module.escape(nature or "课程")}">{html_module.escape(name)}</span>')
        parts.append('</div></div>')
    parts.append('</div>')
    return ''.join(parts)


# ===== 用户画像 API（r6b：点亮已会技能 + 目标设定，供AI差距分析对照；r13起按登录用户分库）=====
@app.route('/api/profile', methods=['GET'])
def get_profile():
    """读取用户画像（年级/专业/意向/已会技能），登录用户读个人库"""
    return jsonify({'success': True, 'profile': db_module.get_user_profile(_current_profile_db())})


@app.route('/api/profile', methods=['PUT'])
def save_profile():
    """保存用户画像（白名单字段覆盖），登录用户写个人库"""
    data = request.json or {}
    allowed = ('grade', 'major', 'target_jobs', 'target_cities', 'known_skills', 'm4_layout')
    patch = {k: data[k] for k in allowed if k in data}
    saved = db_module.save_user_profile(patch, _current_profile_db())
    return jsonify({'success': True, 'profile': saved})


@app.route('/api/profile/skill', methods=['POST'])
def toggle_profile_skill():
    """点亮/取消点亮一个已会技能节点，登录用户写个人库"""
    data = request.json or {}
    node_id = (data.get('id') or '').strip()
    if not node_id:
        return jsonify({'error': '缺少节点ID'}), 400
    known, profile = db_module.toggle_known_skill(
        node_id, data.get('name', ''), data.get('type', ''), db_path=_current_profile_db())
    return jsonify({'success': True, 'known': known, 'profile': profile})


# ===== 账号 API（r13：用户名+密码登录，每账号独立个人数据）=====
@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """注册新账号：成功后自动登录（写入会话）"""
    data = request.json or {}
    user, err = db_module.create_user(data.get('username'), data.get('password'))
    if err:
        return jsonify({'error': err}), 400
    session['user_id'] = user['id']
    session['username'] = user['username']
    return jsonify({'success': True, 'user': user})


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """登录：校验用户名密码，写入会话"""
    data = request.json or {}
    user = db_module.verify_user(data.get('username'), data.get('password'))
    if not user:
        return jsonify({'error': '用户名或密码错误'}), 401
    session['user_id'] = user['id']
    session['username'] = user['username']
    return jsonify({'success': True, 'user': user})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """退出登录：清空会话"""
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """查询当前登录状态（前端刷新/恢复会话用）"""
    user = _current_user()
    return jsonify({'success': True, 'user': user})


@app.route('/api/ai-analyze', methods=['POST'])
def ai_analyze():
    """AI智能分析：将图谱数据发给LLM，返回路径分析结果"""
    data = request.json
    keyword = data.get('keyword', '')
    dim = data.get('dim', 'all')
    depth = data.get('depth', 1)
    nodes = data.get('nodes', [])
    edges = data.get('edges', [])
    mode_req = data.get('mode', 'auto')
    user_grade = (data.get('user_grade') or '').strip()
    center_id = data.get('center_id', '')
    target_jobs = [str(j).strip() for j in (data.get('target_jobs') or []) if str(j).strip()]

    # 用户画像：已点亮掌握的技能（点亮清单），用于能力差距对照（r6b；r13起读登录用户个人库）
    profile = db_module.get_user_profile(_current_profile_db())
    known_skills = profile.get('known_skills') or []
    known_names = '、'.join([str(s.get('name', '')) for s in known_skills if s.get('name')])

    if not nodes:
        return jsonify({'error': '无图谱数据可分析'}), 400

    # ===== 数据瘦身：限制发送给LLM的规模，避免prompt过大导致超时 =====
    # 匹配节点优先，其次按关联层级由近到远排序，超量截断
    MAX_LLM_NODES = 80
    MAX_LLM_EDGES = 120
    truncated = False
    if len(nodes) > MAX_LLM_NODES:
        truncated = True
        nodes = sorted(nodes, key=lambda n: (0 if n.get('matched') else 1, n.get('level', 99)))[:MAX_LLM_NODES]
        kept_ids = {n['id'] for n in nodes}
        edges = [e for e in edges if e.get('source') in kept_ids and e.get('target') in kept_ids]
    if len(edges) > MAX_LLM_EDGES:
        truncated = True
        edges = edges[:MAX_LLM_EDGES]

    # 构建节点和边的文本描述
    nodes_text = []
    for n in nodes:
        props_str = ''
        if n.get('properties'):
            props_str = '；'.join([f'{k}: {v}' for k, v in n['properties'].items() if v])
        level = n.get('level', 0)
        level_label = ['搜索匹配', '一跳关联', '二跳关联', '三跳关联'][min(level, 3)]
        matched = '【匹配】' if n.get('matched') else ''
        agg_note = '【聚合占位节点：代表某地区不具名的多家企业，非具体企业】' if '汇总' in (n.get('name') or '') else ''
        nodes_text.append(f"- {matched}节点ID:{n['id']} | 名称:{n['name']} | 类型:{n.get('type','')} | 层级:{level_label} | {props_str}{agg_note}")

    edges_text = []
    for e in edges:
        dim_label = e.get('source_dimension', '')
        support = e.get('support', '')
        if isinstance(support, list):
            support = '；'.join([s for s in support if s])
        folders = e.get('source_folders', [])
        if isinstance(folders, str):
            folders = [folders] if folders else []
        pages = e.get('page', [])
        if isinstance(pages, str):
            pages = [pages] if pages else []
        src_str = '、'.join([_clean_source_tag(f) for f in folders]) or '未标注'
        page_str = '、'.join([str(p) for p in pages]) or '无'
        attrs_str = ''
        attrs = e.get('attrs') or {}
        if attrs:
            attrs_parts = []
            for k, v in attrs.items():
                if v in (None, '', [], {}):
                    continue
                if isinstance(v, list):
                    v = '、'.join([str(x) for x in v])
                attrs_parts.append(f'{k}:{v}')
            if attrs_parts:
                attrs_str = ' | 属性:' + '；'.join(attrs_parts)
        edges_text.append(f"- {e['source']} -> {e['target']} | 关系:{e.get('relation','')} | 维度:{dim_label} | 来源:{src_str} | 页:{page_str} | 支撑:{support[:80] if support else '无'}{attrs_str}")

    graph_digest = _build_graph_digest(nodes, edges)

    mode = detect_report_mode(mode_req, keyword, nodes, center_id)
    mode_label = MODE_LABELS[mode]
    dim_prompt = DIM_ANALYSIS_PROMPT.get(dim, DIM_ANALYSIS_PROMPT['all'])
    dim_name = {'job_cluster': '岗位聚集', 'skill_structure': '技能结构', 'career_path': '发展路径',
                'course_system': '课程体系', 'job_coopetition': '岗位竞合', 'competition_practice': '竞赛实践',
                'all': '综合维度'}.get(dim, '综合维度')

    # M4/M5 报告模式的顶层附加字段模板（四段式结构不变，在此之上追加）
    extra_schema = ''
    if mode == 'm4':
        jobs_hint = '、'.join(target_jobs) if target_jobs else ''
        extra_schema = f"""

【M4附加字段：mindmap（必须作为顶层字段与四段式并列返回）】
"mindmap": {{
    "title": "个性化培养思维导图",
    "categories": [
        {{
            "name": "大类通识课",
            "years": [
                {{"year": "大一", "items": ["《课程名》（大一·秋）"]}},
                {{"year": "大二", "items": ["《课程名》（大二·春）"]}},
                {{"year": "大三", "items": []}},
                {{"year": "大四", "items": []}}
            ]
        }},
        {{"name": "专业必修课", "years": [同上结构，覆盖大一至大四]}},
        {{"name": "专业选修课", "recommend_note": "结合目标岗位的一句话选课策略（无目标岗位时省略此字段）", "years": [同上结构]}},
        {{"name": "选修课", "years": [同上结构]}},
        {{"name": "竞赛实践", "years": [{{"year": "大一", "items": ["竞赛名（时间节点）"]}}]}}
    ]
}}

M4字段要求：
K. mindmap五大分支顺序固定：大类通识课、专业必修课、专业选修课、选修课、竞赛实践；每个分支的years覆盖大一至大四四个年份（year逐字用"大一""大二""大三""大四"）；课程类分支该年无对应课程时items为空数组，禁止编造课程凑数；竞赛实践分支禁止整体留空——数据中有竞赛/实践类节点时逐字采用，某年数据不足时按该专业培养惯例补入当年适合的竞赛/实践建议并尾注"（AI建议）"（如"全国大学生电子设计竞赛（一般暑期备赛，AI建议）"），四年合计至少4条
L. 课程名必须逐字来自节点列表/锚点清单（与tag=course同源同约束）；学期括注铁律：学年与学期只以该课程节点属性"开课学年""开课学期"为准，格式"（大二·秋）"；开课学期为逗号分隔多值（如"秋,春"）表示同一学年两学期都开课，需逐值括注（如"（大二·秋）（大二·春）"）；数据未标注学期的可不括注；禁止凭印象猜测学期，禁止把所有课程默认堆到同一学期；竞赛条目写"竞赛名（一般X月报名，以官网通知为准）"式时间节点；mindmap条目一律不带[来源：]标注（精简展示），来源追溯由four_year_plan承担
M. {('用户叠加了目标岗位：' + jobs_hint + '。专业选修课分支必须：①从数据中的专业选修课里挑出与该岗位最相关的课程排前；②recommend_note给出一句话选课策略；③被推荐课程的条目尾注"★建议优先"，其余课程保留但不加星标；选修课分支同理处理') if target_jobs else '用户未叠加目标岗位：专业选修课与选修课按数据原样列出，recommend_note省略'}
"""
    elif mode == 'm5':
        jobs_hint = '、'.join(target_jobs) if target_jobs else ''
        extra_schema = f"""

【M5附加字段：career_paths（必须作为顶层字段与四段式并列返回）】
"career_paths": {{
    "title": "职业方向路径图",
    "root_major": "用户专业名（即搜索关键词，逐字照抄）",
    "paths": [
        {{
            "name": "路径名（如：就业-硬件研发工程师 / 考研 / 直博 / 选调 / 支教保研）",
            "type": "路径类型（就业|升学读研|直博|考研|选调|支教保研|干部保研 之一）",
            "overview": "该路径一句话概述与适配度判断，40-80字",
            "path_nodes": [
                {{"type": "技能|课程|竞赛 之一", "name": "节点名（逐字来自图谱数据）", "refs": ["来源名"]}}
            ],
            "steps": [
                {{"period": "大二·春", "action": "该阶段关键动作；事实步骤句尾标注 [来源：数据来源名 页号]，规划性步骤无需来源"}}
            ],
            "advice": ["该路径专属建议1（具体可执行，20-60字）"]
        }}
    ]
}}

M5字段要求：
K. paths输出5-8条：就业路径1-3条（岗位名取自数据中的相邻岗位/锚点清单，逐字照抄）；保研/考研/升学读研1条（按学生情况择一或并列）；直博1条（常识允许时）；选调/支教保研/干部保研等政策路径1-2条；与用户专业明显无关的路径省略
K2. path_nodes（就业路径必需；升学读研/保研/考研/直博路径同样必需——从"专业出发要经过哪些课程/技能/竞赛/科研训练才能到达该升学方向"的链路中选取，体现该方向的先修课程与能力准备；政策路径（选调/支教保研/干部保研）省略该字段）：3-6个按成长先后排序的关键节点，type只能是技能/课程/竞赛之一，name逐字来自图谱数据节点名，refs标注来源；这些节点将渲染成"专业→技能/课程/竞赛→目标"发散导图的分支节点
L. steps每条路径4-8步、按时间升序，period用"大一·秋"式学期或"大二"式学年；就业路径的步骤须落到数据中具体的课程/技能/竞赛节点并标注来源；政策路径（选调/支教保研/干部保研）无图谱数据支撑时，steps只写"关注XX发布/准备XX考试"这类通用规划动作（不标来源、不编造具体政策文件名与院校名），细节放advice
M. advice每条路径1-3条，具体可执行；禁止"要努力学习""提升综合素质"这类空话套话；时间表述用"一般X月，以官方通知为准"式
N. {('用户叠加了目标岗位：' + jobs_hint + '。paths第一条必须是为该岗位量身定制的就业路径——name写"就业-' + jobs_hint + '"（岗位名逐字照抄），type为就业，overview给出该岗位与用户数据基座的适配度判断，steps全部落到数据中与该岗位直接相关的课程/技能/证书/竞赛节点并标注来源，advice给出针对该岗位的补强建议；其余路径按K条默认规则排后') if target_jobs else '用户未叠加目标岗位：paths按K条默认规则输出，不指定置顶路径'}
"""

    # 反馈4：选定年级时培养路径只覆盖当前学年→大四，低年级学期不再生成
    grade_scope_semesters = ''
    if user_grade in ('大一', '大二', '大三', '大四'):
        _g_years = ('大一', '大二', '大三', '大四')[('大一', '大二', '大三', '大四').index(user_grade):]
        grade_scope_semesters = '、'.join(f'{y}·{sm}' for y in _g_years for sm in ('秋', '春'))

    system_prompt = f"""{dim_prompt}
{REPORT_MODE_PROMPTS[mode]}
{GROUNDING_RULES}

用户搜索了关键词"{keyword}"，以下是【{dim_name}】维度的知识图谱数据（含节点属性和关联关系）。
请基于上述分析框架和图谱数据，进行专业的路径分析。路径应紧扣该维度的分析侧重点，帮助用户获得该维度下的就业与职业规划洞察。

你必须严格返回以下JSON格式——四段式培养路径报告结构（不要包含任何其他文字，不要用markdown代码块）：
{{
    "title": "分析主题标题",
    "summary": "总体分析总结，160-260字，依次概括三层：①本专业的核心课程——从数据的专业必修/专业核心课中点名3-5门最关键的；②必备基础课程——点名2-3门数理与通识基础课（如数学/物理/编程/外语类）；③对应的专业核心技能——上述课程共同支撑的2-4项核心能力；最后一句点明当前最该做什么。点名的课程名必须逐字来自数据并在句尾附[来源：数据来源名 页号]",
    "target_profile": {{
        "title": "目标画像",
        "items": [
            "画像条目1：岗位/专业定义、分级要求、核心技能清单、需求分布等，复述数据事实时句尾标注 [来源：数据来源名 页号]",
            "画像条目2：...",
            "画像条目3：...",
            "画像条目4：..."
        ]
    }},
    "four_year_plan": [
        {{"stage": "大一·秋", "text": "必修课《课程名》", "tag": "course"}},
        {{"stage": "大一·秋", "text": "技能名及等级要求", "tag": "skill"}},
        {{"stage": "大一·春", "text": "竞赛名及时间节点", "tag": "comp"}},
        {{"stage": "大一·春", "text": "具体可执行建议", "tag": "advice"}}
    ],
    "action_list": [
        {{
            "stage": "大一",
            "goals": ["本阶段核心目标1-2条"],
            "courses": ["必学课程与要达到的掌握程度"],
            "skills": ["建议掌握的技能"],
            "comps": ["推荐竞赛及报名/备赛时间节点"],
            "pitfalls": ["避坑提示"]
        }}
    ],
    "table": {{
        "title": "表格标题",
        "headers": ["列名1", "列名2", "列名3"],
        "rows": [["值1", "值2", "值3"], ["值1", "值2", "值3"]]
    }},
    "gap_next": {{
        "status": "当前阶段已完成/已掌握情况的核实性描述，80-120字",
        "top3": ["下一步优先事项1", "优先事项2", "优先事项3"],
        "alternatives": ["备选岗位/方向1", "备选方向2"]
    }}
}}

字段说明与要求：
1. tag枚举只有四个取值：course=官方课程（课程名必须逐字取自下方节点列表/锚点清单中的课程节点，开课学期与数据属性一致）、skill=技能要求（技能名优先取自数据）、comp=竞赛参考（竞赛/证书/科研，可写广为人知的具体名称）、advice=AI学习建议（不受图谱限制）
2. target_profile 输出3-6条，每条30-90字，凡复述数据事实的条目句尾必须带 [来源：数据来源名 页号]，来源名与页号逐字取自关系描述中的"来源"和"页"字段；条目中作为岗位/职业参考依据的课程只能是专业必修课（专业核心课）与专业选修课，数理基础课与通识课不作岗位依据（详见事实层约束B2）
3. four_year_plan {'必须覆盖' + grade_scope_semesters + '这些学期' if grade_scope_semesters else '必须覆盖大一·秋、大一·春、大二·秋、大二·春、大三·秋、大三·春、大四·秋、大四·春全部8个学期'}（stage逐字用"大一·秋"这类格式），每学期至少3条；数据中带开课学年+学期属性的课程必须归位到对应学期且tag=course（开课学年不在覆盖范围内的课程直接略过，不得写入）——开课学期为逗号分隔多值（如"开课学年: 大三；开课学期: 秋,春"）表示该课在大三秋和大三春都要挂载，逐学期各写一条；严禁把带学期属性的课程全部堆到同一学期或忽略属性自行安排；tag=course条目只写课程名（格式"必修课《课程名》"或"《课程名》"），禁止附学分/性质备注与说明文字；某学期数据无课程时用技能/竞赛/建议条目填满，禁止编造课程名凑数
4. action_list 覆盖{'用户当前年级"' + user_grade + '"至"大四"的各学年' if grade_scope_semesters else '四个学年'}（stage用"大一""大二""大三""大四"），每个stage的goals必须1-2条，courses/skills/comps/pitfalls每项都尽量给1-2条，严禁某个stage五个列表全空；comps条目要给时间节点（如"一般3月报名，以官网通知为准"）；pitfalls每阶段1条
5. table必须输出4-8行数据行、4-6列；单元格内容尽量直接取自【图谱数据锚点清单】中的实体名称与边属性数值（如薪资范围、技能等级要求、平均年限、重叠度）；禁止输出空行或整行"——"
6. gap_next 的top3必须是恰好3条可执行动作（每条30-60字），alternatives给2-3个备选岗位/方向（优先取数据中的相邻岗位）；若用户点亮了已会技能（见用户输入"已点亮掌握的技能"），能力差距分析必须对照该清单：已点亮的技能视为已掌握，不得再列为待补差距，差距条目围绕未点亮但关键的要求展开；纯JSON输出，不要有额外文字
{extra_schema}
【事实层硬约束（target_profile全部条目、four_year_plan中tag=course/skill/comp的条目、table，违反任何一条即为不合格输出）】
A. 严禁编造：事实层所有岗位名、技能名、企业名、城市名、课程名、证书名、薪资数字、比例数字，必须来自输入数据；输入数据中不存在的信息一律不得出现在事实层。
B. 课程防编造（最高优先级）：tag=course条目中的课程名必须逐字出现在下方【节点列表】或【图谱数据锚点清单】中，且开课学年/学期必须与数据属性一致；数据中没有的课程一律不得以course标签出现——宁可少放课程，也不虚构课程；想给用户额外学习建议时改用tag=advice。
B2. 职业参考课程限定：推导目标岗位、岗位画像以及"课程支撑岗位"类表述时，只允许以专业必修课（专业核心课）和专业选修课作为职业能力依据；高等数学、线性代数、概率论与数理统计、大学物理、复变函数、积分变换等数理基础课以及外语、体育、思政、军事理论等通识课只能作为学业基础提及，严禁写成"支撑某岗位""对应某职业""岗位要求该课"式表述。
C. 接地引用：target_profile凡复述数据的条目必须带 [来源：数据来源名 页号]；four_year_plan中tag=course/skill/comp条目若直接复述数据（课程学分、技能等级、竞赛名称来自数据），也标注来源；来源名与页号逐字取自关系描述中的"来源"和"页"字段。
D. 表格接地：table中涉及课程/证书/企业/岗位的单元格，只能引用【图谱数据锚点清单】中列出的具体名称；锚点未覆盖的单元格填"——"，禁止虚构具体课程、证书、企业名。
E. 聚合占位节点：名称含"多家企业汇总"的节点代表某地区不具名的多家企业的聚合，不是具体企业；分析中应表述为"京津冀地区多家企业"等，禁止将其当作一家具体企业展开分析。
F. 字数与深度：字数统计一律不含[来源：]标注；summary正文160-260字；target_profile每条30-90字；four_year_plan条目按tag分区精简——tag=course条目只写课程名（如"必修课《微积分I》"），严禁输出"学分未标""（X学分，必修）""（性质：必修）"这类元信息备注，严禁在课程名后附长说明（数据中确有学分数字时可写"《课程名》（4学分）"）；tag=skill带等级要求；tag=comp带时间节点；tag=advice每条20-60字、具体可执行；事实层宁可具体不可抽象，写不足时补充节点属性细节而不是抽象概括。

【建议层硬约束（tag=advice条目、action_list、gap_next，违反任何一条即为不合格输出）】
G. 具体资源名规则（已放开）：建议层可以写具体的竞赛名、证书名、工具名、书籍名、课程类型名，只要该领域真实广为人知（如"全国大学生电子设计竞赛""蓝桥杯""LeetCode""全国计算机等级考试二级"）即可写具体名称，不限白名单；但仍不得编造具体企业名（统一用"行业头部企业"）、不得编造具体薪资数字和百分比（统一用"行业平均薪资水平"）、不写具体报名网址和电话（时间节点写"一般X月报名，以官网通知为准"）。
H. 允许通识经验：备赛时机（如"大二暑期开始备赛"）、考研/就业时间线（如"大三下学期确定考研或就业方向"）、学习方法论、软实力培养等通用经验可以直接给出，不受图谱数据限制。
I. 建议层无需来源标注、无需加「AI建议」前缀（前端统一渲染徽标）；每条建议须具体可执行，禁止"要努力学习""提升综合素质""扎实掌握""积极参与"这类空话套话（此禁令针对建议层全部条目）。
J. 输出前自查（逐条核对，任何一条不满足就修改后再输出）：①four_year_plan{'覆盖' + grade_scope_semesters + '且每学期至少3条' if grade_scope_semesters else '覆盖8个学期且每学期至少3条'}；②tag=course的课程名全部来自数据且学期归位正确；③action_list{'覆盖"' + user_grade + '"至"大四"各学年' if grade_scope_semesters else '覆盖四个学年'}且comps带时间节点、任何stage五个列表不全空；④gap_next的top3恰好3条且具体可执行；⑤建议条目中不出现"扎实掌握""积极参与""提升综合素质""要努力学习"。"""

    user_content = f"""搜索关键词：{keyword}
用户当前年级：{user_grade or '未指定'}
已点亮掌握的技能：{known_names or '（无——用户尚未点亮任何已会技能）'}
分析维度：{dim_name}

=== 节点列表（共{len(nodes)}个）===
{chr(10).join(nodes_text)}

=== 关联关系（共{len(edges)}条）===
{chr(10).join(edges_text)}

=== 图谱数据锚点清单（本子图实证数据一览）===
{graph_digest}

写作硬性要求：①summary与target_profile合计至少点名上方锚点清单中的6个具体条目——实体名必须逐字照抄锚点清单原文（岗位名、企业名、技能名、课程名），属性数值如薪资范围、技能等级要求、平均年限；②数据中带开课学年+学期的课程必须全部归位到four_year_plan对应学期（节点列表中标注了开课属性的课程优先处理）；③table单元格优先直接取用锚点清单内容；④事实层（target_profile/four_year_plan事实条目/table）严禁使用锚点清单与子图实体之外的任何具体企业名、课程名、证书名，泛指时用"行业头部企业""行业认可度高的证书"等表述；建议层（tag=advice/action_list/gap_next）可写广为人知的竞赛名/工具名/书籍名（如"蓝桥杯""LeetCode"），但不得编造企业名与薪资数字。
{'年级定制要求：用户当前年级为"' + user_grade + '"——①summary末尾必须点明该年级本阶段最优先的1-2个动作（本学期时间参照：秋季学期9-12月、春季学期3-6月）；②gap_next.top3以该年级当前阶段的紧迫度排序，优先写本学年内可完成的动作；③four_year_plan与action_list只覆盖从"' + user_grade + '"到"大四"的学期/学年，用户已修完的低年级学期与学年一律不再出现；④该年级对应学年的条目须与上述时间敏感对齐。' if user_grade else ''}
{'画像差距要求：用户点亮了已会技能（' + known_names + '）——target_profile的能力差距描述与gap_next.top3必须对照该清单：已点亮的技能作为用户已有基础表述，不得再列为待补动作；待补差距优先围绕数据中未点亮的关键技能/课程/竞赛展开，并可标注"已具备基础，进阶XX"式表述。' if known_names else ''}

请按四段式结构{'及顶层附加字段mindmap' if mode == 'm4' else '及顶层附加字段career_paths' if mode == 'm5' else ''}生成培养路径报告并返回JSON。

输出前最后自查：four_year_plan{'覆盖' + grade_scope_semesters + '、每学期至少3条' if grade_scope_semesters else '覆盖大一·秋至大四·春8个学期、每学期至少3条'}；tag=course的课程名全部来自节点列表且学期归位正确；action_list{'覆盖"' + user_grade + '"至"大四"各学年且每个stage至少1条行动项' if grade_scope_semesters else '覆盖四个学年且每个stage至少1条行动项'}；gap_next的top3恰好3条；{'mindmap五大分支齐全、课程名全部来自数据' if mode == 'm4' else 'career_paths路径数5-8条、就业路径岗位名来自数据' if mode == 'm5' else '建议句中不得出现"扎实掌握""积极参与""提升综合素质""要努力学习"'}{'；年级自查：summary末尾点明当前年级优先动作、gap_next.top3按该年级紧迫度排序' if user_grade else ''}。"""

    # ===== 结果缓存：同一参数重复生成直接命中（传 refresh=1 强制重新生成） =====
    ai_cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cache', 'ai_analyze')
    os.makedirs(ai_cache_dir, exist_ok=True)
    cache_key = hashlib.md5((mode + '||' + system_prompt + '||' + user_content).encode('utf-8')).hexdigest()
    cache_file = os.path.join(ai_cache_dir, cache_key + '.json')
    force_refresh = str(data.get('refresh') or '').lower() in ('1', 'true')
    analysis = None
    validation = None
    cache_hit = False
    if not force_refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            cand = cached.get('analysis')
            if isinstance(cand, dict) and (cand.get('summary') or cand.get('four_year_plan')):
                analysis = cand
                validation = cached.get('validation')
                cache_hit = True
                print(f"[ai-analyze] 命中结果缓存（{cache_key[:8]}），跳过LLM直接出报告；传 refresh=1 可强制重新生成", flush=True)
        except Exception as _ce:
            print(f"[ai-analyze] 读取缓存失败（忽略）: {_ce}", flush=True)

    try:
        payload = {
            'model': 'tju-llm',
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content}
            ],
            'temperature': 0.3,
            'max_tokens': 16384,
            'chat_template_kwargs': {'enable_thinking': False}
        }
        headers = {
            'Authorization': f'Bearer {LLM_API_KEY}',
            'Content-Type': 'application/json'
        }
        # LLM偶发返回破损JSON/空内容或网络抖动，自动重试最多3次（指数退避；第3次降级为非流式兜底）
        if not cache_hit:
            analysis = None
            last_err = None
            for attempt in range(1, 4):
                try:
                    llm_started = time.time()
                    if attempt < 3:
                        # 流式：首字到达快，服务卡顿时读超时可及时中断
                        stream_payload = dict(payload)
                        stream_payload['stream'] = True
                        resp = requests.post(LLM_API_URL, headers=headers, json=stream_payload, stream=True, timeout=(15, 240))
                        resp.raise_for_status()
                        content_parts = []
                        for line in resp.iter_lines(decode_unicode=True):
                            if time.time() - llm_started > 600:
                                raise requests.exceptions.RequestException('LLM生成超过10分钟仍未完成（服务负载过高），请稍后重试')
                            if not line or not line.startswith('data:'):
                                continue
                            chunk_str = line[5:].strip()
                            if chunk_str == '[DONE]':
                                break
                            try:
                                chunk = json.loads(chunk_str)
                            except json.JSONDecodeError:
                                continue
                            delta = (chunk.get('choices') or [{}])[0].get('delta') or {}
                            piece = delta.get('content')
                            if piece:
                                content_parts.append(piece)
                        content = ''.join(content_parts)
                    else:
                        # 第3次重试降级为非流式：流式端点偶发异常时兜底
                        resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=(15, 600))
                        resp.raise_for_status()
                        content = (resp.json().get('choices') or [{}])[0].get('message', {}).get('content') or ''
                    if not content.strip():
                        raise requests.exceptions.RequestException('LLM未返回内容（服务繁忙或排队超时），请稍后重试')
                    print(f"[ai-analyze] LLM接收完成: {len(content)}字符, 耗时{time.time() - llm_started:.0f}s", flush=True)

                    # 清理可能的markdown代码块标记
                    content = content.strip()
                    if content.startswith('```'):
                        content = content.split('\n', 1)[1] if '\n' in content else content
                    if content.endswith('```'):
                        content = content.rsplit('```', 1)[0]
                    content = content.strip()

                    try:
                        analysis = json.loads(content)
                    except json.JSONDecodeError:
                        # LLM 输出被 max_tokens 截断时，尝试自动修复：保留已完整闭合的部分
                        fixed = _repair_truncated_json(content)
                        try:
                            analysis = json.loads(fixed)
                            print(f"[ai-analyze] 检测到JSON截断，已自动修复（修复后长度 {len(fixed)}字符，顶层键: {list(analysis.keys()) if isinstance(analysis, dict) else type(analysis).__name__}）", flush=True)
                        except json.JSONDecodeError as e2:
                            try:
                                with open('_debug_llm_content.json', 'w', encoding='utf-8') as _f:
                                    _f.write(content)
                                print(f"[ai-analyze] 修复失败样本已落盘 _debug_llm_content.json（长度{len(content)}，修复后仍报错：{e2}）", flush=True)
                            except Exception:
                                pass
                            raise json.JSONDecodeError('输出无法解析（可能被截断）', content, 0)
                    break
                except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
                    last_err = e
                    if attempt < 3:
                        wait = 3 if attempt == 1 else 8
                        print(f"[ai-analyze] 第{attempt}次LLM调用失败（{type(e).__name__}: {e}），{wait}秒后自动重试", flush=True)
                        time.sleep(wait)
                    else:
                        print(f"[ai-analyze] 第{attempt}次LLM调用失败（{type(e).__name__}: {e}），已达最大重试次数", flush=True)
            if analysis is None:
                raise last_err

        # 形态防御：LLM偶发把dict字段漂移为list，统一归一化避免下游渲染崩溃
        tp = analysis.get('target_profile')
        if isinstance(tp, list):
            tp_items = []
            for it in tp:
                if isinstance(it, dict):
                    txt = str(it.get('text') or it.get('content') or '').strip()
                    if txt:
                        tp_items.append(txt)
                elif str(it or '').strip():
                    tp_items.append(str(it).strip())
            analysis['target_profile'] = {'title': '目标岗位画像', 'items': tp_items}
        elif tp is not None and not isinstance(tp, dict):
            analysis['target_profile'] = {}
        if not isinstance(analysis.get('gap_next'), dict):
            analysis['gap_next'] = {}
        if not isinstance(analysis.get('table'), dict):
            analysis['table'] = {}

        node_map = {}
        for n in nodes:
            node_map[n['id']] = n

        def _llm_rewrite(sents, whitelist):
            sys_p = ("你是文本校对器。以下句子来自就业分析报告，因包含知识图谱中不存在的实体名而不合格。"
                     "请逐句重写：只保留白名单实体，无法保留的信息删除，保持原意与语气，禁止引入新实体。"
                     "直接返回JSON数组，如 [\"重写句1\", \"重写句2\"]，不要任何其他文字。")
            usr_p = "白名单实体（节选）：" + "、".join(whitelist) + "\n待重写句子：\n" + "\n".join([f"{i+1}. {s}" for i, s in enumerate(sents)])
            payload = {'model': 'tju-llm', 'messages': [
                {'role': 'system', 'content': sys_p}, {'role': 'user', 'content': usr_p}],
                'temperature': 0.2, 'max_tokens': 4096,
                'chat_template_kwargs': {'enable_thinking': False}}
            headers = {'Authorization': f'Bearer {LLM_API_KEY}', 'Content-Type': 'application/json'}
            for attempt in range(1, 3):
                try:
                    resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=120)
                    resp.raise_for_status()
                    rc = resp.json()['choices'][0]['message']['content'].strip()
                    if rc.startswith('```'):
                        rc = rc.split('\n', 1)[1] if '\n' in rc else rc
                    if rc.endswith('```'):
                        rc = rc.rsplit('```', 1)[0]
                    return json.loads(rc.strip())
                except Exception as e:
                    if attempt < 2:
                        print(f"[ai-analyze] 句子改写第{attempt}次失败（{e}），2秒后重试", flush=True)
                        time.sleep(2)
                    else:
                        print(f"[ai-analyze] 句子改写失败（{e}），跳过改写保留原句", flush=True)
                        return list(sents)

        if not cache_hit:
            validation = validate_report(analysis, node_map, edges, llm_rewrite=_llm_rewrite, user_grade=user_grade)
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump({'analysis': analysis, 'validation': validation,
                               'cached_at': datetime.now().isoformat()}, f, ensure_ascii=False)
                print(f"[ai-analyze] 结果已写入缓存（{cache_key[:8]}）", flush=True)
            except Exception as _we:
                print(f"[ai-analyze] 写入结果缓存失败（忽略）: {_we}", flush=True)

        # 保存报告为HTML文件
        report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'reports')
        os.makedirs(report_dir, exist_ok=True)

        all_dims = []
        for n in nodes:
            for d in (n.get('source_dimensions') or []):
                if d not in all_dims:
                    all_dims.append(d)
        all_srcs = set()
        for it in list(nodes) + list(edges):
            v = it.get('source_folders') or []
            if isinstance(v, str):
                v = [v]
            all_srcs.update([x for x in v if x])

        depth_str = str(depth)
        # 清理文件名中的非法字符
        safe_keyword = keyword.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
        safe_dim = dim_name.replace('/', '_')
        safe_mode = mode_label.replace('/', '_')
        filename = f'{safe_keyword}_{safe_mode}_{safe_dim}_{depth_str}层.html'
        filepath = os.path.join(report_dir, filename)

        # 生成HTML内容
        html_parts = []
        html_parts.append(f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{analysis.get('title', '分析报告')}</title>
<style>
body {{ font-family: system-ui, -apple-system, 'Microsoft YaHei', sans-serif; background: #f5f7fa; margin: 0; padding: 32px; color: #1a202c; }}
.container {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
.meta {{ font-size: 13px; color: #94a3b8; margin-bottom: 20px; }}
.summary {{ font-size: 14px; line-height: 1.8; color: #475569; background: #fff; padding: 20px; border-radius: 6px; border: 1px solid #e2e8f0; margin-bottom: 28px; }}
h2 {{ font-size: 16px; font-weight: 700; margin: 28px 0 16px; padding-left: 10px; border-left: 3px solid #2c6ecb; }}
.tag {{ display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 10px; vertical-align: 2px; margin-right: 6px; }}
.tag-course {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }}
.tag-skill {{ background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }}
.tag-comp {{ background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }}
.tag-advice {{ background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }}
.ref-sup {{ color: #2c6ecb; font-size: 10px; margin: 0 1px; }}
.profile-card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; margin-bottom: 28px; }}
.profile-card h3 {{ margin: 0 0 12px; font-size: 15px; color: #0f172a; }}
.profile-list {{ margin: 0; padding: 0; list-style: none; }}
.profile-list li {{ font-size: 13.5px; line-height: 1.9; color: #475569; padding: 6px 0 6px 18px; position: relative; border-bottom: 1px dashed #f1f5f9; }}
.profile-list li:last-child {{ border-bottom: none; }}
.profile-list li::before {{ content: '▸'; position: absolute; left: 2px; color: #2c6ecb; }}
.timeline {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 24px; margin-bottom: 28px; }}
.tl-year-head {{ display: flex; align-items: center; gap: 10px; margin: 20px 0 12px; }}
.tl-year-head:first-child {{ margin-top: 0; }}
.tl-year {{ font-size: 13px; font-weight: 700; color: #fff; background: #2c6ecb; padding: 3px 14px; border-radius: 14px; white-space: nowrap; }}
.tl-year-line {{ flex: 1; height: 1px; background: #e2e8f0; }}
.tl-sem-row {{ display: flex; gap: 12px; margin-bottom: 12px; }}
.tl-sem {{ flex-shrink: 0; width: 70px; font-size: 12.5px; font-weight: 600; color: #2c6ecb; text-align: center; background: #eff6ff; border-radius: 4px; padding: 4px 0; align-self: flex-start; }}
.tl-items {{ flex: 1; min-width: 0; }}
.tl-item {{ font-size: 13px; line-height: 1.8; color: #475569; padding: 3px 0; }}
.tl-item.advice-item {{ background: #fffbeb; border-left: 3px solid #fbbf24; padding-left: 8px; border-radius: 0 4px 4px 0; }}
.action-card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; margin-bottom: 16px; }}
.action-title {{ font-size: 15px; font-weight: 700; color: #2c6ecb; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid #f1f5f9; }}
.action-sec {{ margin-bottom: 10px; display: flex; }}
.action-sec-label {{ flex-shrink: 0; width: 96px; font-size: 12.5px; font-weight: 600; color: #64748b; padding-top: 2px; }}
.action-sec ul {{ margin: 0; padding: 0; list-style: none; flex: 1; }}
.action-sec li {{ font-size: 13px; line-height: 1.8; color: #475569; }}
.action-sec li::before {{ content: '·'; font-weight: 700; margin-right: 6px; color: #94a3b8; }}
.gap-card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; margin-bottom: 28px; }}
.gap-status {{ font-size: 13.5px; line-height: 1.8; color: #475569; margin-bottom: 14px; }}
.gap-top3 {{ margin: 0; padding-left: 22px; }}
.gap-top3 li {{ font-size: 13.5px; line-height: 1.9; color: #1e293b; }}
.gap-alt {{ font-size: 13px; color: #64748b; line-height: 1.8; }}
.gap-alt b {{ color: #475569; }}
.ref-list {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; margin-bottom: 28px; }}
.ref-list ol {{ margin: 0; padding-left: 22px; }}
.ref-list li {{ font-size: 12.5px; line-height: 1.9; color: #64748b; }}
.table-wrap {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; margin-bottom: 28px; overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ padding: 10px 14px; background: #f5f7fa; font-weight: 600; text-align: left; border-bottom: 2px solid #e2e8f0; }}
td {{ padding: 10px 14px; color: #475569; border-bottom: 1px solid #e2e8f0; }}
tr:last-child td {{ border-bottom: none; }}
.layer-legend {{ display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }}
.legend-chip {{ display: inline-flex; align-items: center; gap: 6px; font-size: 12px; padding: 5px 12px; border-radius: 20px; border: 1px solid; }}
.legend-fact {{ background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }}
.legend-ai {{ background: #fffbeb; border-color: #fde68a; color: #b45309; }}
.fact-chip {{ display: inline-block; font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 10px; background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; vertical-align: 2px; margin-left: 8px; }}
.advice-badge {{ display: inline-block; font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 10px; background: #fffbeb; color: #b45309; border: 1px solid #fde68a; vertical-align: 2px; margin-left: 8px; }}
.advice-card {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 16px 20px; margin-bottom: 12px; }}
.advice-title {{ font-size: 14px; font-weight: 600; color: #b45309; margin-bottom: 10px; }}
.advice-list {{ margin: 0; padding-left: 20px; }}
.advice-list li {{ font-size: 13px; line-height: 1.9; color: #78350f; margin-bottom: 4px; }}
.base-bar {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 12px 16px; font-size: 12.5px; color: #1d4ed8; margin-bottom: 20px; line-height: 1.7; }}
.base-bar span {{ font-weight: 700; margin-right: 6px; }}
.base-bar b {{ font-size: 15px; margin: 0 2px; }}
.mindmap {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 22px; margin-bottom: 24px; }}
.mm-root {{ display: inline-block; background: #1d4ed8; color: #fff; font-size: 14px; font-weight: 700; padding: 8px 26px; border-radius: 20px; margin-bottom: 8px; }}
.mm-branches {{ border-left: 2px solid #cbd5e1; margin-left: 16px; }}
.mm-cat {{ display: flex; align-items: flex-start; padding: 12px 0 2px 0; }}
.mm-cat-conn {{ width: 20px; height: 2px; background: #cbd5e1; margin-top: 18px; flex: none; }}
.mm-cat-label {{ flex: none; width: 108px; font-size: 13px; font-weight: 700; color: #1e40af; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 10px 8px; margin-right: 14px; text-align: center; align-self: flex-start; }}
.mm-main {{ flex: 1; min-width: 0; }}
.mm-rec {{ font-size: 12px; color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 10px; margin-bottom: 8px; line-height: 1.6; }}
.mm-years {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }}
.mm-year {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 9px; }}
.mm-year-label {{ font-size: 12px; font-weight: 700; color: #475569; margin-bottom: 6px; }}
.mm-items {{ display: flex; flex-direction: column; gap: 4px; }}
.mm-item {{ font-size: 12px; line-height: 1.6; color: #334155; background: #f1f5f9; border-radius: 6px; padding: 4px 8px; word-break: break-all; }}
.mm-item .ref-sup {{ margin-left: 4px; }}
.mm-empty {{ color: #cbd5e1; text-align: center; background: transparent; }}
.mm-star {{ color: #b45309; font-weight: 700; }}
.fy-grid {{ display: grid; grid-template-columns: 11fr 9fr; gap: 16px; margin-bottom: 24px; align-items: start; }}
.fy-col {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 20px; }}
.fy-col-head {{ font-size: 14.5px; font-weight: 700; color: #1d4ed8; padding-bottom: 10px; border-bottom: 2px solid #eff6ff; margin-bottom: 12px; }}
.fy-col-head.fy-head-ai {{ color: #b45309; border-bottom-color: #fef3c7; }}
.fy-col-sub {{ display: block; font-size: 11px; font-weight: 500; color: #94a3b8; margin-top: 2px; }}
.fy-year {{ margin-bottom: 14px; }}
.fy-year:last-child {{ margin-bottom: 0; }}
.fy-year-label {{ display: inline-block; font-size: 12px; font-weight: 700; color: #fff; background: #2c6ecb; padding: 2px 14px; border-radius: 12px; margin-bottom: 8px; }}
.fy-year-ai {{ background: #d97706; }}
.fy-sem {{ display: flex; gap: 10px; margin-bottom: 8px; }}
.fy-sem-label {{ flex: none; width: 64px; font-size: 11.5px; font-weight: 600; color: #1d4ed8; background: #eff6ff; border-radius: 4px; padding: 3px 0; text-align: center; align-self: flex-start; }}
.fy-sem-items {{ flex: 1; min-width: 0; }}
.fy-course {{ font-size: 12.5px; line-height: 1.9; color: #334155; }}
.ct-tag {{ display: inline-block; font-size: 10.5px; font-weight: 600; padding: 0 6px; border-radius: 8px; margin-right: 6px; border: 1px solid; vertical-align: 1px; white-space: nowrap; }}
.ct-0 {{ background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }}
.ct-1 {{ background: #f5f3ff; color: #6d28d9; border-color: #ddd6fe; }}
.ct-2 {{ background: #ecfeff; color: #0e7490; border-color: #a5f3fc; }}
.ct-3 {{ background: #f1f5f9; color: #475569; border-color: #e2e8f0; }}
.ct-plain {{ background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }}
.fy-block {{ margin-bottom: 10px; }}
.fy-block:last-child {{ margin-bottom: 0; }}
.fy-block-label {{ font-size: 12px; font-weight: 700; color: #b45309; margin-bottom: 3px; }}
.fy-block ul {{ margin: 0; padding-left: 16px; }}
.fy-block li {{ font-size: 12.5px; line-height: 1.8; color: #475569; }}
.fy-empty {{ color: #cbd5e1; font-size: 12px; text-align: center; padding: 16px 0; }}
.cp-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-bottom: 24px; }}
.cp-card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px 18px; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }}
.cp-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }}
.cp-name {{ font-size: 15px; font-weight: 700; color: #0f172a; }}
.cp-type {{ font-size: 11px; font-weight: 600; padding: 2px 10px; border-radius: 12px; background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; white-space: nowrap; }}
.cp-type-policy {{ background: #ecfdf5; color: #047857; border-color: #a7f3d0; }}
.cp-type-job {{ background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }}
.cp-type-master {{ background: #f5f3ff; color: #6d28d9; border-color: #ddd6fe; }}
.cp-type-phd {{ background: #ecfeff; color: #0e7490; border-color: #a5f3fc; }}
.cp-type-exam {{ background: #fff7ed; color: #c2410c; border-color: #fed7aa; }}
.cp-overview {{ font-size: 13px; color: #475569; line-height: 1.7; margin-bottom: 10px; }}
.cp-steps {{ border-left: 2px solid #e2e8f0; margin: 0 0 10px 6px; padding-left: 16px; display: flex; flex-direction: column; gap: 8px; }}
.cp-step {{ position: relative; font-size: 12.5px; line-height: 1.7; color: #334155; }}
.cp-step::before {{ content: ''; position: absolute; left: -21px; top: 7px; width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; }}
.cp-period {{ display: inline-block; font-size: 11px; font-weight: 700; color: #1d4ed8; background: #eff6ff; border-radius: 6px; padding: 1px 8px; margin-right: 8px; vertical-align: 1px; }}
.cp-advice {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 10px 12px; font-size: 12.5px; color: #78350f; }}
.cp-advice b {{ display: block; margin-bottom: 4px; font-size: 12.5px; color: #b45309; }}
.cp-advice ul {{ margin: 0; padding-left: 18px; }}
.cp-advice li {{ margin-bottom: 3px; line-height: 1.7; }}
.cp-mm {{ display: flex; align-items: center; gap: 26px; background: linear-gradient(180deg, #f0f6ff 0%, #ffffff 90px); border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px 14px 12px; margin-bottom: 24px; }}
.cp-mm-root {{ display: flex; align-items: center; gap: 10px; writing-mode: vertical-rl; position: relative; flex-shrink: 0; align-self: center; max-height: 280px; margin: 0; padding: 14px 10px; background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); border-radius: 8px; box-shadow: 0 2px 8px rgba(37,99,235,0.25); }}
.cp-mm-root::after {{ content: ''; position: absolute; top: 50%; right: -26px; width: 26px; border-top: 2px solid #cbd5e1; }}
.cp-mm-root-badge {{ padding: 2px 9px; border-radius: 999px; background: rgba(255,255,255,0.22); color: #fff; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
.cp-mm-root-name {{ color: #fff; font-size: 15px; font-weight: 700; letter-spacing: 0.5px; }}
.cp-mm-branches {{ flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; gap: 10px; margin-top: 0; padding-left: 0; }}
.cp-mm-branch {{ position: relative; margin-left: 26px; padding: 12px 14px; background: #fff; border: 1px solid #e2e8f0; border-left: 3px solid #93c5fd; border-radius: 8px; }}
.cp-mm-branch::before {{ content: ''; position: absolute; left: -26px; top: 50%; width: 26px; border-top: 2px solid #cbd5e1; }}
.cp-mm-branch::after {{ content: ''; position: absolute; left: -26px; top: -10px; bottom: -10px; border-left: 2px solid #cbd5e1; }}
.cp-mm-branch:first-child::after {{ top: 50%; }}
.cp-mm-branch:last-child::after {{ bottom: 50%; }}
.cp-mm-branch:only-child::after {{ display: none; }}
.cp-mm-flow {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }}
.cp-mm-chip {{ display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 999px; background: #f1f5f9; border: 1px solid #e2e8f0; font-size: 12px; line-height: 1.6; color: #1a202c; }}
.cp-mm-chip i {{ font-style: normal; font-size: 10px; font-weight: 700; opacity: 0.72; }}
.cp-mm-chip-skill {{ background: #dbeafe; border-color: #bfdbfe; color: #1d4ed8; }}
.cp-mm-chip-course {{ background: #d1fae5; border-color: #a7f3d0; color: #047857; }}
.cp-mm-chip-comp {{ background: #fef3c7; border-color: #fde68a; color: #b45309; }}
.cp-mm-chip-text {{ background: #f1f5f9; color: #475569; }}
.cp-mm-arrow {{ color: #94a3b8; font-size: 13px; flex-shrink: 0; }}
.cp-mm-arrow-final {{ color: #2563eb; font-size: 15px; font-weight: 700; margin: 0 3px; }}
.cp-mm-target {{ display: inline-flex; align-items: center; padding: 5px 16px; border-radius: 999px; background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); color: #fff; font-size: 13px; font-weight: 700; box-shadow: 0 2px 6px rgba(37,99,235,0.3); white-space: nowrap; }}
.cp-mm-note {{ margin-top: 9px; }}
.cp-mm-overview {{ font-size: 12px; color: #64748b; line-height: 1.7; margin-bottom: 5px; }}
.cp-mm-advice {{ margin: 8px 0 2px; padding: 7px 12px 8px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; }}
.cp-mm-advice-label {{ font-size: 11px; font-weight: 700; color: #b45309; letter-spacing: 0.5px; margin-bottom: 6px; }}
.cp-mm-advice-list {{ margin: 0; padding-left: 16px; }}
.cp-mm-advice-list li {{ font-size: 12px; color: #78350f; line-height: 1.7; }}
.cp-mm-branch-study {{ border-left-color: #c4b5fd; }}
.cp-mm-target-study {{ background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); box-shadow: 0 2px 6px rgba(124,58,237,0.3); }}
.cp-mm-target-study i {{ font-style: normal; font-size: 10px; font-weight: 400; opacity: 0.9; background: rgba(255,255,255,0.2); border-radius: 999px; padding: 1px 8px; margin-right: 6px; }}
.cp-mm-steps {{ margin: 7px 0 8px; padding: 7px 10px 8px; background: #faf5ff; border: 1px solid #ede9fe; border-radius: 8px; }}
.cp-mm-steps-label {{ font-size: 11px; font-weight: 700; color: #6d28d9; letter-spacing: 0.5px; margin-bottom: 6px; }}
.cp-mm-step-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 5px 16px; }}
.cp-mm-step {{ display: flex; align-items: flex-start; gap: 7px; font-size: 12px; line-height: 1.6; }}
.cp-mm-period {{ flex-shrink: 0; padding: 1px 8px; border-radius: 6px; background: #ede9fe; border: 1px solid #ddd6fe; color: #6d28d9; font-size: 11px; font-weight: 700; white-space: nowrap; }}
.cp-mm-action {{ color: #334155; min-width: 0; }}
@media(max-width: 900px) {{
  .cp-grid {{ grid-template-columns: 1fr; }}
  .cp-mm {{ display: block; padding: 12px 10px 10px; }}
  .cp-mm-root {{ writing-mode: horizontal-tb; margin: 0 auto 6px; padding: 7px 14px; max-height: none; }}
  .cp-mm-root::after {{ display: none; }}
  .cp-mm-branches {{ display: block; padding-left: 8px; }}
  .cp-mm-branch {{ margin-left: 0; margin-bottom: 10px; padding: 12px 14px; }}
  .cp-mm-branch::before {{ display: none; }}
  .cp-mm-branch::after {{ display: none; }}
  .cp-mm-target {{ white-space: normal; }}
  .cp-mm-step-grid {{ grid-template-columns: 1fr; }}
  .fy-grid {{ grid-template-columns: 1fr; }}
  .mm-years {{ grid-template-columns: repeat(2, 1fr); }}
  .mm-cat {{ flex-direction: column; }}
  .mm-cat-conn {{ display: none; }}
  .mm-cat-label {{ width: auto; margin-right: 0; margin-bottom: 8px; }}
}}
</style>
</head>
<body>
<div class="container">
<h1>{analysis.get('title', '分析报告')}</h1>
<div class="meta">关键词：{keyword}　|　维度：{dim_name}　|　报告模式：{mode_label}　|　展开层级：{depth_str}层　|　生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
<div class="meta" style="font-size:11px;color:#94a3b8;">接地校验：删除句子 {validation.get('deleted_sentences', 0)}，补来源 {validation.get('patched_sources', 0)}，补AI标签 {validation.get('patched_ai_tags', 0)}，课程降级 {validation.get('course_downgraded', 0)}，非法标签修正 {validation.get('bad_tags', 0)}，学期归一 {validation.get('bad_stages', 0)}，剔除无效项 {validation.get('invalid_items_removed', 0)}，剔除建议编造 {validation.get('advice_items_removed', 0)}，剔除陈词 {validation.get('cliche_sentences_removed', 0)}，重写 {validation.get('rewritten_sentences', 0)}，替换违规单元格 {validation.get('replaced_cells', 0)}，裁剪超长 {validation.get('length_trimmed', 0)}，缺失学期 {len(validation.get('missing_semesters') or [])}/{len(grade_scope_semesters.split('、')) if grade_scope_semesters else 8}，行动项回填 {validation.get('action_backfilled', 0)}</div>
<div class="base-bar"><span>图谱数据基座</span>本报告基于就业知识图谱子图生成：<b>{len(nodes)}</b> 个实体 · <b>{len(edges)}</b> 条关联 · 覆盖 {len(all_dims)} 个维度（{'、'.join(all_dims) if all_dims else '综合'}）· <b>{len(all_srcs)}</b> 份来源文档 —— 正文上标 <b style="font-size:12px">[Sn]</b> 可对应文末「数据来源」逐条溯源；黄色「AI建议」条目为模型补全，仅供参考</div>
<div class="layer-legend">
<span class="legend-chip legend-fact">L1 官方课程 · 培养方案/职业标准权威骨架</span>
<span class="legend-chip" style="background:#ecfdf5;border-color:#a7f3d0;color:#047857;">L2 技能/竞赛 · 多源抽取（职业标准·行业报告·竞赛简章）</span>
<span class="legend-chip legend-ai">L3 AI 建议 · 模型补全，仅供参考</span>
</div>
''')

        # ---- 上标引用收集：[来源：xx] → [Sn]，文末生成数据来源区 ----
        ref_list = []

        # 反馈4：选择年级后，渲染与返回前端的 analysis 均只保留该年级至大四的条目
        if user_grade in ('大一', '大二', '大三', '大四'):
            _scope_years_r = ('大一', '大二', '大三', '大四')[('大一', '大二', '大三', '大四').index(user_grade):]

            def _year_in_scope_r(st):
                m_yr = re.search(r'大一|大二|大三|大四', str(st or ''))
                return bool(m_yr and m_yr.group(0) in _scope_years_r)

            analysis['four_year_plan'] = [p for p in (analysis.get('four_year_plan') or [])
                                          if isinstance(p, dict) and _year_in_scope_r(p.get('stage'))]
            analysis['action_list'] = [a for a in (analysis.get('action_list') or [])
                                       if isinstance(a, dict) and _year_in_scope_r(a.get('stage'))]
            _mm_r = analysis.get('mindmap')
            if isinstance(_mm_r, dict):
                for _c_r in (_mm_r.get('categories') or []):
                    if isinstance(_c_r, dict):
                        _c_r['years'] = [y for y in (_c_r.get('years') or [])
                                         if isinstance(y, dict) and _year_in_scope_r(y.get('year'))]

        def _render_refs(text):
            escaped = html_module.escape(str(text or ''))
            out, pos = [], 0
            for m in re.finditer(r'\[来源[：:]\s*([^\[\]]{2,80})\]', escaped):
                src = re.split(r'P?\s*(?:&quot;|["\u201c\u201d])', m.group(1).strip())[0].strip()
                src = re.sub(r'(?:\s+(?:[Pp]\d+(?:\s*[,，、~\-至]\s*[Pp]?\d+)*|\d{1,3}(?:\s*[,，、~\-至]\s*\d{1,3})*|第\s*\d+\s*页|无))+$', '', src)
                if not src:
                    continue
                if src not in ref_list:
                    ref_list.append(src)
                out.append(escaped[pos:m.start()])
                out.append(f'<span class="ref-sup">[S{ref_list.index(src) + 1}]</span>')
                pos = m.end()
            out.append(escaped[pos:])
            return ''.join(out)

        if analysis.get('summary'):
            html_parts.append(f'<div class="summary">{_render_refs(analysis["summary"]).replace(chr(10), "<br>")}</div>')

        profile = analysis.get('target_profile') or {}
        profile_title = str(profile.get('title') or '目标岗位画像').strip()
        profile_items = [str(it).strip() for it in (profile.get('items') or []) if str(it).strip()]
        if profile_title or profile_items:
            html_parts.append(f'<h2>{html_module.escape(profile_title)}<span class="fact-chip">L1 权威 + L2 抽取</span></h2>')
            if profile_items:
                html_parts.append('<div class="profile-card"><h3>画像要点</h3><ul class="profile-list">')
                for it in profile_items:
                    html_parts.append(f'<li>{_render_refs(it)}</li>')
                html_parts.append('</ul></div>')

        # ---- M4附加：个性化培养总览（两栏巨图：左=课程主线·按学期 / 右=竞赛技能AI建议·按年） ----
        mindmap = analysis.get('mindmap') or {}
        mm_cats = [c for c in (mindmap.get('categories') or [])
                   if isinstance(c, dict) and str(c.get('name') or '').strip()]
        mm_is_m4 = bool(mm_cats) and mode == 'm4'
        if mm_is_m4:
            COURSE_TAG_ORDER = ['大类通识课', '专业必修课', '专业选修课', '选修课']
            SEM_ORDER = ['大一·秋', '大一·春', '大二·秋', '大二·春', '大三·秋', '大三·春', '大四·秋', '大四·春']
            YEAR_ORDER = ['大一', '大二', '大三', '大四']

            def _new_bucket():
                return {'goals': [], 'skills': [], 'comps': [], 'advice': [], 'courses': [], 'pitfalls': []}

            # ---- 左栏数据：课程收集 [(课程名, 学期组, 标签, 星标)] ----
            courses = {}
            seen_courses = set()
            rec_note_by_tag = {}

            def _add_course(disp, sem_key, tag, star):
                if re.search(r'英语|体育', str(disp or '')):
                    return
                key = (disp, sem_key)
                if key in seen_courses:
                    return
                seen_courses.add(key)
                courses.setdefault(sem_key, []).append((disp, tag, star))

            def _render_course_item(name, tag, star):
                cls = ('ct-' + str(COURSE_TAG_ORDER.index(tag))) if tag in COURSE_TAG_ORDER else 'ct-plain'
                star_html = '<span class="mm-star">★建议优先</span>' if star else ''
                return f'<div class="fy-course"><span class="ct-tag {cls}">{html_module.escape(tag)}</span>{html_module.escape(name)}{star_html}</div>'

            for c in mm_cats:
                cname = str(c.get('name')).strip()
                note = str(c.get('recommend_note') or '').strip()
                if note:
                    rec_note_by_tag[cname] = note
                if cname == '竞赛实践':
                    continue
                for y in (c.get('years') or []):
                    if not isinstance(y, dict):
                        continue
                    yname = str(y.get('year') or '').strip()
                    for it in (y.get('items') or []):
                        it = str(it).strip()
                        mname = re.search(r'《([^》]+)》', it)
                        if not it or not mname:
                            continue
                        disp = '《' + mname.group(1) + '》'
                        star = '★' in it
                        sems = re.findall(r'大[一二三四]·[秋春]', it)
                        if sems:
                            for sm in sems:
                                _add_course(disp, sm, cname, star)
                        else:
                            _add_course(disp, (yname if yname in YEAR_ORDER else '其他') + '·未标注学期', cname, star)
            for p in (analysis.get('four_year_plan') or []):
                if not isinstance(p, dict) or str(p.get('tag') or '') != 'course':
                    continue
                mname = re.search(r'《([^》]+)》', str(p.get('text') or ''))
                if not mname:
                    continue
                stage = str(p.get('stage') or '').strip()
                if stage in SEM_ORDER:
                    _add_course('《' + mname.group(1) + '》', stage, '官方课程', False)
                else:
                    yr_part = stage.split('·')[0] if stage else ''
                    _add_course('《' + mname.group(1) + '》', (yr_part if yr_part in YEAR_ORDER else '其他') + '·未标注学期', '官方课程', False)

            # ---- 右栏数据：按学年分组（竞赛/技能/建议） ----
            years_data = {}
            for a in (analysis.get('action_list') or []):
                if not isinstance(a, dict):
                    continue
                st = str(a.get('stage') or '').strip()
                if not st:
                    continue
                bucket = years_data.setdefault(st, _new_bucket())
                for key in ('goals', 'skills', 'comps', 'courses', 'pitfalls'):
                    for it in (a.get(key) or []):
                        it = str(it).strip()
                        if it and it not in bucket[key]:
                            bucket[key].append(it)
            for c in mm_cats:
                if str(c.get('name')).strip() != '竞赛实践':
                    continue
                for y in (c.get('years') or []):
                    if not isinstance(y, dict):
                        continue
                    yname = str(y.get('year') or '').strip()
                    if not yname:
                        continue
                    bucket = years_data.setdefault(yname, _new_bucket())
                    for it in (y.get('items') or []):
                        it = str(it).strip()
                        if it and it not in bucket['comps']:
                            bucket['comps'].append(it)
            for p in (analysis.get('four_year_plan') or []):
                if not isinstance(p, dict):
                    continue
                tag = str(p.get('tag') or '')
                text = str(p.get('text') or '').strip()
                stage = str(p.get('stage') or '').strip()
                yr = stage.split('·')[0] if '·' in stage else stage
                if not text or yr not in YEAR_ORDER or tag not in ('skill', 'comp', 'advice'):
                    continue
                bucket = years_data.setdefault(yr, _new_bucket())
                key = {'skill': 'skills', 'comp': 'comps', 'advice': 'advice'}[tag]
                if text not in bucket[key]:
                    bucket[key].append(text)

            # ---- 渲染两栏总览 ----
            html_parts.append(f'<h2>{html_module.escape(str(mindmap.get("title") or "个性化培养总览"))}<span class="fact-chip">官方培养方案骨架 + 分层补全</span></h2>')
            html_parts.append('<div class="fy-grid">')
            # 左栏：课程主线（按学期）
            html_parts.append('<div class="fy-col fy-courses">')
            html_parts.append('<div class="fy-col-head">课程主线<span class="fy-col-sub">官方课程 + 个性化培养 · 按学期</span></div>')
            for tg in COURSE_TAG_ORDER:
                if rec_note_by_tag.get(tg):
                    html_parts.append(f'<div class="mm-rec">选课策略（AI建议）：{html_module.escape(rec_note_by_tag[tg])}</div>')
            total_courses = sum(len(v) for v in courses.values())
            html_parts.append('<div class="fy-course-body">')
            if total_courses == 0:
                html_parts.append('<div class="fy-empty">暂无课程数据</div>')
            for yr in YEAR_ORDER:
                sems = [s for s in SEM_ORDER if s.startswith(yr) and s in courses]
                no_sem = courses.get(yr + '·未标注学期') or []
                if not sems and not no_sem:
                    continue
                html_parts.append('<div class="fy-year">')
                html_parts.append(f'<span class="fy-year-label">{html_module.escape(yr)}</span>')
                for sm in sems + ([yr + '·未标注学期'] if no_sem else []):
                    html_parts.append('<div class="fy-sem">')
                    html_parts.append(f'<span class="fy-sem-label">{html_module.escape("学期未标" if "未标注" in sm else sm)}</span>')
                    html_parts.append('<div class="fy-sem-items">')
                    for (name, tag, star) in courses[sm]:
                        html_parts.append(_render_course_item(name, tag, star))
                    html_parts.append('</div></div>')
                html_parts.append('</div>')
            leftover = [k for k in courses if k not in SEM_ORDER and not any(k.startswith(y) for y in YEAR_ORDER)]
            for k in leftover:
                html_parts.append('<div class="fy-sem">')
                html_parts.append('<span class="fy-sem-label">学期未标</span>')
                html_parts.append('<div class="fy-sem-items">')
                for (name, tag, star) in courses[k]:
                    html_parts.append(_render_course_item(name, tag, star))
                html_parts.append('</div></div>')
            html_parts.append('</div></div>')
            # 右栏：竞赛 / 技能 / AI建议（按学年）
            html_parts.append('<div class="fy-col fy-extras">')
            html_parts.append('<div class="fy-col-head fy-head-ai">竞赛 / 技能 / AI建议<span class="fy-col-sub">L2 + L3 · 仅供参考</span></div>')
            BLOCK_META = [('goals', '核心目标'), ('skills', '技能要求'), ('comps', '推荐竞赛'),
                          ('advice', 'AI建议'), ('courses', '课程建议'), ('pitfalls', '避坑提示')]
            html_parts.append('<div class="fy-course-body">')
            rendered_years = [y for y in YEAR_ORDER if y in years_data] + [y for y in years_data if y not in YEAR_ORDER]
            has_extra = False
            for yr in rendered_years:
                data = years_data[yr]
                if not any(data[k] for k, _ in BLOCK_META):
                    continue
                has_extra = True
                html_parts.append('<div class="fy-year">')
                html_parts.append(f'<span class="fy-year-label fy-year-ai">{html_module.escape(yr)}</span>')
                for key, label in BLOCK_META:
                    items = data[key]
                    if not items:
                        continue
                    html_parts.append('<div class="fy-block">')
                    html_parts.append(f'<div class="fy-block-label">{label}</div>')
                    html_parts.append('<ul>')
                    for it in items:
                        html_parts.append(f'<li>{_render_refs(it)}</li>')
                    html_parts.append('</ul></div>')
                html_parts.append('</div>')
            if not has_extra:
                html_parts.append('<div class="fy-empty">暂无竞赛/建议数据</div>')
            html_parts.append('</div></div></div>')

        # ---- M5附加：职业方向多路径（就业+升学读研/考研/直博=发散导图：专业→技能/课程/竞赛→目标，升学分支内嵌时间流程；政策=对比卡片）----
        cp_data = analysis.get('career_paths') or {}
        cp_paths = [p for p in (cp_data.get('paths') or [])
                    if isinstance(p, dict) and str(p.get('name') or '').strip()]
        if cp_paths:
            html_parts.append(f'<h2>{html_module.escape(str(cp_data.get("title") or "职业方向路径图"))}<span class="fact-chip">事实步骤接地溯源 · 规划步骤为AI建议</span></h2>')
            TYPE_CLS = {'升学读研': 'cp-type-master', '直博': 'cp-type-phd',
                        '考研': 'cp-type-exam', '选调': 'cp-type-policy', '支教保研': 'cp-type-policy',
                        '干部保研': 'cp-type-policy'}
            STUDY_TYPES = {'升学读研', '保研', '直博', '考研'}
            job_paths = [p for p in cp_paths if str(p.get('type') or '').strip() == '就业' or str(p.get('type') or '').strip() in STUDY_TYPES]
            other_paths = [p for p in cp_paths if str(p.get('type') or '').strip() != '就业' and str(p.get('type') or '').strip() not in STUDY_TYPES]
            # 就业+升学路径：发散导图（与主页面同构；分支内嵌时间流程+路径建议面板）
            if job_paths:
                root_major = str(cp_data.get('root_major') or '').strip() or '本专业'
                html_parts.append('<div class="cp-mm">')
                html_parts.append(f'<div class="cp-mm-root"><span class="cp-mm-root-badge">专业</span><span class="cp-mm-root-name">{html_module.escape(root_major)}</span></div>')
                html_parts.append('<div class="cp-mm-branches">')
                for p in job_paths:
                    mm_nodes = [n for n in (p.get('path_nodes') or [])
                                if isinstance(n, dict) and str(n.get('name') or '').strip()]
                    ptype = str(p.get('type') or '').strip()
                    is_study = ptype in STUDY_TYPES
                    if is_study:
                        job_name = re.sub(r'^(升学读研|保研|考研|直博)[-—·\s]*', '', str(p.get('name')).strip())
                    else:
                        job_name = re.sub(r'^就业[-—·\s]*', '', str(p.get('name')).strip())
                    branch_cls = ' cp-mm-branch-study' if is_study else ''
                    html_parts.append(f'<div class="cp-mm-branch{branch_cls}"><div class="cp-mm-flow">')
                    if not mm_nodes and str(p.get('overview') or '').strip():
                        html_parts.append(f'<span class="cp-mm-chip cp-mm-chip-text">{_render_refs(str(p.get("overview")).strip())}</span>')
                    else:
                        for ni, n in enumerate(mm_nodes):
                            if ni > 0:
                                html_parts.append('<span class="cp-mm-arrow">→</span>')
                            ntype = str(n.get('type') or '').strip()
                            ncls = ('cp-mm-chip-skill' if '技能' in ntype
                                    else 'cp-mm-chip-course' if '课程' in ntype
                                    else 'cp-mm-chip-comp' if '竞赛' in ntype else '')
                            html_parts.append('<span class="cp-mm-chip ' + ncls + '">' + (f'<i>{html_module.escape(ntype)}</i>' if ntype else '') + html_module.escape(str(n.get('name')).strip()) + '</span>')
                    html_parts.append('<span class="cp-mm-arrow cp-mm-arrow-final">⇒</span>')
                    if is_study:
                        tbadge = f'<i>{html_module.escape(ptype)}</i>' if ptype else ''
                        html_parts.append(f'<span class="cp-mm-target cp-mm-target-study">{tbadge}{html_module.escape(job_name)}</span>')
                    else:
                        html_parts.append(f'<span class="cp-mm-target">{html_module.escape(job_name)}</span>')
                    html_parts.append('</div>')
                    pover_mm = str(p.get('overview') or '').strip()
                    padv_mm = [str(x).strip() for x in (p.get('advice') or []) if str(x).strip()]
                    cp_steps_mm = [s for s in (p.get('steps') or [])
                                   if isinstance(s, dict) and str(s.get('action') or '').strip()]
                    if pover_mm or padv_mm or cp_steps_mm:
                        html_parts.append('<div class="cp-mm-note">')
                        if pover_mm:
                            html_parts.append(f'<div class="cp-mm-overview">{_render_refs(pover_mm)}</div>')
                        if cp_steps_mm:
                            html_parts.append('<div class="cp-mm-steps"><div class="cp-mm-steps-label">时间流程</div><div class="cp-mm-step-grid">')
                            for s in cp_steps_mm:
                                period = str(s.get('period') or '').strip() or '—'
                                html_parts.append(f'<div class="cp-mm-step"><span class="cp-mm-period">{html_module.escape(period)}</span><span class="cp-mm-action">{_render_refs(s.get("action"))}</span></div>')
                            html_parts.append('</div></div>')
                        if padv_mm:
                            html_parts.append('<div class="cp-mm-advice"><div class="cp-mm-advice-label">路径建议</div><ul class="cp-mm-advice-list">')
                            for it in padv_mm:
                                html_parts.append(f'<li>{_render_refs(it)}</li>')
                            html_parts.append('</ul></div>')
                        html_parts.append('</div>')
                    html_parts.append('</div>')
                html_parts.append('</div></div>')
            # 政策路径：对比卡片
            if other_paths:
                html_parts.append('<div class="cp-grid">')
                for p in other_paths:
                    pname = str(p.get('name')).strip()
                    ptype = str(p.get('type') or '').strip()
                    pover = str(p.get('overview') or '').strip()
                    padv = [str(x).strip() for x in (p.get('advice') or []) if str(x).strip()]
                    cp_steps = [s for s in (p.get('steps') or [])
                                if isinstance(s, dict) and str(s.get('action') or '').strip()]
                    html_parts.append('<div class="cp-card">')
                    html_parts.append('<div class="cp-head">')
                    html_parts.append(f'<span class="cp-name">{html_module.escape(pname)}</span>')
                    if ptype:
                        type_cls = TYPE_CLS.get(ptype, '')
                        html_parts.append(f'<span class="cp-type {type_cls}">{html_module.escape(ptype)}</span>')
                    html_parts.append('</div>')
                    if pover:
                        html_parts.append(f'<div class="cp-overview">{html_module.escape(pover)}</div>')
                    if cp_steps:
                        html_parts.append('<div class="cp-steps">')
                        for s in cp_steps:
                            period = str(s.get('period') or '').strip()
                            html_parts.append('<div class="cp-step">')
                            if period:
                                html_parts.append(f'<span class="cp-period">{html_module.escape(period)}</span>')
                            html_parts.append(f'<span>{_render_refs(s.get("action"))}</span>')
                            html_parts.append('</div>')
                        html_parts.append('</div>')
                    if padv:
                        html_parts.append('<div class="cp-advice"><b>路径建议</b><ul>')
                        for it in padv:
                            html_parts.append(f'<li>{html_module.escape(it)}</li>')
                        html_parts.append('</ul></div>')
                    html_parts.append('</div>')
                html_parts.append('</div>')

        plan = [p for p in (analysis.get('four_year_plan') or []) if isinstance(p, dict) and str(p.get('text') or '').strip()]
        if plan and not mm_is_m4:
            _plan_title = f'培养路径总览（{user_grade}—大四）' if user_grade in ('大一', '大二', '大三', '大四') else '四年培养路径总览'
            html_parts.append(f'<h2>{_plan_title}<span class="fact-chip">官方培养方案骨架 + 分层补全</span></h2>')
            html_parts.append('<div class="timeline">')
            sem_order = ['大一·秋', '大一·春', '大二·秋', '大二·春', '大三·秋', '大三·春', '大四·秋', '大四·春']
            by_sem = {}
            for p in plan:
                by_sem.setdefault(str(p.get('stage') or '未标注'), []).append(p)
            ordered_sems = [s for s in sem_order if s in by_sem] + [s for s in by_sem if s not in sem_order]
            TAG_META = {'course': ('官方课程', 'tag-course'), 'skill': ('技能要求', 'tag-skill'),
                        'comp': ('竞赛参考', 'tag-comp'), 'advice': ('AI建议', 'tag-advice')}
            for yr in ['大一', '大二', '大三', '大四']:
                sems = [s for s in ordered_sems if s.startswith(yr)]
                if not sems:
                    continue
                html_parts.append('<div class="tl-year-head">')
                html_parts.append(f'<span class="tl-year">{yr}</span><span class="tl-year-line"></span>')
                html_parts.append('</div>')
                for sem in sems:
                    html_parts.append('<div class="tl-sem-row">')
                    html_parts.append(f'<div class="tl-sem">{html_module.escape(sem)}</div>')
                    html_parts.append('<div class="tl-items">')
                    for p in by_sem[sem]:
                        tag_label, tag_cls = TAG_META.get(str(p.get('tag') or 'advice'), TAG_META['advice'])
                        if tag_cls == 'tag-course' and re.search(r'英语|体育', str(p.get('text') or '')):
                            continue
                        advice_cls = ' advice-item' if tag_cls == 'tag-advice' else ''
                        html_parts.append(f'<div class="tl-item{advice_cls}"><span class="tag {tag_cls}">{tag_label}</span>{_render_refs(p.get("text"))}</div>')
                    html_parts.append('</div></div>')
            html_parts.append('</div>')

        if analysis.get('table') and analysis['table'].get('headers'):
            html_parts.append(f'<h2>{html_module.escape(analysis["table"].get("title", "数据汇总"))}<span class="fact-chip">图谱实证</span></h2>')
            html_parts.append('<div class="table-wrap"><table><thead><tr>')
            for h in analysis['table']['headers']:
                html_parts.append(f'<th>{html_module.escape(str(h))}</th>')
            html_parts.append('</tr></thead><tbody>')
            for row in analysis['table'].get('rows', []):
                html_parts.append('<tr>')
                for cell in row:
                    html_parts.append(f'<td>{_render_refs(str(cell))}</td>')
                html_parts.append('</tr>')
            html_parts.append('</tbody></table></div>')

        actions = [a for a in (analysis.get('action_list') or []) if isinstance(a, dict)]
        if actions and not mm_is_m4:
            html_parts.append('<h2>分阶段行动清单<span class="advice-badge">AI 建议 · 仅供参考</span></h2>')
            ACTION_BLOCKS = [('goals', '核心目标'), ('courses', '必学课程'), ('skills', '建议技能'),
                             ('comps', '推荐竞赛（含时间节点）'), ('pitfalls', '避坑提示')]
            for a in actions:
                html_parts.append('<div class="action-card">')
                html_parts.append(f'<div class="action-title">{html_module.escape(str(a.get("stage") or ""))}</div>')
                for key, label in ACTION_BLOCKS:
                    items = [str(x).strip() for x in (a.get(key) or []) if str(x).strip()]
                    if not items:
                        continue
                    html_parts.append('<div class="action-sec">')
                    html_parts.append(f'<div class="action-sec-label">{label}</div>')
                    html_parts.append('<ul>')
                    for it in items:
                        html_parts.append(f'<li>{_render_refs(it)}</li>')
                    html_parts.append('</ul></div>')
                html_parts.append('</div>')

        gap = analysis.get('gap_next') or {}
        gap_status = str(gap.get('status') or '').strip()
        gap_top3 = [str(x).strip() for x in (gap.get('top3') or []) if str(x).strip()]
        gap_alts = [str(x).strip() for x in (gap.get('alternatives') or []) if str(x).strip()]
        if gap_status or gap_top3 or gap_alts:
            html_parts.append('<h2>差距与下一步<span class="advice-badge">AI 建议 · 仅供参考</span></h2>')
            html_parts.append('<div class="gap-card">')
            if gap_status:
                html_parts.append(f'<div class="gap-status">{_render_refs(gap_status)}</div>')
            if gap_top3:
                html_parts.append('<div class="gap-alt" style="margin-bottom:10px"><b>下一步 Top3</b></div>')
                html_parts.append('<ol class="gap-top3">')
                for it in gap_top3:
                    html_parts.append(f'<li>{_render_refs(it)}</li>')
                html_parts.append('</ol>')
            if gap_alts:
                html_parts.append('<div class="gap-alt" style="margin-top:14px"><b>备选岗位 / 方向：</b>' + _render_refs('；'.join(gap_alts)) + '</div>')
            html_parts.append('</div>')

        if ref_list:
            html_parts.append('<h2 id="report-refs">数据来源<span class="fact-chip">正文上标 [Sn] 对应以下来源，统一汇总于此</span></h2>')
            html_parts.append('<div class="ref-list"><ol>')
            for i, src in enumerate(ref_list, 1):
                html_parts.append(f'<li>[S{i}]　{html_module.escape(src)}</li>')
            html_parts.append('</ol></div>')

        html_parts.append('</div></body></html>')

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(html_parts))

        return jsonify({'success': True, 'analysis': analysis, 'report_path': filename, 'truncated': truncated,
                        'llm_caps': {'nodes': MAX_LLM_NODES, 'edges': MAX_LLM_EDGES},
                        'mode': mode, 'mode_label': mode_label, 'validation': validation})

    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'LLM请求失败: {str(e)}'}), 500
    except json.JSONDecodeError as e:
        print(f"[ai-analyze] JSON解析失败: {str(e)}；内容长度={len(content)}，前300字符：{content[:300]!r}")
        return jsonify({'error': f'LLM返回解析失败: {str(e)}', 'raw': content[:500]}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'分析失败: {str(e)}'}), 500


# ============ 职业方向：岗位总览 + 九段式职业发展图（LLM生成，磁盘缓存） ============

CAREER_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cache', 'career')
os.makedirs(CAREER_CACHE_DIR, exist_ok=True)

_CAREER_OVERVIEW_SYSTEM = """你是一名资深的高校专业就业指导专家。根据给定专业名称，生成该专业就业岗位分类总览，供学生在职业方向报告中总览就业面。
要求：
1. 固定8个大类且顺序不变：通信与网络类、嵌入式与硬件类、芯片与半导体类、信号与算法类、控制与自动化类、软件与互联网类、产品与支持类、科研与教育类。
2. 每个大类包含：name（大类名）、desc（不超过16字的一句话简介）、jobs（该大类下5-8个具体岗位名，如"嵌入式软件工程师"）。
3. 岗位必须与该专业真实就业方向匹配，覆盖面广；岗位名简洁规范，不得编造明显不存在的岗位。
4. 只输出JSON，不要任何解释文字或markdown围栏。格式：
{"major":"专业名","categories":[{"name":"大类名","desc":"简介","jobs":["岗位1","岗位2"]}]}"""

_CAREER_DETAIL_SYSTEM = """你是一名资深的职业发展规划专家。针对指定专业的学生想从事的具体岗位，生成一条从入学到深造的九段式职业发展路径图。
九段名称与顺序必须逐字为：基础课程、专业核心课、技能树、项目竞赛、实习就业、岗位细分、学习阶段、求职准备、深造进阶。
要求：
1. sections数组严格按上述九段顺序排列，name逐字一致，不得增删改。
2. 除"学习阶段"外，每段items为3-6条短句（每条不超过40字）：课程给真实课程名、技能给具体技术名、竞赛给真实竞赛名、实习就业给可执行的行动建议、岗位细分给该岗位的典型细分方向、求职准备和深造进阶给阶段化建议。
3. "学习阶段"段的items为对象数组 [{"stage":"大一","text":"该阶段重点做什么"}]，覆盖大一至大四（必要时加研一/研二），每条不超过50字，内容与前面各段衔接。
4. 内容贴合"该专业背景→该岗位"的真实成长路径，具体可执行，不空谈。
5. 只输出JSON，不要任何解释文字或markdown围栏。格式：
{"job":"岗位名","major":"专业名","sections":[{"name":"基础课程","items":["课程1","课程2"]},{"name":"学习阶段","items":[{"stage":"大一","text":"..."}]}]}"""


def _career_cache_path(kind, key):
    slug = re.sub(r'[^\w]+', '_', key).strip('_')[:80] or 'default'
    return os.path.join(CAREER_CACHE_DIR, f'{kind}_{slug}.json')


def _career_llm_json(system_prompt, user_prompt, cache_path, refresh=False):
    """读缓存（refresh=1强制重新生成）→ 调LLM → 解析JSON（含截断修复）→ 写缓存"""
    if not refresh:
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if cached:
                return cached
        except Exception:
            pass
    content = _llm_chat(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=8192, temperature=0.3, timeout=600)
    if not content:
        return None
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text).strip()
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = json.loads(_repair_truncated_json(text))
        except Exception:
            data = None
    if not data:
        return None
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return data


@app.route('/api/career_overview', methods=['GET'])
def career_overview():
    """岗位分类总览（图二）：8大类岗位清单，LLM生成并缓存"""
    try:
        major = (request.args.get('major') or '').strip()
        if not major:
            return jsonify({'error': '缺少major参数'}), 400
        data = _career_llm_json(
            _CAREER_OVERVIEW_SYSTEM, f'专业名称：{major}',
            _career_cache_path('overview', major),
            refresh=request.args.get('refresh') == '1')
        if not data:
            return jsonify({'error': 'LLM生成岗位总览失败，请稍后重试'}), 500
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'岗位总览生成失败: {str(e)}'}), 500


@app.route('/api/career_detail', methods=['GET'])
def career_detail_api():
    """九段式职业发展图（图三）：基础课程→专业核心课→技能树→项目竞赛→实习就业→岗位细分→学习阶段→求职准备→深造进阶"""
    try:
        major = (request.args.get('major') or '').strip()
        job = (request.args.get('job') or '').strip()
        if not major or not job:
            return jsonify({'error': '缺少major或job参数'}), 400
        data = _career_llm_json(
            _CAREER_DETAIL_SYSTEM, f'学生专业：{major}\n目标岗位：{job}',
            _career_cache_path('detail', f'{major}|{job}'),
            refresh=request.args.get('refresh') == '1')
        if not data:
            return jsonify({'error': 'LLM生成职业发展图失败，请稍后重试'}), 500
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'职业发展图生成失败: {str(e)}'}), 500


# ============ 发展规划：读研/政策性就业/出国 → 分学年时间准备规划（LLM生成，磁盘缓存） ============

_PLAN_SYSTEM = """你是一名资深的高校升学就业规划导师。针对给定的【学生专业+规划路径】，生成一份"分学年时间准备规划"，指导本科生从大一到毕业逐年准备该路径。
输出严格JSON（不要任何解释文字或markdown围栏），格式：
{"title":"XX专业XX时间准备","sections":[{"name":"总体路线","groups":[{"name":"路线名","items":["短句1","短句2"]}]}]}
结构要求（sections共8-11个，按下列规则命名与排列）：
1. 第一个section固定为"总体路线"：groups为该路径的2-3条并行路线，每条路线items按"年级+动作"链展开（如"大一打基础""大三下夏令营""大四上预推免/九推"），最后加一个"核心区别"或"核心要求"组，对比各路线的关键门槛。
2. 中间各section为分学年准备："大一准备""大二准备""大三上准备""大三下准备""大三暑假准备""大四上准备""大四下准备"，按路径适用性取舍（无实质内容的学年可省略该section）；每个学年section含2-4个group，group命名按路径适配（如：学业、英语、科研竞赛、信息收集、报名材料、关键时间、关键提醒、关键目标），每个group 2-6条items。
3. 收尾2-3个section：命名按路径适配（如"考研初试科目""保研关键指标""重要时间节点""申请材料清单""语言考试要求"等），每组含2-4个group、每组2-6条items。
4. 所有items为短语（每条3-16字，如"保持绩点""六级刷分""5-6月投夏令营"），禁止长句；时间线必须真实（具体到月份/年级），内容贴合该专业真实课程与该路径真实流程；不得编造政策文件号、网址。
5. 只输出JSON。"""

_PLAN_ITEM_NOTES = {
    '保研': '国内推免（保研）。核心看绩点排名+科研竞赛+六级，无初试笔试。时间线：大一打基础→大二保排名、进实验室跟项目→大三上冲绩点、刷六级、出科研成果→大三下3-4月备材料（个人陈述/推荐信/成绩单排名证明）→5-6月投夏令营→6-7月参营争取优秀营员→大四上9月预推免、9月底-10月初九推系统填报确认录取→大四下提前进组做毕设、修研究生课程。',
    '考研': '全国硕士研究生统一招生考试（考研）。核心看初试分数+复试表现。时间线：大三上确定学硕/专硕与院校专业→大三下收集真题、开始数学英语一轮→大三暑假强化（黄金复习期，政治启动）→大四上9月预报名、10月正式报名、11月网上确认、12月底初试→大四下2月查分、3月国家线/自划线、3-4月复试、4-5月调剂→录取。初试科目：政治+英语一/二+数学一/二+专业课（按目标院校最新招生简章）。',
    '选调': '选调生（含定向选调与普通选调）。围绕：党员/学生干部/校级获奖等报考条件需从大一积累；大四上9-11月各省招录公告发布并报名；笔试（行测申论或综合案例分析）；大四下面试、考察、体检、公示录用；录用后一般基层锻炼2年左右；定向选调主要面向双一流高校毕业生。',
    '参军入伍': '大学生应征入伍。围绕：全国征兵网上半年（2月左右）与下半年（8月左右）两次应征报名；体检、政审与役前教育；大学生优先审批定兵；在校生可保留学籍退役复学，学费补偿代偿；毕业生可走直招军士；退役后考研加10分/立二等功免初试、专项硕士研究生招录计划。',
    '西部计划': '大学生志愿服务西部计划。围绕：应届毕业生或毕业2年内未就业可报；每年4-6月通过西部计划官网或学校项目办报名，含笔试面试体检；7月底培训出征；服务岗位含乡村教育、乡村建设、健康乡村、基层青年工作、乡村社会治理、卫国戍边等专项；服务期1-3年；期满享受考研初试加分、公务员定向考录等政策。',
    '三支一扶': '高校毕业生"三支一扶"计划（支教/支农/支医/帮扶乡村振兴）。围绕：每年5-7月各省公告报名，笔试（公共基础知识等）+面试体检；8-9月岗前培训后上岗；服务期一般2年，期间有工作生活补贴并缴纳社保；期满考核合格部分省份可直接转聘入编，或享受事业单位定向招录、考研加分。',
    '特岗计划': '农村义务教育阶段学校教师特设岗位计划。围绕：需持有教师资格证（或符合"先岗后证"政策）；每年4-6月各省公告报名，笔试（教育综合+学科知识）+面试；8月岗前培训后到乡镇义务教育学校任教；服务期3年，工资性补助由中央与地方共同承担；3年服务期满考核合格且愿意留任的入编定岗。',
    '国际组织任职实习': '国际组织任职/实习。围绕：英语（或法语等官方语言）流利是硬门槛，尽早考出雅思/托福；积累专业实力与国际视野（海外交换、模拟联合国、国际会议志愿者）；主要渠道：JPO初级专业人员项目、国家留学基金委国际组织实习项目资助、学校合作推荐、国际组织官网自行申请；申请周期长达半年到1年，需提前1-2年规划；关注人社部国际组织人才信息服务网的招考推送。',
    '英联邦方向（英港新澳）': '留学英联邦方向（英国/中国香港/新加坡/澳洲，以一年制授课型硕士为主）。围绕：均分GPA是核心，大一到大三保持高分；雅思6.5+尽早考出（最好大三前）；大三上定校定专业（冲刺/匹配/保底分档）；大三暑假完成文书与推荐信；大四上9-11月开放申请即尽早递交（滚动录取先到先得）；11月-次年2月拿有条件offer；大四下满足均分与语言条件换无条件offer、办理签证。',
    '北美方向（美加）': '留学北美方向（美国/加拿大）。围绕：GPA3.0+、托福100或雅思7.0、GRE/GMAT最好大三考完；大二大三积累科研（进实验室、发文）与实习，申请博士可提前套磁导师；大三暑假确定选校清单、完成SOP文书与推荐信网申；大四上12月-次年1月多数硕士项目截止（博士多在12月15日前后）；大四下2-4月收offer、办理I-20与存款证明、5-6月F-1签证。',
    '欧亚方向（欧日韩）': '留学欧亚方向（欧陆/日本/韩国）。围绕：欧陆（德国/法国等）重本科课程匹配度，德国需通过APS审核，多数公立免学费但要求德语/法语成绩，语言需尽早起步；日本路径：语言学校或研究生（预科）→修士考试，需N1/N2+研究计划书并提前联系导师；韩国需Topik4级以上；各国各校申请时间差异大，需提前查阅官网、大四上递交。',
}


@app.route('/api/plan_detail', methods=['GET'])
def plan_detail_api():
    """发展规划（图四）：读研/政策性就业/出国 → 分学年时间准备规划"""
    try:
        major = (request.args.get('major') or '').strip()
        item = (request.args.get('item') or '').strip()
        if not major or not item:
            return jsonify({'error': '缺少major或item参数'}), 400
        note = _PLAN_ITEM_NOTES.get(item)
        if not note:
            return jsonify({'error': f'未知规划路径: {item}'}), 400
        note = note + _policy_facts_for(item)
        data = _career_llm_json(
            _PLAN_SYSTEM,
            f'学生专业：{major}\n规划路径：{item}\n路径要点：{note}\n标题：{major}专业{item}时间准备',
            _career_cache_path('plan', f'{major}|{item}'),
            refresh=request.args.get('refresh') == '1')
        if not data:
            return jsonify({'error': 'LLM生成规划失败，请稍后重试'}), 500
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'规划生成失败: {str(e)}'}), 500


@app.route('/api/policy_hits', methods=['GET'])
def policy_hits_api():
    """发展规划路线 → 本校升学政策库命中的概要资料（L1/L2，本地JSON即时返回）"""
    item = (request.args.get('item') or '').strip()
    q = (request.args.get('q') or '').strip()
    if not item and not q:
        return jsonify({'success': True, 'hits': []})
    words = [w for w in re.split(r'[,，;；\s]+', q) if w]
    if item and item not in words:
        words.insert(0, item)
    policy = _UPGRADE_KB.get('policy')
    hits, seen = [], set()
    for w in words:
        w_n = _norm_text(w)
        if not w_n or not policy:
            continue
        for e in policy.get('entries', []):
            eid = str(e.get('id') or e.get('title') or '')
            if not eid or eid in seen:
                continue
            title_n = _norm_text(e.get('title', ''))
            matched = bool(w_n in title_n or title_n in w_n)
            if not matched:
                for kw in (e.get('keywords') or []):
                    kw_n = _norm_text(kw)
                    if kw_n and (kw_n in w_n or w_n in kw_n):
                        matched = True
                        break
            if not matched:
                continue
            seen.add(eid)
            hits.append({
                'id': eid,
                'title': e.get('title', ''),
                'layer': e.get('layer', ''),
                'summary': e.get('summary', ''),
                'facts': (e.get('facts') or [])[:6],
                'url': e.get('url') or ''
            })
    hits = hits[:5]
    return jsonify({'success': True, 'item': item, 'hits': hits})


# ============ 智能分析：点击课程/竞赛/技能/岗位 → 知识搜索(预留) + 大模型介绍 ============

SMART_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cache', 'smart')
os.makedirs(SMART_CACHE_DIR, exist_ok=True)

_SMART_TYPE_LABELS = {'course': '课程', 'comp': '竞赛', 'skill': '技能', 'job': '岗位'}

_SMART_SYSTEM = """你是一名资深的高校学业与职业发展顾问。针对给定的条目（课程/竞赛/技能/岗位之一）和所属专业背景，生成简明实用的智能分析。
要求：
1. intro：2-3句话介绍该条目的内容与作用（课程=教什么/在培养方案中的位置；竞赛=比什么/含金量；技能=是什么/应用场景；岗位=做什么/发展空间），不空谈。
2. difficulty：难度评价，格式"★★★☆☆（一句话说明）"，星级1-5，说明不超过30字。
3. prereq：先修基础数组，1-4条短句（每条不超过20字）；无先修要求时为空数组。
4. advice：建议数组，2-4条（每条20-50字），课程给学习建议、竞赛给备赛建议、技能给掌握路径、岗位给准备建议；具体可执行，禁止"努力学习""提升自己"式空话。
5. 内容贴合专业背景，真实可信；不得编造具体网址、电话、政策文件号。
6. 只输出JSON，不要任何解释文字或markdown围栏。格式：
{"name":"条目名","intro":"...","difficulty":"★★★☆☆（...）","prereq":["..."],"advice":["..."]}"""


# ============ 升学政策知识库（L1/L2本地数据，大模型调用时优先采用） ============

UPGRADE_KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'raw', '升学政策库')


def _load_upgrade_kb():
    """加载本地升学资料库（data/raw/升学政策库/*.json）：L1官方政策/L2多源整理，优先于大模型通用知识"""
    kb = {'bonus': None, 'policy': None}
    for key, fname in (('bonus', '竞赛加分名单.json'), ('policy', '升学政策_天津大学.json')):
        try:
            with open(os.path.join(UPGRADE_KB_DIR, fname), 'r', encoding='utf-8') as f:
                kb[key] = json.load(f)
        except Exception:
            kb[key] = None
    return kb


_UPGRADE_KB = _load_upgrade_kb()


def _norm_text(s):
    """条目名归一化：小写、全角转半角、去空白与引号装饰符，便于模糊匹配"""
    s = str(s or '').strip().lower()
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            ch = ' '
        elif 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        out.append(ch)
    s = ''.join(out)
    for ch in ' \t\r\n"\'“”‘’「」《》·、，':
        s = s.replace(ch, '')
    return s


def _match_bonus_comps(name):
    """按名称模糊匹配学院推免加分竞赛名单（正名+别名双向包含）"""
    bonus = _UPGRADE_KB.get('bonus')
    if not bonus:
        return []
    node = _norm_text(name)
    if not node:
        return []
    hits = []
    for c in bonus.get('competitions', []):
        cands = [_norm_text(c.get('name', ''))] + [_norm_text(a) for a in (c.get('aliases') or [])]
        for cd in cands:
            if cd and (cd in node or node in cd):
                hits.append(c)
                break
    return hits


def _match_policy_entries(name):
    """按条目名匹配升学政策库条目（关键词双向包含）"""
    policy = _UPGRADE_KB.get('policy')
    hits = []
    if not policy:
        return hits
    node = _norm_text(name)
    if not node:
        return hits
    for e in policy.get('entries', []):
        for kw in (e.get('keywords') or []):
            kw_n = _norm_text(kw)
            if kw_n and (kw_n in node or node in kw_n):
                hits.append(e)
                break
    return hits


def _entry_facts_text(e):
    facts = e.get('facts') or []
    if facts:
        return '；'.join(str(x) for x in facts)
    return str(e.get('summary') or '')


def _bonus_rules_text():
    bonus = _UPGRADE_KB.get('bonus') or {}
    rules = bonus.get('rules') or {}
    return '；'.join(filter(None, [rules.get('subject_cap'), rules.get('level_weights'),
                                   rules.get('team_rule'), rules.get('regional_rule')])) or ''


def gather_knowledge_hits(name, item_type):
    """本地L1/L2知识源检索：竞赛查学院推免加分名单，任意条目名可命中升学政策库；
    命中结果作为参考资料并入大模型分析（prompt中"优先采用其中事实"）并回传前端展示。
    后续可继续在此追加课程评价网址、学校数据等知识源，返回 [{title, url, snippet}] 列表"""
    hits = []
    try:
        bonus = _UPGRADE_KB.get('bonus') or {}
        meta = bonus.get('meta') or {}
        src_url = ((meta.get('source_urls') or [''])[0]) or '#'
        college = meta.get('college') or '本学院'
        year = meta.get('year') or ''
        if item_type == 'comp':
            matched = _match_bonus_comps(name)
            if matched:
                names = '、'.join(str(c.get('name') or '').strip() for c in matched[:3])
                hits.append({
                    'title': f'✓ 在推免加分名单内（{college}{year}年细则）',
                    'url': src_url,
                    'snippet': f'「{name}」匹配名单内竞赛：{names}。{_bonus_rules_text()}'
                })
            else:
                hits.append({
                    'title': f'— 暂不在推免加分名单内（{college}{year}年细则）',
                    'url': src_url,
                    'snippet': f'「{name}」未出现在{college}推免优秀加分的学生课外科技竞赛项目列表（共20项）中；文体竞赛按细则另行认定。加分以学院当年最新细则与鉴定答辩结果为准。'
                })
        for e in _match_policy_entries(name)[:3]:
            hits.append({
                'title': f"{e.get('title', '')}（天大升学政策{e.get('layer', '')}）",
                'url': e.get('url') or '#',
                'snippet': _entry_facts_text(e)
            })
    except Exception:
        pass
    return hits[:5]


def _policy_facts_for(item):
    """发展规划路径注入本地升学政策事实（L1/L2数据优先于模型通用知识）"""
    if item != '保研':
        return ''
    policy = _UPGRADE_KB.get('policy')
    if not policy:
        return ''
    lines = []
    for e in policy.get('entries', []):
        if e.get('id') == 'tuimian_prepare':
            continue
        body = _entry_facts_text(e)
        if body:
            lines.append(f"- {e.get('title', '')}：{body}")
    if not lines:
        return ''
    return '\n天津大学升学政策本地资料（优先采用其中事实，规划内容不得与之矛盾）：\n' + '\n'.join(lines)


@app.route('/api/tuimian_bonus', methods=['GET'])
def tuimian_bonus_api():
    """查询竞赛是否在学院推免加分名单内（本地L1数据，供前端徽章即时显示）"""
    try:
        name = (request.args.get('name') or '').strip()
        if not name:
            return jsonify({'error': '缺少name参数'}), 400
        matched = _match_bonus_comps(name)
        bonus = _UPGRADE_KB.get('bonus') or {}
        meta = bonus.get('meta') or {}
        return jsonify({
            'success': True,
            'in_list': bool(matched),
            'matched': [str(c.get('name') or '').strip() for c in matched[:5]],
            'rules': bonus.get('rules') or {},
            'college': meta.get('college') or '',
            'year': meta.get('year') or '',
            'source_urls': meta.get('source_urls') or []
        })
    except Exception as e:
        return jsonify({'error': f'查询失败: {str(e)}'}), 500


@app.route('/api/smart_analysis', methods=['GET'])
def smart_analysis():
    """点击报告中的课程/竞赛/技能/岗位：知识搜索(预留) + 大模型生成介绍与建议，磁盘缓存"""
    try:
        item_type = (request.args.get('type') or '').strip()
        name = (request.args.get('name') or '').strip()
        major = (request.args.get('major') or '').strip() or '本专业'
        if item_type not in _SMART_TYPE_LABELS or not name:
            return jsonify({'error': '缺少type或name参数'}), 400
        cache_path = os.path.join(
            SMART_CACHE_DIR, f'{item_type}_' + re.sub(r'[^\w]+', '_', f'{major}|{name}').strip('_')[:100] + '.json')
        if request.args.get('refresh') != '1':
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if cached:
                    return jsonify({'success': True, 'data': cached})
            except Exception:
                pass
        hits = gather_knowledge_hits(name, item_type)
        ref_block = ''
        if hits:
            ref_lines = [f"- {h.get('title', '')}：{h.get('snippet', '')}（{h.get('url', '')}）" for h in hits[:5]]
            ref_block = '\n知识搜索参考资料（优先采用其中事实）：\n' + '\n'.join(ref_lines)
        type_label = _SMART_TYPE_LABELS[item_type]
        data = _career_llm_json(
            _SMART_SYSTEM,
            f'条目类型：{type_label}\n条目名称：{name}\n学生专业：{major}{ref_block}',
            cache_path,
            refresh=request.args.get('refresh') == '1')
        if not data:
            return jsonify({'error': '智能分析生成失败，请稍后重试'}), 500
        data['type'] = item_type
        data['type_label'] = type_label
        data['knowledge_hits'] = hits
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'智能分析失败: {str(e)}'}), 500


@app.route('/api/import_url', methods=['POST'])
def import_url():
    """通道A：抓取网页并抽取六表数据（返回预览，不落盘）"""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url.lower().startswith(('http://', 'https://')):
        return jsonify({'error': '请输入以 http(s):// 开头的网址'}), 400
    try:
        page_title, text = _fetch_url_text(url)
    except Exception as e:
        return jsonify({'error': f'网页抓取失败: {e}'}), 400
    if len(text) < 100:
        return jsonify({'error': '网页正文过短，可能由JavaScript动态渲染。请复制网页正文后改用"粘贴文本"通道导入'}), 400
    truncated = len(text) > WEB_EXTRACT_MAX_CHARS
    if truncated:
        text = text[:WEB_EXTRACT_MAX_CHARS]
    doc_type = (data.get('doc_type') or 'auto').strip()
    extracted = _run_web_extract(page_title or url, text, url, doc_type)
    if extracted is None:
        return jsonify({'error': 'LLM抽取失败，请稍后重试或改用粘贴文本通道'}), 502
    return jsonify({
        'success': True,
        'title': extracted['title'],
        'source_label': url,
        'truncated': truncated,
        'doc_type': extracted['doc_type'],
        'doc_type_label': extracted['doc_type_label'],
        'tables': extracted['tables']
    })


@app.route('/api/import_text', methods=['POST'])
def import_text():
    """通道B：粘贴文本抽取六表数据（返回预览，不落盘）"""
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if len(text) < 30:
        return jsonify({'error': '粘贴内容过短（至少30字）'}), 400
    title = (data.get('title') or '').strip() or '粘贴材料'
    truncated = len(text) > WEB_EXTRACT_MAX_CHARS
    if truncated:
        text = text[:WEB_EXTRACT_MAX_CHARS]
    doc_type = (data.get('doc_type') or 'auto').strip()
    extracted = _run_web_extract(title, text, '粘贴文本', doc_type)
    if extracted is None:
        return jsonify({'error': 'LLM抽取失败，请稍后重试'}), 502
    return jsonify({
        'success': True,
        'title': extracted['title'],
        'source_label': '粘贴文本',
        'truncated': truncated,
        'doc_type': extracted['doc_type'],
        'doc_type_label': extracted['doc_type_label'],
        'tables': extracted['tables']
    })


def _ocr_image_to_text(image_path):
    """RapidOCR 识别图片文字：按行聚类（y中心相近归一行，行内按x排序），输出近似版面文本供LLM抽取"""
    ocr = _get_ocr()
    result, _ = ocr(image_path)
    if not result:
        return ''
    items = []
    heights = []
    for box, text, _score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        heights.append(max(ys) - min(ys))
        items.append((min(xs), (min(ys) + max(ys)) / 2.0, str(text)))
    items.sort(key=lambda i: i[1])
    tol = max((max(heights) if heights else 12.0) * 0.6, 8.0)
    lines = []
    cur, cur_y = [], None
    for x, y, text in items:
        if cur_y is None or abs(y - cur_y) <= tol:
            cur.append((x, text))
            cur_y = y if cur_y is None else (cur_y + y) / 2.0
        else:
            cur.sort(key=lambda i: i[0])
            lines.append('  '.join(t for _, t in cur))
            cur, cur_y = [(x, text)], y
    if cur:
        cur.sort(key=lambda i: i[0])
        lines.append('  '.join(t for _, t in cur))
    return '\n'.join(lines)


@app.route('/api/import_image', methods=['POST'])
def import_image():
    """通道C：上传图片（培养方案/招聘公告等截图），本地OCR转文字后走同一LLM抽取管线（返回预览，不落盘）；
    原图暂存临时目录，确认导入时按标题拷入文件夹供数据溯源"""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '未收到图片文件'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.bmp', '.webp'):
        return jsonify({'error': '仅支持 png/jpg/jpeg/bmp/webp 图片'}), 400
    title = (request.form.get('title') or '').strip() or \
        os.path.splitext(os.path.basename(f.filename))[0] or '图片材料'
    doc_type = (request.form.get('doc_type') or 'auto').strip()

    # 清理超时残留（>24h）的暂存原图
    now = time.time()
    for tk in list(_PENDING_IMAGES.keys()):
        if now - _PENDING_IMAGES[tk].get('ts', 0) > 86400:
            old = _PENDING_IMAGES.pop(tk)
            try:
                if os.path.exists(old['path']):
                    os.remove(old['path'])
            except OSError:
                pass

    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    token = uuid.uuid4().hex
    img_path = os.path.join(tmp_dir, f'{token}{ext}')
    f.save(img_path)

    try:
        try:
            ocr_text = _ocr_image_to_text(img_path)
        except Exception as e:
            traceback.print_exc()
            try:
                os.remove(img_path)
            except OSError:
                pass
            return jsonify({'error': f'OCR识别失败: {e}'}), 500
        if len(ocr_text.strip()) < 10:
            try:
                os.remove(img_path)
            except OSError:
                pass
            return jsonify({'error': '图片中识别到的文字过少，请更换更清晰的截图'}), 400
    except Exception:
        try:
            os.remove(img_path)
        except OSError:
            pass
        raise

    truncated = len(ocr_text) > WEB_EXTRACT_MAX_CHARS
    if truncated:
        ocr_text = ocr_text[:WEB_EXTRACT_MAX_CHARS]
    extracted = _run_web_extract(title, ocr_text, title, doc_type)
    if extracted is None:
        try:
            os.remove(img_path)
        except OSError:
            pass
        return jsonify({'error': 'LLM抽取失败，请稍后重试'}), 502

    _PENDING_IMAGES[token] = {'path': img_path, 'title': extracted['title'], 'ts': time.time()}
    return jsonify({
        'success': True,
        'title': extracted['title'],
        'source_label': extracted['title'],
        'channel': 'image',
        'image_token': token,
        'ocr_chars': len(ocr_text),
        'truncated': truncated,
        'doc_type': extracted['doc_type'],
        'doc_type_label': extracted['doc_type_label'],
        'tables': extracted['tables']
    })


@app.route('/api/import_confirm', methods=['POST'])
def import_confirm():
    """通道A/B/C确认落盘：写入"网页-<标题>"（图片通道"图片-<标题>"）文件夹的xlsx并复用导入链；
    登录用户默认写入个人资料区，勾选共享则写入公共资料库；图片通道将原图拷入文件夹供数据溯源"""
    data = request.json or {}
    tables = data.get('tables')
    if not isinstance(tables, dict) or not tables:
        return jsonify({'error': '无有效抽取数据'}), 400
    raw_title = (data.get('title') or '').strip()
    title = re.sub(r'[\\/:*?"<>|\r\n\t]', '', raw_title)[:24].strip() or '未命名网页'
    prepared = {}
    for dim_name, headers in WEB_TABLE_HEADERS.items():
        info = tables.get(dim_name) or {}
        rows = info.get('rows') if isinstance(info, dict) else None
        if not rows:
            continue
        clean_rows = []
        for row in rows:
            if not isinstance(row, list):
                continue
            row = [str(cell).strip() if cell is not None else '' for cell in row[:len(headers)]]
            row += [''] * (len(headers) - len(row))
            clean_rows.append(row)
        if clean_rows:
            prepared[dim_name] = clean_rows
    if not prepared:
        return jsonify({'error': '六个维度均无数据行，未创建文件夹'}), 400

    channel = (data.get('channel') or 'web').strip()
    prefix = '图片-' if channel == 'image' else '网页-'
    share = bool(data.get('share'))
    user = _current_user()
    base_dir = UPLOAD_DIR if (share or not user) else _user_upload_dir(user['id'])

    folder_name = f'{prefix}{title}'
    folder_path = os.path.join(base_dir, folder_name)
    n = 2
    while os.path.exists(folder_path):
        folder_name = f'{prefix}{title}-{n}'
        folder_path = os.path.join(base_dir, folder_name)
        n += 1
    os.makedirs(folder_path, exist_ok=True)

    # 图片通道：把待确认原图按材料标题重命名拷入文件夹（数据溯源按来源名匹配文件）
    image_token = (data.get('image_token') or '').strip()
    if image_token:
        entry = _PENDING_IMAGES.pop(image_token, None)
        if entry and os.path.exists(entry.get('path', '')):
            src_img = entry['path']
            ext = os.path.splitext(src_img)[1]
            safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', '', entry.get('title') or title)[:24] or '图片材料'
            try:
                shutil.copy(src_img, os.path.join(folder_path, f'{safe_name}{ext}'))
            finally:
                try:
                    os.remove(src_img)
                except OSError:
                    pass

    # 按文档类型确定来源层级：standard/curriculum→L1权威，其余→L2抽取
    doc_type = (data.get('doc_type') or 'auto').strip()
    source_level = db_module.DOC_TYPE_LEVELS.get(doc_type, 'L2')

    results = {}
    for dim_name, clean_rows in prepared.items():
        headers = WEB_TABLE_HEADERS[dim_name]
        filepath = os.path.join(folder_path, DIMENSION_FILES[dim_name])
        wb = Workbook()
        ws = wb.active
        ws.title = '数据表'
        ws.append(headers)
        for row in clean_rows:
            ws.append(row)
        note = wb.create_sheet('填写说明')
        note['A1'] = '本文件由网页导入通道生成（LLM抽取，经用户预览确认）'
        note['A2'] = f'材料标题: {raw_title}'
        note['A3'] = f'来源: {data.get("source_label", "")}'
        note['A4'] = f'文档类型: {data.get("doc_type_label", "")}'
        note['A5'] = f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        wb.active = 0
        wb.save(filepath)

        dim_key = DIMENSION_KEYS[dim_name]
        builder = DIMENSION_BUILDERS[dim_name]
        try:
            nodes, edges = builder(headers, clean_rows)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({'error': f'{dim_name}构建图谱失败: {e}', 'folder': folder_name, 'results': results}), 500
        # 打来源层级标签，供图谱分层着色与报告引用分级
        for node in nodes:
            node['source_level'] = source_level
        for edge in edges:
            edge['source_level'] = source_level
        db_path = db_module.get_db_path(folder_path, dim_key)
        db_module.init_db(db_path)
        db_module.import_graph_data(db_path, nodes, edges)
        global_db = db_module.get_global_db_path(dim_key)
        db_module.init_global_db(global_db)
        db_module.sync_folder_to_global(global_db, folder_name, nodes, edges)
        results[dim_name] = {'rows': len(clean_rows), 'nodes': len(nodes), 'edges': len(edges)}
    return jsonify({'success': True, 'folder': folder_name, 'results': results})


def _folder_sync_sort_key(name):
    """文件夹同步顺序：含年份的新培养方案排前面。
    学期类字段合并语义为"先到先得（已有值保留、新源仅回填空缺）"，
    因此最新培养方案必须最先同步才能以最新为准；无年份的旧来源排最后。"""
    m = re.search(r'20\d{2}', name)
    return (-(int(m.group()) if m else 0), name)


def auto_import_all():
    """启动时自动扫描所有文件夹，预导入到各自SQLite并同步到全局库"""
    # 临时数据白名单：只导入这些数据源文件夹（None表示全部导入）
    ACTIVE_SOURCE_WHITELIST = None
    # 启用聚焦示例：ACTIVE_SOURCE_WHITELIST = ['天大电自培养方案2025', '竞赛名录2026', '_跨文档推导']
    def should_import_source(folder_name):
        if ACTIVE_SOURCE_WHITELIST is None:
            return True
        return folder_name in ACTIVE_SOURCE_WHITELIST

    all_folders = []
    if os.path.exists(GRAPH_BASE_DIR):
        for name in os.listdir(GRAPH_BASE_DIR):
            path = os.path.join(GRAPH_BASE_DIR, name)
            if os.path.isdir(path) and should_import_source(name):
                all_folders.append((name, path))
    if os.path.exists(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if os.path.isdir(path) and should_import_source(name):
                all_folders.append((name, path))

    if not all_folders:
        print('[auto-import] 未找到任何文件夹')
        return

    # 同步顺序：新培养方案（文件夹名含年份，年份大者先）→ 旧来源最后
    # 签名计算仍用原顺序，避免顺序调整引发一次无谓的全量重建
    sync_order = sorted(all_folders, key=lambda t: _folder_sync_sort_key(t[0]))

    print(f'[auto-import] 开始扫描 {len(all_folders)} 个文件夹...')
    for i, (folder_name, folder_path) in enumerate(sync_order, 1):
        for dim_name, dim_key in DIMENSION_KEYS.items():
            filename = DIMENSION_FILES[dim_name]
            filepath = os.path.join(folder_path, filename)
            if not os.path.exists(filepath):
                continue
            try:
                ensure_imported(folder_path, dim_key)
            except Exception as e:
                print(f'[auto-import] 文件夹 "{folder_name}" 维度 "{dim_name}" 导入失败: {e}')
        print(f'[auto-import] ({i}/{len(all_folders)}) {folder_name} 完成')

    # 重建所有维度的全局库：按来源文件mtime签名跳过未变化的维度，避免每次重启都清库重建
    for dim_name, dim_key in DIMENSION_KEYS.items():
        global_db = db_module.get_global_db_path(dim_key)
        try:
            db_module.init_global_db(global_db)
            filename = DIMENSION_FILES[dim_name]
            source_sig = db_module.compute_dim_signature(
                [(fn, os.path.join(fp, filename)) for fn, fp in all_folders]
            )
            if (source_sig
                    and db_module.get_meta(global_db, 'rebuild_sig') == source_sig
                    and db_module.count_nodes(global_db) > 0):
                print(f'[auto-import] {dim_name} 来源未变化，跳过全局库重建')
                continue
            db_module.rebuild_global_db(global_db)
            for folder_name, folder_path in sync_order:
                filepath = os.path.join(folder_path, filename)
                if not os.path.exists(filepath):
                    continue
                try:
                    db_path = db_module.get_db_path(folder_path, dim_key)
                    if db_module.is_imported(db_path):
                        nodes, edges = db_module.get_graph_data(db_path)
                        db_module.sync_folder_to_global(global_db, folder_name, nodes, edges)
                except Exception as e:
                    print(f'[auto-import] 全局库 "{dim_name}" 同步 "{folder_name}" 失败: {e}')
            db_module.set_meta(global_db, 'rebuild_sig', source_sig)
            print(f'[auto-import] {dim_name} 全局库重建完成: {db_module.count_nodes(global_db)} 节点')
        except Exception as e:
            print(f'[auto-import] {dim_name} 全局库重建失败: {e}')

    print(f'[auto-import] 全部完成：{len(all_folders)} 个文件夹')


# 启动时自动扫描导入（debug模式下仅在reloader子进程执行，避免父/子进程并发重建）
# 后台线程执行：服务立即监听端口（浏览器可秒开），数据导入/全局库重建在后台进行
app.debug = True
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
    threading.Thread(target=auto_import_all, daemon=True, name='auto-import').start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('APP_PORT', '6006')), debug=True, threaded=True)
