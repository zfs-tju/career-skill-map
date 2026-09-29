"""
知识图谱展示系统 - SQLite数据库模块
5个维度各一个独立db文件，互不干涉
"""

import os
import json
import time
import sqlite3
import threading
import hashlib

# 数据库文件根目录：每个文件夹下会创建 db_data/ 子目录存放6个db
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 6个维度英文key
DIMENSIONS = ['job_cluster', 'skill_structure', 'career_path', 'course_system', 'job_coopetition', 'competition_practice']

# ===== 数据来源层级（P0-3图谱分层着色依据）=====
# L1=权威骨架（职业标准/官方培养方案，权重1.0） L2=多源抽取（政策/行业报告/竞赛/招聘，权重0.6-0.9） L3=模型补全（AI建议，权重0.3）
SOURCE_LEVELS = {'L1': 3, 'L2': 2, 'L3': 1}
# 文档类型 -> 来源层级：职业标准与培养方案是官方权威，其余按多源抽取处理
DOC_TYPE_LEVELS = {'standard': 'L1', 'curriculum': 'L1', 'policy': 'L2', 'competition': 'L2', 'auto': 'L2'}


def higher_level(a, b):
    """合并同实体多来源时取更权威的层级（L1 > L2 > L3），空值按L2处理"""
    la = SOURCE_LEVELS.get(a or 'L2', 2)
    lb = SOURCE_LEVELS.get(b or 'L2', 2)
    top = max(la, lb)
    for lvl, v in SOURCE_LEVELS.items():
        if v == top:
            return lvl
    return 'L2'

# 线程锁，防止并发写入冲突
_db_locks = {}
_db_locks_guard = threading.Lock()


def _get_lock(db_path):
    """获取某个db文件的线程锁"""
    with _db_locks_guard:
        if db_path not in _db_locks:
            _db_locks[db_path] = threading.Lock()
        return _db_locks[db_path]



# 统一连接入口：等锁超时30秒，避免并发写时立刻抛 database is locked
DB_TIMEOUT = 30


def _connect(db_path):
    """带等锁超时的统一连接入口"""
    return sqlite3.connect(db_path, timeout=DB_TIMEOUT)


def get_meta(db_path, key):
    """读取meta表某个key的值，不存在返回None"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None


def set_meta(db_path, key, value):
    """写入meta表某个key的值"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()


def compute_dim_signature(folder_files):
    """根据各文件夹维度数据文件的mtime计算重建签名，无有效文件返回None"""
    parts = []
    for folder_name, filepath in folder_files:
        try:
            mt = os.path.getmtime(filepath)
        except OSError:
            continue
        parts.append('%s@%d' % (folder_name, int(mt)))
    if not parts:
        return None
    return hashlib.md5('|'.join(parts).encode('utf-8')).hexdigest()

def get_db_path(folder_path, dimension):
    """获取某个文件夹某个维度的db文件路径"""
    db_dir = os.path.join(folder_path, 'db_data')
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, f'{dimension}.db')


def _escape_id(node_id):
    """转义节点ID中的特殊字符（SQLite TEXT无需转义，但保险起见处理引号）"""
    if node_id is None:
        return ''
    return str(node_id).replace("'", "''")


def init_db(db_path):
    """初始化某个维度的数据库，创建表结构"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        # 节点表
        c.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL,
                category    TEXT,
                properties  TEXT
            )
        ''')
        # 边表
        c.execute('''
            CREATE TABLE IF NOT EXISTS edges (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT NOT NULL,
                target       TEXT NOT NULL,
                relation     TEXT,
                label        TEXT,
                support      TEXT,
                page         TEXT,
                product      TEXT,
                attrs        TEXT,
                source_level TEXT
            )
        ''')
        # 兼容旧表：补充 attrs / source_level 列
        c.execute("PRAGMA table_info(edges)")
        cols = [row[1] for row in c.fetchall()]
        if 'attrs' not in cols:
            c.execute("ALTER TABLE edges ADD COLUMN attrs TEXT")
        if 'source_level' not in cols:
            c.execute("ALTER TABLE edges ADD COLUMN source_level TEXT")
        c.execute("PRAGMA table_info(nodes)")
        cols = [row[1] for row in c.fetchall()]
        if 'source_level' not in cols:
            c.execute("ALTER TABLE nodes ADD COLUMN source_level TEXT")
        # 导入标记表，记录是否已从Excel导入
        c.execute('''
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()
        conn.close()


# ===== 全局汇总数据库 =====

# 全局库根目录（图谱聚合库与账号体系分置）
GLOBAL_DB_DIR = os.path.join(BASE_DIR, 'data', 'db', 'graphs')
ACCOUNT_DB_DIR = os.path.join(BASE_DIR, 'data', 'db', 'accounts')


def get_global_db_path(dimension):
    """获取某个维度的全局库路径"""
    os.makedirs(GLOBAL_DB_DIR, exist_ok=True)
    return os.path.join(GLOBAL_DB_DIR, f'{dimension}.db')


def init_global_db(db_path):
    """初始化全局库表结构"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                type           TEXT NOT NULL,
                category       TEXT,
                properties     TEXT,
                source_folders TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS edges (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source         TEXT NOT NULL,
                target         TEXT NOT NULL,
                relation       TEXT,
                label          TEXT,
                support        TEXT,
                page           TEXT,
                product        TEXT,
                attrs          TEXT,
                source_folders TEXT
            )
        ''')
        # 兼容旧表：如果存在 source_folder 列但不存在 source_folders，则重建
        c.execute("PRAGMA table_info(edges)")
        cols = [row[1] for row in c.fetchall()]
        if 'source_folder' in cols and 'source_folders' not in cols:
            c.execute('ALTER TABLE edges RENAME TO edges_old')
            c.execute('''
                CREATE TABLE edges (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    source         TEXT NOT NULL,
                    target         TEXT NOT NULL,
                    relation       TEXT,
                    label          TEXT,
                    support        TEXT,
                    page           TEXT,
                    product        TEXT,
                    attrs          TEXT,
                    source_folders TEXT
                )
            ''')
            c.execute('DROP TABLE edges_old')
            c.execute("PRAGMA table_info(edges)")
            cols = [row[1] for row in c.fetchall()]
        if 'attrs' not in cols:
            c.execute("ALTER TABLE edges ADD COLUMN attrs TEXT")
        # 兼容旧表：补充 source_level 列（P0-3来源层级）
        c.execute("PRAGMA table_info(nodes)")
        cols = [row[1] for row in c.fetchall()]
        if 'source_level' not in cols:
            c.execute("ALTER TABLE nodes ADD COLUMN source_level TEXT")
        c.execute("PRAGMA table_info(edges)")
        cols = [row[1] for row in c.fetchall()]
        if 'source_level' not in cols:
            c.execute("ALTER TABLE edges ADD COLUMN source_level TEXT")
        c.execute('''
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()
        conn.close()


def sync_folder_to_global(global_db_path, folder_name, nodes, edges):
    """将某个文件夹的数据同步到全局库
    - 同类型同名节点合并，source_folders 记录来源，属性取并集
    - 边去重后插入，带 source_folder 标记
    - 同名边（source+target+relation 相同）只保留一条
    """
    lock = _get_lock(global_db_path)
    with lock:
        conn = _connect(global_db_path)
        c = conn.cursor()

        # 先从所有边中移除该文件夹的旧贡献
        c.execute("SELECT id, support, page, source_folders FROM edges")
        old_edges = c.fetchall()
        for erow in old_edges:
            eid, esup, epage, esrc_folders_json = erow
            src_folders = []
            if esrc_folders_json:
                try:
                    src_folders = json.loads(esrc_folders_json)
                except json.JSONDecodeError:
                    pass
            if folder_name not in src_folders:
                continue
            supports = []
            if esup:
                try:
                    supports = json.loads(esup)
                except json.JSONDecodeError:
                    supports = [esup]
            pages = []
            if epage:
                try:
                    pages = json.loads(epage)
                except json.JSONDecodeError:
                    pages = [epage]
            idx = src_folders.index(folder_name)
            src_folders.pop(idx)
            if idx < len(supports):
                supports.pop(idx)
            if idx < len(pages):
                pages.pop(idx)
            if len(src_folders) == 0:
                c.execute("DELETE FROM edges WHERE id=?", (eid,))
            else:
                c.execute(
                    "UPDATE edges SET support=?, page=?, source_folders=? WHERE id=?",
                    (json.dumps(supports, ensure_ascii=False),
                     json.dumps(pages, ensure_ascii=False),
                     json.dumps(src_folders, ensure_ascii=False), eid)
                )

        # 合并节点
        for node in nodes:
            props = _normalize_properties(node.get('properties', {}))
            props_json = json.dumps(props, ensure_ascii=False)
            node_level = node.get('source_level') or 'L2'
            c.execute("SELECT properties, source_folders, source_level FROM nodes WHERE id=?", (node['id'],))
            row = c.fetchone()
            if row:
                # 合并属性
                existing_props = {}
                if row[0]:
                    try:
                        existing_props = json.loads(row[0])
                    except json.JSONDecodeError:
                        pass
                merged_props = _merge_properties(existing_props, props)
                # 合并 source_folders
                existing_folders = []
                if row[1]:
                    try:
                        existing_folders = json.loads(row[1])
                    except json.JSONDecodeError:
                        pass
                if folder_name not in existing_folders:
                    existing_folders.append(folder_name)
                # 来源层级取更权威者（L1 > L2 > L3）
                merged_level = higher_level(row[2], node_level)
                c.execute(
                    "UPDATE nodes SET properties=?, source_folders=?, source_level=? WHERE id=?",
                    (json.dumps(merged_props, ensure_ascii=False),
                     json.dumps(existing_folders, ensure_ascii=False), merged_level, node['id'])
                )
            else:
                c.execute(
                    "INSERT INTO nodes (id, name, type, category, properties, source_folders, source_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        node['id'], node['name'], node['type'],
                        node.get('category', node['type']),
                        props_json,
                        json.dumps([folder_name], ensure_ascii=False),
                        node_level
                    )
                )

        # 合并边：跨文件夹相同边(source+target+relation)合并为一条
        # support/page/source_folders 合并为JSON数组
        for edge in edges:
            esrc = edge['source']
            etgt = edge['target']
            erel = edge.get('relation', '')
            esup = edge.get('support', '')
            epage = edge.get('page', '')
            eprod = edge.get('product', '')
            edge_level = edge.get('source_level') or 'L2'
            c.execute(
                "SELECT id, support, page, product, source_folders, source_level FROM edges WHERE source=? AND target=? AND relation=?",
                (esrc, etgt, erel)
            )
            erow = c.fetchone()
            if erow:
                eid, existing_sup, existing_page, existing_prod, existing_folders_json, existing_level = erow
                supports = []
                if existing_sup:
                    try:
                        supports = json.loads(existing_sup)
                    except json.JSONDecodeError:
                        supports = [existing_sup]
                supports.append(esup)
                pages = []
                if existing_page:
                    try:
                        pages = json.loads(existing_page)
                    except json.JSONDecodeError:
                        pages = [existing_page]
                pages.append(epage)
                src_folders = []
                if existing_folders_json:
                    try:
                        src_folders = json.loads(existing_folders_json)
                    except json.JSONDecodeError:
                        pass
                if folder_name not in src_folders:
                    src_folders.append(folder_name)
                merged_prod = existing_prod or ''
                if eprod and eprod != merged_prod:
                    if merged_prod:
                        prod_list = [p for p in [merged_prod, eprod] if p]
                        merged_prod = json.dumps(list(set(prod_list)), ensure_ascii=False)
                    else:
                        merged_prod = eprod
                merged_level = higher_level(existing_level, edge_level)
                c.execute(
                    "UPDATE edges SET support=?, page=?, product=?, source_folders=?, source_level=? WHERE id=?",
                    (json.dumps(supports, ensure_ascii=False),
                     json.dumps(pages, ensure_ascii=False),
                     merged_prod,
                     json.dumps(src_folders, ensure_ascii=False), merged_level, eid)
                )
            else:
                c.execute(
                    "INSERT INTO edges (source, target, relation, label, support, page, product, attrs, source_folders, source_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        esrc, etgt, erel,
                        edge.get('label', erel),
                        json.dumps([esup], ensure_ascii=False),
                        json.dumps([epage], ensure_ascii=False),
                        eprod,
                        json.dumps(edge['attrs'], ensure_ascii=False) if edge.get('attrs') else None,
                        json.dumps([folder_name], ensure_ascii=False),
                        edge_level
                    )
                )

        conn.commit()
        conn.close()


SEMESTER_KEYS = ('开课学年', '开课学期')
_YEAR_NUM_MAP = {'1': '大一', '2': '大二', '3': '大三', '4': '大四'}


def _norm_semester_value(key, v):
    """学期类字段规范化：列表/数字/逗号串 → 规范逗号串，剔除非法值（防列表污染）
    - 开课学年：'1'-'4' → 大一-大四；同义对（如 ['大三','3']）归并取中文词（2025版为准）
    - 开课学期：合法值 秋/春/夏/短学期/全部（数字学期丢弃，与 _r10_fix_semester 一致）
    """
    if v is None:
        return ''
    raw_items = v if isinstance(v, list) else str(v).split(',')
    out = []
    if key == '开课学年':
        cn, num = [], []
        for it in raw_items:
            s = str(it).strip()
            if s in ('大一', '大二', '大三', '大四', '全部'):
                cn.append(s)
            elif s in _YEAR_NUM_MAP:
                num.append(_YEAR_NUM_MAP[s])
        out = cn or num
    else:
        for it in raw_items:
            s = str(it).strip()
            if s in ('秋', '春', '夏', '短学期', '全部') and s not in out:
                out.append(s)
    return ','.join(dict.fromkeys(out))


def _normalize_properties(props):
    """入库前统一规范化：学期类字段强制走 _norm_semester_value，
    杜绝数字学年/列表污染绕过 _merge_properties 直写库（INSERT/add_node/update_node 路径）"""
    if not isinstance(props, dict):
        return props
    out = dict(props)
    for k in SEMESTER_KEYS:
        if k in out:
            out[k] = _norm_semester_value(k, out[k])
    return out


def _merge_properties(existing, new):
    """合并两个属性字典
    - 学期类字段（开课学年/开课学期）：规范化为逗号串；已有值保留、新源仅回填空缺
      （多培养方案冲突由 _r10_fix_semester.py 按"最新方案优先"统一仲裁，
      此处禁止覆盖已仲裁值，也禁止再产出 Python 列表污染）
    - 列表类型（如"产品"）取并集
    - 相同key不同值：合并为列表去重
    - 相同key相同值：保持不变
    """
    merged = dict(existing)
    for k, v in new.items():
        if k in SEMESTER_KEYS:
            old_v = _norm_semester_value(k, merged.get(k))
            new_v = _norm_semester_value(k, v)
            merged[k] = old_v or new_v
            continue
        if k not in merged or merged[k] is None or merged[k] == '':
            merged[k] = v
        else:
            old_val = merged[k]
            if isinstance(old_val, list) or isinstance(v, list):
                # 列表合并去重
                old_list = old_val if isinstance(old_val, list) else [old_val]
                new_list = v if isinstance(v, list) else [v]
                for item in new_list:
                    if item not in old_list:
                        old_list.append(item)
                merged[k] = old_list
            elif old_val == v:
                # 值相同，不变
                pass
            else:
                # 值不同，合并为列表去重
                merged[k] = [old_val, v]
    # 自愈：旧库残留的学期列表污染，即使新源未触碰也一并规范化
    for sk in SEMESTER_KEYS:
        if sk in merged:
            merged[sk] = _norm_semester_value(sk, merged[sk])
    return merged


def get_global_graph_data(db_path):
    """从全局库读取全部节点和边"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # 自愈兜底：旧库缺 source_level 列时先补齐，避免按名访问崩溃
        c.execute("PRAGMA table_info(edges)")
        ecols = [row[1] for row in c.fetchall()]
        if 'source_level' not in ecols:
            c.execute("ALTER TABLE edges ADD COLUMN source_level TEXT")
            conn.commit()
        c.execute("PRAGMA table_info(nodes)")
        ncols = [row[1] for row in c.fetchall()]
        if 'source_level' not in ncols:
            c.execute("ALTER TABLE nodes ADD COLUMN source_level TEXT")
            conn.commit()
        c.execute('SELECT * FROM nodes')
        nodes = []
        for row in c.fetchall():
            props = {}
            if row['properties']:
                try:
                    props = json.loads(row['properties'])
                except json.JSONDecodeError:
                    props = {}
            source_folders = []
            if row['source_folders']:
                try:
                    source_folders = json.loads(row['source_folders'])
                except json.JSONDecodeError:
                    pass
            nodes.append({
                'id': row['id'],
                'name': row['name'],
                'type': row['type'],
                'category': row['category'] or row['type'],
                'properties': props,
                'source_folders': source_folders,
                'source_level': row['source_level'] or 'L2'
            })
        c.execute('SELECT * FROM edges ORDER BY id')
        edges = []
        for row in c.fetchall():
            # support/page/source_folders 是 JSON 数组
            supports = []
            if row['support']:
                try:
                    supports = json.loads(row['support'])
                except json.JSONDecodeError:
                    supports = [row['support']]
            pages = []
            if row['page']:
                try:
                    pages = json.loads(row['page'])
                except json.JSONDecodeError:
                    pages = [row['page']]
            src_folders = []
            if row['source_folders']:
                try:
                    src_folders = json.loads(row['source_folders'])
                except json.JSONDecodeError:
                    pass
            edges.append({
                'id': row['id'],
                'source': row['source'],
                'target': row['target'],
                'relation': row['relation'] or '',
                'label': row['label'] or row['relation'] or '',
                'support': supports,
                'page': pages,
                'product': row['product'] or '',
                'attrs': _parse_attrs(row['attrs']),
                'source_folders': src_folders,
                'source_level': row['source_level'] or 'L2'
            })
        conn.close()
        return nodes, edges


def rebuild_global_db(global_db_path):
    """清空全局库所有数据"""
    lock = _get_lock(global_db_path)
    with lock:
        conn = _connect(global_db_path)
        c = conn.cursor()
        c.execute('DELETE FROM nodes')
        c.execute('DELETE FROM edges')
        c.execute('DELETE FROM meta')
        conn.commit()
        conn.close()


def count_nodes(db_path):
    """统计该库节点数量（懒导入空数据防护用）"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM nodes')
        n = c.fetchone()[0]
        conn.close()
        return n


def is_imported(db_path):
    """检查该维度是否已从Excel导入"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM meta WHERE key='imported'")
        row = c.fetchone()
        conn.close()
        return row is not None and row[0] == '1'


def mark_imported(db_path):
    """标记该维度已导入"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('imported', '1')")
        conn.commit()
        conn.close()


def reset_db(db_path):
    """清空某维度数据库的所有数据（重新导入前调用）"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute('DELETE FROM nodes')
        c.execute('DELETE FROM edges')
        c.execute('DELETE FROM meta')
        conn.commit()
        conn.close()


def import_graph_data(db_path, nodes, edges):
    """将构建好的图谱数据导入数据库（覆盖式导入）"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        # 先清空
        c.execute('DELETE FROM nodes')
        c.execute('DELETE FROM edges')
        c.execute('DELETE FROM meta')
        # 写入节点
        for node in nodes:
            props = node.get('properties', {})
            c.execute(
                "INSERT OR REPLACE INTO nodes (id, name, type, category, properties, source_level) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    node['id'],
                    node['name'],
                    node['type'],
                    node.get('category', node['type']),
                    json.dumps(props, ensure_ascii=False),
                    node.get('source_level') or 'L2'
                )
            )
        # 写入边
        for edge in edges:
            c.execute(
                "INSERT INTO edges (source, target, relation, label, support, page, product, attrs, source_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    edge['source'],
                    edge['target'],
                    edge.get('relation', ''),
                    edge.get('label', edge.get('relation', '')),
                    edge.get('support', ''),
                    edge.get('page', ''),
                    edge.get('product', ''),
                    json.dumps(edge['attrs'], ensure_ascii=False) if edge.get('attrs') else None,
                    edge.get('source_level') or 'L2'
                )
            )
        # 标记已导入
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('imported', '1')")
        conn.commit()
        conn.close()


def get_graph_data(db_path):
    """从数据库读取全部节点和边"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # 自愈兜底：旧库缺 source_level 列时先补齐，避免按名访问崩溃
        c.execute("PRAGMA table_info(edges)")
        ecols = [row[1] for row in c.fetchall()]
        if 'source_level' not in ecols:
            c.execute("ALTER TABLE edges ADD COLUMN source_level TEXT")
            conn.commit()
        # 读节点
        c.execute('SELECT * FROM nodes')
        nodes = []
        for row in c.fetchall():
            props = {}
            if row['properties']:
                try:
                    props = json.loads(row['properties'])
                except json.JSONDecodeError:
                    props = {}
            nodes.append({
                'id': row['id'],
                'name': row['name'],
                'type': row['type'],
                'category': row['category'] or row['type'],
                'properties': props,
                'source_level': row['source_level'] or 'L2'
            })
        # 读边
        c.execute('SELECT * FROM edges ORDER BY id')
        edges = []
        for row in c.fetchall():
            edges.append({
                'id': row['id'],
                'source': row['source'],
                'target': row['target'],
                'relation': row['relation'] or '',
                'label': row['label'] or row['relation'] or '',
                'support': row['support'] or '',
                'page': row['page'] or '',
                'product': row['product'] or '',
                'attrs': _parse_attrs(row['attrs']),
                'source_level': row['source_level'] or 'L2'
            })
        conn.close()
        return nodes, edges


# ===== 节点CRUD =====

def add_node(db_path, node):
    """新增节点，返回写入后的节点"""
    lock = _get_lock(db_path)
    props = _normalize_properties(node.get('properties', {}))
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO nodes (id, name, type, category, properties, source_level) VALUES (?, ?, ?, ?, ?, ?)",
            (
                node['id'],
                node['name'],
                node['type'],
                node.get('category', node['type']),
                json.dumps(props, ensure_ascii=False),
                node.get('source_level') or 'L2'
            )
        )
        conn.commit()
        conn.close()
    return node


def update_node(db_path, node_id, node):
    """更新节点"""
    lock = _get_lock(db_path)
    props = _normalize_properties(node.get('properties', {}))
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute(
            "UPDATE nodes SET name=?, type=?, category=?, properties=? WHERE id=?",
            (
                node['name'],
                node['type'],
                node.get('category', node['type']),
                json.dumps(props, ensure_ascii=False),
                node_id
            )
        )
        conn.commit()
        conn.close()


def delete_node(db_path, node_id):
    """删除节点及其关联的所有边"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("DELETE FROM edges WHERE source=? OR target=?", (node_id, node_id))
        c.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        conn.commit()
        conn.close()


# ===== 边CRUD =====

def _parse_attrs(attrs_json):
    """解析边attrs的JSON文本，失败返回空dict"""
    if not attrs_json:
        return {}
    try:
        val = json.loads(attrs_json)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def add_edge(db_path, edge):
    """新增边，返回含id的边"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO edges (source, target, relation, label, support, page, product, attrs, source_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge['source'],
                edge['target'],
                edge.get('relation', ''),
                edge.get('label', edge.get('relation', '')),
                edge.get('support', ''),
                edge.get('page', ''),
                edge.get('product', ''),
                json.dumps(edge['attrs'], ensure_ascii=False) if edge.get('attrs') else None,
                edge.get('source_level') or 'L2'
            )
        )
        edge_id = c.lastrowid
        conn.commit()
        conn.close()
    edge['id'] = edge_id
    return edge


def update_edge(db_path, edge_id, edge):
    """更新边"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute(
            "UPDATE edges SET source=?, target=?, relation=?, label=?, support=?, page=?, product=? WHERE id=?",
            (
                edge['source'],
                edge['target'],
                edge.get('relation', ''),
                edge.get('label', edge.get('relation', '')),
                edge.get('support', ''),
                edge.get('page', ''),
                edge.get('product', ''),
                edge_id
            )
        )
        conn.commit()
        conn.close()


def delete_edge(db_path, edge_id):
    """删除边"""
    lock = _get_lock(db_path)
    with lock:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("DELETE FROM edges WHERE id=?", (edge_id,))
        conn.commit()
        conn.close()


# ===== 用户画像（点亮已会技能/目标设定，r6b）=====
# 独立于六维度库，存账号数据目录：data/db/accounts/user_profile.db
PROFILE_DB_PATH = os.path.join(ACCOUNT_DB_DIR, 'user_profile.db')

# 画像白名单字段与默认值
PROFILE_DEFAULTS = {
    'grade': '',            # 当前年级：大一/大二/大三/大四
    'major': '',            # 专业
    'target_jobs': [],      # 意向岗位
    'target_cities': [],    # 意向城市
    'known_skills': [],     # 已点亮掌握的节点 [{id, name, type, marked_at}]
    'm4_layout': {}         # 个性培养布局：课程显示过滤 + 画布拖放块 {filters:[], canvas:{}}
}


def _open_profile_db(db_path):
    """打开画像库，表不存在则自建"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = _connect(db_path)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn, c


def get_user_profile(db_path=PROFILE_DB_PATH):
    """读取用户画像，缺失字段补默认值"""
    lock = _get_lock(db_path)
    with lock:
        conn, c = _open_profile_db(db_path)
        c.execute("SELECT key, value FROM profile")
        rows = c.fetchall()
        conn.close()
    profile = dict(PROFILE_DEFAULTS)
    for k, v in rows:
        if k not in PROFILE_DEFAULTS:
            continue
        try:
            parsed = json.loads(v)
            profile[k] = parsed if parsed is not None else PROFILE_DEFAULTS[k]
        except (ValueError, TypeError):
            profile[k] = PROFILE_DEFAULTS[k]
    # 保证列表类型字段是列表
    for list_key in ('target_jobs', 'target_cities', 'known_skills'):
        if not isinstance(profile[list_key], list):
            profile[list_key] = []
    return profile


def save_user_profile(profile, db_path=PROFILE_DB_PATH):
    """保存用户画像（仅接受白名单字段，整字段覆盖）"""
    lock = _get_lock(db_path)
    with lock:
        conn, c = _open_profile_db(db_path)
        for key in PROFILE_DEFAULTS:
            if key not in profile:
                continue
            c.execute(
                "INSERT OR REPLACE INTO profile (key, value) VALUES (?, ?)",
                (key, json.dumps(profile[key], ensure_ascii=False))
            )
        conn.commit()
        conn.close()
    return get_user_profile(db_path)


def toggle_known_skill(node_id, name, node_type, db_path=PROFILE_DB_PATH):
    """点亮/取消点亮一个节点（已会技能），返回 (是否已点亮, 最新画像)"""
    profile = get_user_profile(db_path)
    old_skills = profile.get('known_skills', [])
    kept = [s for s in old_skills if s.get('id') != node_id]
    if len(kept) < len(old_skills):
        now_known = False  # 原来已点亮 → 本次取消
    else:
        kept.append({
            'id': node_id,
            'name': name,
            'type': node_type,
            'marked_at': time.strftime('%Y-%m-%d %H:%M')
        })
        now_known = True
    profile['known_skills'] = kept
    save_user_profile(profile, db_path)
    return now_known, profile


# ===== 账号体系（用户名+密码登录，每账号独立个人数据，r13）=====
# 独立账号库：data/db/accounts/accounts.db（users表）；个人画像按用户分库：data/db/accounts/users/u<id>/user_profile.db
ACCOUNTS_DB_PATH = os.path.join(ACCOUNT_DB_DIR, 'accounts.db')
USERS_PROFILE_DIR = os.path.join(ACCOUNT_DB_DIR, 'users')


def _hash_password(password, salt):
    """PBKDF2加盐哈希（标准库实现，无需额外依赖）"""
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 120000).hex()


def _open_accounts_db():
    """打开账号库，表不存在则自建"""
    os.makedirs(os.path.dirname(ACCOUNTS_DB_PATH), exist_ok=True)
    conn = _connect(ACCOUNTS_DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT
    )""")
    conn.commit()
    return conn, c


def create_user(username, password):
    """注册新用户，成功返回 {'id','username'}；用户名已存在或参数非法返回 (None, 原因)"""
    username = (username or '').strip()
    password = password or ''
    if not (2 <= len(username) <= 20):
        return None, '用户名需2-20个字符'
    if len(password) < 6:
        return None, '密码至少6位'
    lock = _get_lock(ACCOUNTS_DB_PATH)
    with lock:
        conn, c = _open_accounts_db()
        c.execute("SELECT id FROM users WHERE username=?", (username,))
        if c.fetchone():
            conn.close()
            return None, '用户名已被注册'
        salt = os.urandom(16).hex()
        c.execute(
            "INSERT INTO users (username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (username, salt, _hash_password(password, salt), time.strftime('%Y-%m-%d %H:%M'))
        )
        uid = c.lastrowid
        conn.commit()
        conn.close()
    return {'id': uid, 'username': username}, None


def verify_user(username, password):
    """校验用户名密码，正确返回 {'id','username'}，错误返回 None"""
    username = (username or '').strip()
    lock = _get_lock(ACCOUNTS_DB_PATH)
    with lock:
        conn, c = _open_accounts_db()
        c.execute("SELECT id, username, salt, password_hash FROM users WHERE username=?", (username,))
        row = c.fetchone()
        conn.close()
    if not row:
        return None
    uid, name, salt, pwd_hash = row
    if _hash_password(password or '', salt) != pwd_hash:
        return None
    return {'id': uid, 'username': name}


def get_user_by_id(user_id):
    """按ID查用户（会话恢复用），不存在返回 None"""
    if not user_id:
        return None
    lock = _get_lock(ACCOUNTS_DB_PATH)
    with lock:
        conn, c = _open_accounts_db()
        c.execute("SELECT id, username FROM users WHERE id=?", (user_id,))
        row = c.fetchone()
        conn.close()
    return {'id': row[0], 'username': row[1]} if row else None


def get_profile_db_path(user_id):
    """按用户返回画像库路径：登录用户各自独立一库，未登录（游客）回退全局库"""
    if not user_id:
        return PROFILE_DB_PATH
    return os.path.join(USERS_PROFILE_DIR, f'u{user_id}', 'user_profile.db')
