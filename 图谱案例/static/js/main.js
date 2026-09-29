/**
 * 就业职业岗位技能图谱系统 - 主逻辑
 */

// ===== 全局状态 =====
let chart = null;
let currentDimension = 'job_cluster';
let graphData = { nodes: [], edges: [], headers: [], raw_data: [] };
let selectedNode = null;
let selectedEdge = null;
let nodeColors = {};
let layoutMode = 'hub'; // 'hub' 四大中心探索视图（默认）| 'force' 力导向全图（兜底）| 'timeline' 培养时序思维导图（仅课程体系维度）
let hubExpanded = { job: false, skill: false, course: false, comp: false }; // 各中心展开状态
let currentRenderLinks = []; // 当前渲染的边（hub模式下供hover点亮跨中心边）
let currentNodeCenter = {};  // 当前渲染节点id -> 中心key
let hubTotalMembers = { job: 0, skill: 0, course: 0, comp: 0 }; // 各中心成员总数
let hubVisibleNodes = 0; // hub视图当前实际展示的节点数（统计栏显示 已展示/总数）
let hubVisibleEdges = 0; // hub视图当前实际展示的边数
let currentEditProperties = [];
let viewScope = 'folder'; // 'folder' | 'global'

// 维度标题映射
const DIMENSION_TITLES = {
    'job_cluster': '专业岗位图谱',
    'skill_structure': '技能结构图谱',
    'course_system': '课程体系图谱',
    'competition_practice': '竞赛实践图谱'
};

// 维度 -> hub中心映射：维度模式=单中心聚焦（只显示该中心及其节点）
// 综合检索生成图谱时清空聚焦，恢复四中心全景
const DIMENSION_CENTER_MAP = {
    'job_cluster': 'job',
    'skill_structure': 'skill',
    'course_system': 'course',
    'competition_practice': 'comp'
};
let dimensionCenter = 'job'; // 当前聚焦中心（null=四中心全景），初始与默认维度 job_cluster 对应

// 节点类型图标映射
const NODE_ICONS = {
    '岗位': '岗', '技能': '技', '企业': '企', '城市': '城', '行业': '行',
    '岗位类别': '类', '技能分类': '分', '工具/平台': '工', '认证证书': '证',
    '职业方向': '向', '专业': '专', '课程': '课', '学院': '院', '院校': '校',
    '竞赛': '赛', '自定义': 'X'
};

// ===== 四大中心配置（四大中心辐射式图谱）=====
// 节点类型 -> 所属中心；未识别类型兜底归入技能中心（知识能力域）
const HUB_TYPE_MAP = {
    '岗位': 'job', '企业': 'job', '城市': 'job', '行业': 'job', '职业方向': 'job', '岗位类别': 'job',
    '技能': 'skill', '技能分类': 'skill', '工具/平台': 'skill', '认证证书': 'skill',
    '专业': 'course', '课程': 'course', '学院': 'course', '院校': 'course',
    '竞赛': 'comp'
};
const HUB_CENTERS = {
    job:     { name: '岗位中心', types: ['岗位', '企业', '行业', '职业方向', '岗位类别', '城市'] },
    skill:   { name: '技能中心', types: ['技能', '技能分类', '工具/平台', '认证证书'] },
    course:  { name: '课程中心', types: ['专业', '课程', '学院', '院校'] },
    comp:    { name: '竞赛中心', types: ['竞赛'] }
};
// 收起态每中心默认显示的核心成员数（按连接度取Top N：默认只呈现核心节点，信息按需触发）
const HUB_TOP_N = 14;

// 课程中心成员过滤：仅保留专业核心/专业选修性质的课程（体育/军事/思政/外语/数理基础/通识/公共实践类不进入课程中心）
// 优先依据库中打标的 properties['课程类别']（来源：培养方案目录 _r10_2024_courses.json + 名称规则兜底），
// 节点缺该属性时（如库重建后未重新打标）退回名称模式识别
const HUB_COURSE_HIDE_RE = new RegExp([
    '体育', '军事', '军训', '国防', '体操', '太极', '武术', '健美', '瑜伽', '游泳',
    '篮球', '足球', '排球', '网球', '羽毛球', '乒乓', '田径', '舞蹈',
    '思想道德', '近现代史', '马克思主义', '毛泽东', '中国特色', '习近平', '形势与政策', '党史', '新中国', '社会主义',
    '心理健康', '健康教育', '择业指导', '职业生涯', '就业指导', '创新创业', '创新实践', '大学生活', '安全教育', '国家安全', '法制', '法律基础',
    '诚信教育', '管理概论', '通识', '劳动', '美育',
    '大学物理', '物理实验', '高等数学', '微积分', '线性代数', '概率论', '复变函数', '数理方程', '数学物理',
    '大学化学', '化学实验', '大学语文', '工程训练', '金工', '制图', '工程图学',
    '大学英语', '英文写作', '英译', '跨文化传播', '西方文化', '(?<!专业)英语',
    '大学计算机基础', '计算机软件技术基础',
    '课程设计', '实习', '毕业设计', '毕业论文', '综合训练'
].join('|'));
function isPublicCourseNode(n) {
    if (!n || n.type !== '课程') return false;
    const cat = n.properties && n.properties['课程类别'];
    if (cat) return cat !== '专业核心' && cat !== '专业选修';
    // 兜底匹配前剥离括号后缀（如"（英语）/（全英文）/（双语）"是授课语言标注，不代表课程性质）
    const bare = String(n.name || '').replace(/[（(][^）)]*[）)]/g, '');
    return HUB_COURSE_HIDE_RE.test(bare);
}

// ===== 培养时序视图配置（P1-1：培养方案转时序思维导图）=====
// 树层级：培养时序(根) -> 学年 -> 学期 -> 课程叶子；默认仅展开到学年层，点击按需展开
const TIMELINE_YEAR_ORDER = ['大一', '大二', '大三', '大四', '全部', '未注明'];
const TIMELINE_YEAR_COLORS = {
    '大一': '#4ade80', '大二': '#38bdf8', '大三': '#fbbf24', '大四': '#f87171',
    '全部': '#a78bfa', '未注明': '#94a3b8'
};
const TIMELINE_SEM_ORDER = ['秋', '春', '夏', '短学期', '全部', '未注明'];
let timelineCourseCount = 0; // 时序视图当前收录的课程数
let timelineYearCount = 0;   // 时序视图当前收录的学年分组数

// 有向边关系白名单：这些关系显示箭头，其余（如技能重叠）无向
const DIRECTED_RELATIONS = ['招聘于', '位于', '属于', '要求', '使用', '可考取',
    '可替代', '晋升/转岗', '需补充技能', '培养', '胜任', '开设', '核心技能',
    '包含', '锻炼', '面向'];

// ===== 初始化 =====
document.addEventListener('DOMContentLoaded', async function() {
    initChart();
    await checkAuth();       // 恢复登录态（决定个人资料区可见性）
    await loadNodeColors();  // 先加载颜色配置，再加载图谱
    await loadFolders();     // loadFolders会自动选择文件夹并触发loadDimension
    // 如果loadFolders没有触发加载（无文件夹），则手动加载
    if (graphData.nodes.length === 0) {
        await loadDimension('job_cluster');
    }

    // 加载用户画像（点亮清单徽标）
    loadProfile();

    // 编辑关系弹窗：关系名称变化时显隐竞争/合作产品字段
    document.getElementById('editEdgeRelation').addEventListener('input', function(e) {
        updateEditEdgeProductField(e.target.value);
    });

    // 统一搜索：实体候选列表点击（事件委托）
    var entityList = document.getElementById('searchEntityList');
    entityList.addEventListener('click', function(e) {
        var item = e.target.closest('.catalog-entity-item');
        if (item && item.dataset.id) {
            selectSearchEntity(item.dataset.id, item.dataset.label);
        }
    });
    // 点击候选列表/叠加条件多选下拉外部时收起
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.catalog-entity-step')) {
            entityList.style.display = 'none';
        }
        if (!e.target.closest('.focus-multi')) {
            collapseAllFocusLists();
        }
    });

    // 维度菜单切换（事件绑定，修复菜单无响应问题）
    document.querySelectorAll('.menu-item').forEach(function(item) {
        item.addEventListener('click', function() {
            var dim = item.getAttribute('data-dimension');
            if (!dim) return;
            var center = DIMENSION_CENTER_MAP[dim] || null;
            // 同维度且已处于同一聚焦状态时忽略重复点击
            if (dim === currentDimension && dimensionCenter === center) return;
            setActiveMenu(dim);
            currentDimension = dim;
            dimensionCenter = center;
            loadDimension(dim, { fromMenu: true });
        });
    });
    // 初始不点亮任何维度（仅在用户点击菜单时高亮）

    // 统一搜索初始化（默认类型：专业，直接拉取目录联想）
    onSearchTypeChange();

    // 窗口大小变化（hub布局为像素坐标，resize后需重排）
    window.addEventListener('resize', function() {
        if (!chart) return;
        chart.resize();
        if (layoutMode === 'hub' && graphData.nodes.length > 0) renderGraph();
    });
});

// 维度菜单激活态
function setActiveMenu(dimension) {
    document.querySelectorAll('.menu-item').forEach(function(item) {
        item.classList.toggle('active', item.getAttribute('data-dimension') === dimension);
    });
}

function initChart() {
    const dom = document.getElementById('graphChart');
    chart = echarts.init(dom, null, { renderer: 'canvas' });

    chart.on('click', function(params) {
        if (params.dataType === 'node') {
            handleNodeClick(params.data);
        } else if (params.dataType === 'edge') {
            handleEdgeClick(params.data);
        }
    });

    // 悬停中心锚点/成员节点：动态淡入跨中心关联邻居（信息按需触发，防抖避免滑过闪烁）
    chart.on('mouseover', function(params) {
        if (params.dataType !== 'node' || layoutMode !== 'hub') return;
        if (params.data.isHub) {
            scheduleHubFocus(buildAnchorFocus(params.data.hubKey));
        } else if (params.data.nodeData) {
            scheduleHubFocus(buildNodeFocus(params.data.id));
        }
    });
    chart.on('mouseout', function(params) {
        if (params.dataType === 'node' && (params.data.isHub || params.data.isFocusNeighbor || params.data.nodeData)) {
            scheduleHubFocus(null);
        }
    });
    chart.on('globalout', function() {
        scheduleHubFocus(null);
    });

    // 点击空白处关闭信息面板
    chart.getZr().on('click', function(event) {
        // 没有点中节点/边时，target为空
        if (!event.target || event.target.type === 'group') {
            closeInfoPanel();
        }
    });
}

// ===== 文件夹管理 =====
async function loadFolders() {
    try {
        const res = await fetch('/api/folders');
        const folders = await res.json();
        const select = document.getElementById('folderSelect');
        select.innerHTML = '<option value="">-- 选择文件夹 --</option>';
        folders.forEach(f => {
            const opt = document.createElement('option');
            opt.value = f.path;
            opt.textContent = `${f.name} (${f.file_count}个文件)`;
            select.appendChild(opt);
        });
        // 自动选择第一个文件夹
        if (folders.length > 0) {
            select.value = folders[0].path;
            await selectFolder(folders[0].path);
        }
    } catch (e) {
        showToast('加载文件夹列表失败', 'error');
    }
}

async function selectFolder(path) {
    if (!path) return;
    closeSidebar();
    try {
        const res = await fetch('/api/folder/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path })
        });
        const data = await res.json();
        if (data.success) {
            const info = document.getElementById('folderInfo');
            info.innerHTML = `已加载: ${data.dimensions.length} 个维度`;
            showToast(`文件夹已切换，共${data.dimensions.length}个维度`, 'success');
            // 该文件夹缺少当前维度时，自动落到首个可用维度（如个人文件夹只导入了课程体系）
            const keys = (data.dimensions || []).map(d => d.key);
            if (keys.length && keys.indexOf(currentDimension) < 0) {
                const preferred = keys.indexOf('course_system') >= 0 ? 'course_system' : keys[0];
                currentDimension = preferred;
                dimensionCenter = DIMENSION_CENTER_MAP[preferred] || null;
                setActiveMenu(preferred);
            }
            await loadDimension(currentDimension);
        }
    } catch (e) {
        showToast('选择文件夹失败', 'error');
    }
}

// ===== 文件夹上传 =====
async function handleFolderUpload(event) {
    const files = event.target.files;
    if (!files || files.length === 0) return;

    showLoading(true);
    const formData = new FormData();
    const folderName = files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : 'uploaded_data';
    formData.append('folder_name', folderName);

    let hasExcel = false;
    for (let f of files) {
        if (f.name.endsWith('.xlsx')) {
            formData.append('files', f, f.name);
            hasExcel = true;
        }
    }

    if (!hasExcel) {
        showLoading(false);
        showToast('未找到Excel文件', 'error');
        return;
    }

    try {
        const res = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        const data = await res.json();
        if (data.success) {
            showToast(`成功导入: ${data.dimensions.length}个维度`, 'success');
            await loadFolders();
            const select = document.getElementById('folderSelect');
            select.value = data.folder;
            await selectFolder(data.folder);
        } else {
            showToast(data.error || '导入失败', 'error');
        }
    } catch (e) {
        showToast('导入失败: ' + e.message, 'error');
    }
    showLoading(false);
    event.target.value = '';
}

// ===== 节点颜色 =====
async function loadNodeColors() {
    try {
        const res = await fetch('/api/node-types');
        nodeColors = await res.json();
    } catch (e) {
        nodeColors = {};
    }
}

// 渲染搜索结果（seed 实体搜索、自由词搜索共用）
function renderCrossSearchData(data, opts) {
    var depth = opts.depth || 1;
    var keyword = opts.keyword || '';
    var dim = opts.dim || 'all';
    var centerLabel = opts.centerLabel || '';
    var centerEntity = opts.centerEntity || '';
    var centerId = opts.centerId || '';

    if (!data.nodes || data.nodes.length === 0) {
        showToast('未找到匹配的节点', 'error');
        document.getElementById('crossSearchResult').innerHTML = '<span style="color:var(--accent-red)">未找到匹配节点</span>';
        document.getElementById('aiAnalyzeBtn').style.display = 'none';
        var modeSelHide = document.getElementById('aiModeSelect');
        if (modeSelHide) modeSelHide.style.display = 'none';
        var gradeSelHide = document.getElementById('aiGradeSelect');
        if (gradeSelHide) gradeSelHide.style.display = 'none';
        var sourcesBtnHide = document.getElementById('sourcesBtn');
        if (sourcesBtnHide) sourcesBtnHide.style.display = 'none';
        var sourcesPanelHide = document.getElementById('sourcesPanel');
        if (sourcesPanelHide) sourcesPanelHide.style.display = 'none';
        lastSourcesData = null;
        return false;
    }

    // 更新图谱数据
    graphData = {
        nodes: data.nodes,
        edges: data.edges,
        headers: [],
        raw_data: []
    };

    // 更新标题
    document.getElementById('graphTitle').textContent = centerLabel ? ('定向图谱：' + centerLabel) : ('自由检索："' + keyword + '"');

    renderGraph();
    updateStats();
    updateLegend();

    // 根据层级差异化显示节点
    if (chart) {
        var opt = chart.getOption();
        // level: 0=中心/匹配节点, 1=一跳, 2=二跳, 3=三跳, 4=四跳
        var levelStyles = {
            0: { opacity: 1, borderWidth: 3, symbolSize: 45, fontWeight: 700, fontSize: 13 },
            1: { opacity: 0.8, borderWidth: 2, symbolSize: 35, fontWeight: 600, fontSize: 12 },
            2: { opacity: 0.55, borderWidth: 1, symbolSize: 25, fontWeight: 400, fontSize: 11 },
            3: { opacity: 0.35, borderWidth: 1, symbolSize: 18, fontWeight: 400, fontSize: 10 },
            4: { opacity: 0.25, borderWidth: 1, symbolSize: 14, fontWeight: 400, fontSize: 9 }
        };
        opt.series[0].data.forEach(function(node) {
            // 自定义数据在 nodeData 上（level/focus 均为后端节点字段）
            var nd = node.nodeData || {};
            var lvl = node.level !== undefined ? node.level : (nd.level !== undefined ? nd.level : 0);
            var style = levelStyles[lvl] || levelStyles[3];
            node.itemStyle = node.itemStyle || {};
            node.itemStyle.opacity = style.opacity;
            node.itemStyle.borderWidth = style.borderWidth;
            node.symbolSize = Math.max(node.symbolSize || 30, style.symbolSize);
            node.label = node.label || {};
            node.label.fontWeight = style.fontWeight;
            node.label.fontSize = style.fontSize;
            // 叠加条件命中的目标岗位/意向城市：琥珀色描边高亮
            if (nd.focus) {
                node.itemStyle.borderWidth = 3;
                node.itemStyle.borderColor = '#f59e0b';
                node.itemStyle.shadowColor = '#f59e0b';
                node.itemStyle.shadowBlur = 10;
                node.label.fontWeight = 600;
            }
        });
        chart.setOption(opt);
    }

    // 显示结果摘要
    var dims = new Set();
    data.nodes.forEach(function(n) {
        (n.source_dimensions || []).forEach(function(d) { dims.add(d); });
    });
    var dimTags = Array.from(dims).map(function(d) {
        return '<span class="result-dim">' + d + '</span>';
    }).join('');
    var summaryHtml =
        '匹配 <b style="color:var(--accent-cyan)">' + data.matched_count + '</b> 个节点（展开' + depth + '层），' +
        '共 <b>' + data.nodes.length + '</b> 个节点、<b>' + data.edges.length + '</b> 条边<br>' + dimTags;
    if (data.focus_matched > 0) {
        summaryHtml += '<br><span style="color:#f59e0b">◉ 已叠加高亮 ' + data.focus_matched + ' 个目标（岗位/城市）</span>';
    }
    document.getElementById('crossSearchResult').innerHTML = summaryHtml;

    // 缓存搜索数据供AI分析使用（seed 搜索时以实体名作为分析主题）
    lastSearchData = {
        keyword: centerEntity || keyword,
        dim: dim,
        depth: depth,
        nodes: data.nodes,
        edges: data.edges,
        center_id: centerId
    };
    // 显示智能分析按钮
    document.getElementById('aiAnalyzeBtn').style.display = 'flex';
    var modeSelShow = document.getElementById('aiModeSelect');
    if (modeSelShow) modeSelShow.style.display = 'flex';
    var gradeSelShow = document.getElementById('aiGradeSelect');
    if (gradeSelShow) gradeSelShow.style.display = 'flex';
    // 显示来源溯源按钮（P4），收起旧面板并重置缓存
    var sourcesBtnShow = document.getElementById('sourcesBtn');
    if (sourcesBtnShow) sourcesBtnShow.style.display = 'flex';
    var sourcesPanelHide = document.getElementById('sourcesPanel');
    if (sourcesPanelHide) sourcesPanelHide.style.display = 'none';
    lastSourcesData = null;
    // 隐藏旧的分析结果按钮和图谱切换按钮
    document.getElementById('aiResultBtn').style.display = 'none';
    document.getElementById('graphViewBtn').style.display = 'none';
    lastAIResult = null;
    // 确保图谱显示，隐藏报告面板
    document.getElementById('graphChart').style.display = 'block';
    document.querySelector('.legend').style.display = 'flex';
    document.getElementById('aiResultPanel').style.display = 'none';

    showToast('找到 ' + data.matched_count + ' 个匹配节点，展开 ' + data.nodes.length + ' 个关联节点', 'success');
    return true;
}

// ===== 统一搜索（三步：类型→实体→叠加条件） =====
var unifiedSearch = {
    type: '专业',        // 当前搜索类型（'__free__' 为高级·自由词）
    selected: null,      // {id, label} 实体级精确命中
    entityTimer: null,
    focusJobs: [],       // 目标岗位实体 id 列表
    focusCities: [],     // 意向城市实体 id 列表
    focusCache: {}       // type -> entities 目录缓存
};

function onSearchTypeChange() {
    var select = document.getElementById('searchTypeSelect');
    var input = document.getElementById('searchEntityInput');
    var list = document.getElementById('searchEntityList');
    var btn = document.getElementById('generateGraphBtn');
    unifiedSearch.type = select.value;
    unifiedSearch.selected = null;
    input.value = '';
    list.style.display = 'none';
    if (unifiedSearch.type === '__free__') {
        btn.disabled = false;
        input.placeholder = '自由关键词，如：长三角 新能源 电力电子';
        return;
    }
    btn.disabled = true;
    input.placeholder = '输入关键词联想，如：电气工程及其自动化';
    fetchSearchEntities('');
}

function onSearchEntityInput() {
    if (unifiedSearch.type === '__free__') return;
    clearTimeout(unifiedSearch.entityTimer);
    unifiedSearch.entityTimer = setTimeout(function() {
        unifiedSearch.selected = null;
        document.getElementById('generateGraphBtn').disabled = true;
        fetchSearchEntities(document.getElementById('searchEntityInput').value.trim());
    }, 300);
}

function onSearchEntityFocus() {
    if (unifiedSearch.type === '__free__') return;
    var list = document.getElementById('searchEntityList');
    if (list.style.display !== 'block') {
        fetchSearchEntities(document.getElementById('searchEntityInput').value.trim());
    }
}

async function fetchSearchEntities(keyword) {
    var list = document.getElementById('searchEntityList');
    try {
        var url = '/api/catalog?type=' + encodeURIComponent(unifiedSearch.type) + '&limit=50';
        if (keyword) url += '&keyword=' + encodeURIComponent(keyword);
        var res = await fetch(url);
        var data = await res.json();
        var entities = data.entities || [];
        if (entities.length === 0) {
            list.innerHTML = '<div class="catalog-entity-empty">无匹配实体</div>';
        } else {
            list.innerHTML = entities.map(function(ent) {
                return '<div class="catalog-entity-item" role="button" tabindex="0" data-id="' + escapeHtml(ent.id) + '" data-label="' + escapeHtml(ent.label) + '">' +
                    '<span class="entity-name">' + escapeHtml(ent.label) + '</span>' +
                    '<span class="entity-count">' + ent.edge_count + ' 条关联</span></div>';
            }).join('');
        }
        list.style.display = 'block';
    } catch (e) {
        list.innerHTML = '<div class="catalog-entity-empty">加载失败</div>';
        list.style.display = 'block';
    }
}

function selectSearchEntity(id, label) {
    unifiedSearch.selected = { id: id, label: label };
    document.getElementById('searchEntityInput').value = label;
    document.getElementById('searchEntityList').style.display = 'none';
    document.getElementById('generateGraphBtn').disabled = false;
}

// ===== 叠加条件：目标岗位/意向城市多选 =====
var FOCUS_META = {
    focusJobMulti: { type: '岗位', arr: 'focusJobs', valuesId: 'focusJobValues', listId: 'focusJobList', placeholder: '目标岗位（可多选）' },
    focusCityMulti: { type: '城市', arr: 'focusCities', valuesId: 'focusCityValues', listId: 'focusCityList', placeholder: '意向城市（可多选）' }
};

function toggleFocusList(multiId) {
    var list = document.getElementById(FOCUS_META[multiId].listId);
    var isOpen = list.style.display === 'block';
    collapseAllFocusLists();
    if (isOpen) return;
    list.style.display = 'block';
    ensureFocusOptions(multiId);
}

function collapseAllFocusLists() {
    document.querySelectorAll('.focus-multi-list').forEach(function(l) { l.style.display = 'none'; });
}

async function ensureFocusOptions(multiId) {
    var meta = FOCUS_META[multiId];
    var list = document.getElementById(meta.listId);
    if (unifiedSearch.focusCache[meta.type]) {
        renderFocusOptions(meta, list);
        return;
    }
    list.innerHTML = '<div class="catalog-entity-empty">加载中...</div>';
    try {
        var res = await fetch('/api/catalog?type=' + encodeURIComponent(meta.type) + '&limit=300');
        var data = await res.json();
        unifiedSearch.focusCache[meta.type] = data.entities || [];
    } catch (e) {
        unifiedSearch.focusCache[meta.type] = [];
    }
    renderFocusOptions(meta, list);
}

function renderFocusOptions(meta, list) {
    if (!list.querySelector('.focus-search-input')) {
        var search = document.createElement('div');
        search.className = 'focus-search-wrap';
        search.innerHTML = '<input type="text" class="focus-search-input" placeholder="输入关键词筛选' + meta.type + '…" oninput="onFocusSearch(this)">';
        list.insertBefore(search, list.firstChild);
    }
    var box = document.getElementById(meta.listId + 'Items');
    if (!box) {
        box = document.createElement('div');
        box.id = meta.listId + 'Items';
        list.appendChild(box);
    }
    box.innerHTML = buildFocusItemsHtml(meta, list);
}

function buildFocusItemsHtml(meta, list) {
    var entities = unifiedSearch.focusCache[meta.type] || [];
    var selected = unifiedSearch[meta.arr];
    var kw = (list.dataset.focusKw || '').trim().toLowerCase();
    var filtered = kw ? entities.filter(function(ent) {
        return (ent.label || '').toLowerCase().indexOf(kw) >= 0;
    }) : entities;
    if (filtered.length === 0) {
        return '<div class="catalog-entity-empty">' + (kw ? '无匹配的' + meta.type : '暂无可选项') + '</div>';
    }
    return filtered.map(function(ent) {
        var checked = selected.indexOf(ent.id) >= 0 ? ' checked' : '';
        return '<label class="focus-option"><input type="checkbox" data-id="' + escapeHtml(ent.id) + '" onchange="onFocusToggle(this)"' + checked + '> <span>' + escapeHtml(ent.label) + '</span></label>';
    }).join('');
}

function onFocusSearch(input) {
    var list = input.closest('.focus-multi-list');
    if (!list) return;
    list.dataset.focusKw = input.value;
    var multi = input.closest('.focus-multi');
    var meta = multi && FOCUS_META[multi.id];
    if (!meta) return;
    var box = document.getElementById(meta.listId + 'Items');
    if (box) box.innerHTML = buildFocusItemsHtml(meta, list);
}

function onFocusToggle(cb) {
    var multi = cb.closest('.focus-multi');
    if (!multi) return;
    var meta = FOCUS_META[multi.id];
    if (!meta) return;
    var arr = unifiedSearch[meta.arr];
    var idx = arr.indexOf(cb.dataset.id);
    if (cb.checked && idx < 0) arr.push(cb.dataset.id);
    if (!cb.checked && idx >= 0) arr.splice(idx, 1);
    renderFocusValues(meta);
}

function renderFocusValues(meta) {
    var box = document.getElementById(meta.valuesId);
    var selected = unifiedSearch[meta.arr];
    if (selected.length === 0) {
        box.innerHTML = '<span class="focus-multi-placeholder">' + meta.placeholder + '</span>';
        return;
    }
    var cache = unifiedSearch.focusCache[meta.type] || [];
    var nameOf = {};
    cache.forEach(function(ent) { nameOf[ent.id] = ent.label; });
    var chips = selected.slice(0, 3).map(function(id) {
        var label = nameOf[id] || id.split(':').pop();
        return '<span class="focus-chip">' + escapeHtml(label) + '</span>';
    });
    if (selected.length > 3) chips.push('<span class="focus-chip">+' + (selected.length - 3) + '</span>');
    box.innerHTML = chips.join('');
}

// ===== 生成图谱（统一搜索入口） =====
async function generateGraph() {
    closeSidebar();
    var isFree = unifiedSearch.type === '__free__';
    var keywordInput = document.getElementById('searchEntityInput').value.trim();
    var params = new URLSearchParams();
    // 综合检索生成的是最全图谱（含四个中心），清除维度聚焦
    dimensionCenter = null;
    params.set('depth', document.getElementById('searchDepthSelect') ? document.getElementById('searchDepthSelect').value : '3');
    if (isFree) {
        if (!keywordInput) {
            showToast('请输入搜索关键词', 'error');
            return;
        }
        params.set('q', keywordInput);
    } else {
        if (!unifiedSearch.selected) {
            showToast('请先在第二步选择具体实体', 'error');
            return;
        }
        params.set('seed', unifiedSearch.selected.id);
    }
    if (unifiedSearch.focusJobs.length) params.set('focus_jobs', unifiedSearch.focusJobs.join(','));
    if (unifiedSearch.focusCities.length) params.set('focus_cities', unifiedSearch.focusCities.join(','));

    showLoading(true);
    try {
        var res = await fetch('/api/search?' + params.toString());
        if (!res.ok) {
            var err = await res.json();
            showToast(err.error || '搜索失败', 'error');
            showLoading(false);
            return;
        }
        var data = await res.json();
        var opts;
        if (isFree) {
            opts = { keyword: keywordInput, dim: 'all', depth: params.get('depth') };
        } else {
            opts = {
                depth: params.get('depth'),
                dim: 'all',
                centerLabel: unifiedSearch.selected.label + '（' + (data.nodes ? data.nodes.length : 0) + '节点）',
                centerEntity: unifiedSearch.selected.label,
                centerId: unifiedSearch.selected.id
            };
        }
        renderCrossSearchData(data, opts);
    } catch (e) {
        showToast('搜索失败: ' + e.message, 'error');
    }
    showLoading(false);
}

// ===== AI 智能分析 =====
let lastSearchData = null;  // 缓存最近一次搜索数据
let lastAIResult = null;    // 缓存AI分析结果
let aiProgressTimer = null;

function startAIAnalysis() {
    if (!lastSearchData || !lastSearchData.nodes || lastSearchData.nodes.length === 0) {
        showToast('请先搜索并展示图谱', 'error');
        return;
    }

    var btn = document.getElementById('aiAnalyzeBtn');
    btn.disabled = true;
    btn.innerHTML = '<span>分析中...</span>';

    // 显示加载条
    var loadingBar = document.getElementById('aiLoadingBar');
    loadingBar.style.display = 'block';
    var fill = document.getElementById('aiProgressFill');
    var detail = document.getElementById('aiLoadingDetail');
    var text = document.getElementById('aiLoadingText');

    // 模拟进度
    var progress = 0;
    var stages = [
        { p: 15, msg: '正在整理图谱节点和关联关系...' },
        { p: 30, msg: '构建分析上下文...' },
        { p: 45, msg: '发送数据到智能体...' },
        { p: 60, msg: '智能体生成中，通常需要几分钟，请耐心等待...' },
        { p: 75, msg: '提取关键节点和关系链...' },
        { p: 88, msg: '正在生成分析报告，服务繁忙时可能需要更久...' }
    ];
    var stageIdx = 0;
    fill.style.width = '5%';
    fill.style.background = '';
    detail.textContent = stages[0].msg;
    aiProgressTimer = setInterval(function() {
        if (stageIdx < stages.length) {
            progress = stages[stageIdx].p;
            detail.textContent = stages[stageIdx].msg;
            fill.style.width = progress + '%';
            stageIdx++;
        } else {
            progress = Math.min(progress + 1, 95);
            fill.style.width = progress + '%';
        }
    }, 2000);

    // 调用后端API
    var abortCtrl = new AbortController();
    var fetchTimeout = setTimeout(function() { abortCtrl.abort(); }, 780000);
    fetch('/api/ai-analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortCtrl.signal,
        body: JSON.stringify({
            keyword: lastSearchData.keyword,
            dim: lastSearchData.dim,
            depth: lastSearchData.depth,
            nodes: lastSearchData.nodes,
            edges: lastSearchData.edges,
            mode: (function() { var sel = document.getElementById('aiModeSelect'); return sel ? sel.value : 'auto'; })(),
            user_grade: (function() { var sel = document.getElementById('aiGradeSelect'); return sel ? sel.value : ''; })(),
            center_id: lastSearchData.center_id || '',
            target_jobs: (function() {
                var ids = (window.unifiedSearch && unifiedSearch.focusJobs) ? unifiedSearch.focusJobs : [];
                if (!ids.length) return [];
                var nameById = {};
                (lastSearchData.nodes || []).forEach(function(n) {
                    if (n && n.id != null) nameById[String(n.id)] = String(n.name || '').trim();
                });
                return ids.map(function(id) { return nameById[String(id)] || String(id).trim(); }).filter(function(s) { return s; });
            })()
        })
    }).then(function(res) {
        clearTimeout(fetchTimeout);
        return res.json();
    }).then(function(data) {
        clearInterval(aiProgressTimer);
        fill.style.width = '100%';
        setTimeout(function() {
            loadingBar.style.display = 'none';
        }, 1500);

        if (data.success) {
            text.textContent = '分析完成';
            detail.textContent = '报告已生成，点击"查看分析报告"查看';
            lastAIResult = data.analysis;
            lastAIResult._keyword = lastSearchData.keyword;
            lastAIResult._dim = lastSearchData.dim;
            lastAIResult._mode = data.mode || '';
            lastAIResult._mode_label = data.mode_label || '';
            lastAIResult._validation = data.validation || null;
            // 显示两个切换按钮
            document.getElementById('aiResultBtn').style.display = 'flex';
            document.getElementById('graphViewBtn').style.display = 'flex';
            if (data.report_path) {
                lastAIResult._report_path = data.report_path;
                if (data.truncated) {
                    var caps = (data.llm_caps && data.llm_caps.nodes) ? data.llm_caps : { nodes: 80, edges: 120 };
                    showToast('分析完成（数据量较大，已按相关度截取前' + caps.nodes + '个节点/' + caps.edges + '条关系参与分析），报告已保存到 report/' + data.report_path, 'success');
                } else {
                    showToast('分析完成，报告已保存到 report/' + data.report_path, 'success');
                }
            } else {
                showToast('分析完成，点击"查看分析报告"查看结果', 'success');
            }
        } else {
            // 失败时进度条明确呈现失败状态，不再显示"报告已生成"误导文案
            text.textContent = '分析失败';
            fill.style.background = '#e74c3c';
            detail.textContent = '分析未成功：' + (data.error || '服务返回异常') + '，请重试或更换报告模式';
            showToast(data.error || '分析失败', 'error');
        }

        btn.disabled = false;
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>重新分析';
    }).catch(function(err) {
        clearTimeout(fetchTimeout);
        clearInterval(aiProgressTimer);
        loadingBar.style.display = 'none';
        btn.disabled = false;
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>智能路径分析';
        if (err.name === 'AbortError') {
            showToast('分析超时（AI服务响应过慢），请稍后重试', 'error');
        } else {
            showToast('分析请求失败: ' + err.message, 'error');
        }
    });
}

// ===== P0-2 分层行动式报告渲染辅助 =====
// [来源：xx] → 上标[Sn]并去重收集进aiRefList（与后端HTML报告同规则）
var aiRefList = [];

function renderRefsAI(text) {
    var raw = String(text || '');
    var out = '';
    var pos = 0;
    var re = /\[来源[：:]\s*([^\[\]]{2,80})\]/g;
    var m;
    while ((m = re.exec(raw)) !== null) {
        var src = m[1].trim();
        out += escapeHtml(raw.slice(pos, m.index));
        pos = m.index + m[0].length;
        src = src.split(/P?\s*(?:&quot;|["\u201c\u201d])/)[0].trim();
        src = src.replace(/(?:\s+(?:[Pp]\d+(?:\s*[,，、~\-至]\s*[Pp]?\d+)*|\d{1,3}(?:\s*[,，、~\-至]\s*\d{1,3})*|第\s*\d+\s*页|无))+\s*$/, '').trim();
        if (!src || /^(无|未知|暂无|未提供|n\/a|none|ai\s*建\s*议)$/i.test(src)) continue;
        if (aiRefList.indexOf(src) < 0) aiRefList.push(src);
        out += '<span class="ai-ref-sup">[S' + (aiRefList.indexOf(src) + 1) + ']</span>';
    }
    out += escapeHtml(raw.slice(pos));
    return out;
}

// 四段式徽标映射：course=官方课程(L1蓝) skill=技能要求(L2绿) comp=竞赛参考(L2橙) advice=AI建议(L3黄)
var AI_TAG_META = {
    'course': { label: '官方课程', cls: 'ai-tag-course' },
    'skill': { label: '技能要求', cls: 'ai-tag-skill' },
    'comp': { label: '竞赛参考', cls: 'ai-tag-comp' },
    'advice': { label: 'AI建议', cls: 'ai-tag-advice' }
};

var AI_SEM_ORDER = ['大一·秋', '大一·春', '大二·秋', '大二·春', '大三·秋', '大三·春', '大四·秋', '大四·春'];

// M4课程过滤：默认全部隐藏（报告生成时不显示），勾选类目后显示对应课程；英语课程一律不进路径（无显示开关）
var M4_FILTER_CATS = ['体育军事', '思政', '通识必修'];
var aiFilterOn = {};
M4_FILTER_CATS.forEach(function(c) { aiFilterOn[c] = false; });
var aiFilterRestored = false;
// 英语课程硬过滤：培养路径不显示，也不提供显示开关
function m4IsEnglish(t) {
    var s = String(t || '');
    var m = s.match(/《([^》]+)》/);
    return /英语/.test(m ? m[1] : s);
}
// 课程分类判定：体育军事/思政/通识必修（branch=所属思维导图分支名）
function m4CatOf(t, branch) {
    var s = String(t || '');
    var m = s.match(/《([^》]+)》/);
    var name = m ? m[1] : s;
    if (/体育|军事/.test(name)) return '体育军事';
    if (/思政|思想道德|马克思主义|毛概|毛泽东思想|近现代史|中国近代史|形势与政策|习近平|德法|伦理/.test(name)) return '思政';
    if (String(branch || '').trim() === '大类通识课' || /^通识/.test(s.trim())) return '通识必修';
    return '';
}
var M4_BRANCH_CAT = { '大类通识课': '通识必修', '专业必修课': '专业必修', '专业选修课': '专业选修', '选修课': '专业选修' };
var M4_COURSE_INDEX = [];

// ===== M4 成长时间轴画布：竞赛/技能块拖放状态 =====
// block = { kind:'skill'|'comp', name, sem:'大一·秋' 等, fromPlan:bool, off:bool }
var M4_CANVAS_STATE = { blocks: [] };
var M4_CUSTOM_BLOCKS = []; // 工具区自主添加的块（技能/竞赛），从画布移除后回到工具区
var m4CanvasRestored = false;
var _aiDragPayload = null;

function aiBlockDragStart(e) {
    var el = e.currentTarget;
    var kd = el.getAttribute('data-kind');
    _aiDragPayload = {
        kind: (kd === 'comp' || kd === 'other') ? kd : 'skill',
        name: el.getAttribute('data-name') || '',
        src: el.getAttribute('data-src') || 'canvas'
    };
    try { e.dataTransfer.setData('text/plain', JSON.stringify(_aiDragPayload)); } catch (err) {}
    e.dataTransfer.effectAllowed = 'move';
    if (el.classList) el.classList.add('ai-dragging');
}
function aiBlockDragEnd(e) {
    var el = e.currentTarget;
    if (el.classList) el.classList.remove('ai-dragging');
    _aiDragPayload = null;
}
function aiCanvasDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    var z = e.currentTarget;
    if (z.classList) z.classList.add('ai-drop-hover');
}
function aiCanvasDragLeave(e) {
    var z = e.currentTarget;
    if (z.classList) z.classList.remove('ai-drop-hover');
}
function aiParseDragPayload(e) {
    var raw = '';
    try { raw = e.dataTransfer.getData('text/plain'); } catch (err) {}
    if (!raw && _aiDragPayload) return _aiDragPayload;
    try { return JSON.parse(raw); } catch (err) { return null; }
}
function aiCanvasDrop(e) {
    e.preventDefault();
    var z = e.currentTarget;
    if (z.classList) z.classList.remove('ai-drop-hover');
    var p = aiParseDragPayload(e);
    if (!p || !String(p.name || '').trim()) return;
    var kind = (p.kind === 'comp' || p.kind === 'other') ? p.kind : 'skill';
    var name = String(p.name).trim();
    var sem = z.getAttribute('data-sem') || '';
    var hit = null;
    for (var i = 0; i < M4_CANVAS_STATE.blocks.length; i++) {
        var b = M4_CANVAS_STATE.blocks[i];
        if (b.kind === kind && b.name === name) { hit = b; break; }
    }
    if (hit) { hit.sem = sem; hit.off = false; }
    else M4_CANVAS_STATE.blocks.push({ kind: kind, name: name, sem: sem, fromPlan: p.src !== 'dock' });
    showAIResult();
}
function aiSrcDrop(e) {
    e.preventDefault();
    if (e.stopPropagation) e.stopPropagation();
    var z = e.currentTarget;
    if (z.classList) z.classList.remove('ai-drop-hover');
    var p = aiParseDragPayload(e);
    if (!p || p.src !== 'canvas') return;
    var kind = (p.kind === 'comp' || p.kind === 'other') ? p.kind : 'skill';
    var name = String(p.name || '').trim();
    M4_CANVAS_STATE.blocks.forEach(function(b) {
        if (b.kind === kind && b.name === name) {
            if (b.fromPlan) b.off = true; else b._del = true;
        }
    });
    M4_CANVAS_STATE.blocks = M4_CANVAS_STATE.blocks.filter(function(b) { return !b._del; });
    showAIResult();
}
function aiBlockRemove(e, el) {
    if (e && e.stopPropagation) e.stopPropagation();
    var blk = el && el.closest ? el.closest('.ai-canvas-block') : el;
    if (!blk) return;
    var kd = blk.getAttribute('data-kind');
    var kind = (kd === 'comp' || kd === 'other') ? kd : 'skill';
    var name = String(blk.getAttribute('data-name') || '').trim();
    M4_CANVAS_STATE.blocks.forEach(function(b) {
        if (b.kind === kind && b.name === name) {
            if (b.fromPlan) b.off = true; else b._del = true;
        }
    });
    M4_CANVAS_STATE.blocks = M4_CANVAS_STATE.blocks.filter(function(b) { return !b._del; });
    showAIResult();
}
async function saveM4Layout() {
    var btn = document.querySelector('.ai-m4-save-btn');
    var oldText = btn ? btn.textContent : '';
    var payload = {
        filters: M4_FILTER_CATS.filter(function(c) { return aiFilterOn[c]; }),
        canvas: M4_CANVAS_STATE.blocks.filter(function(b) { return !b.off; }).map(function(b) {
            return { kind: b.kind, name: b.name, sem: b.sem };
        }),
        custom: M4_CUSTOM_BLOCKS.slice()
    };
    try {
        var res = await fetch('/api/profile', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ m4_layout: payload })
        });
        var data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || '保存失败');
        profileCache.m4_layout = payload;
        if (btn) { btn.textContent = '✓ 已保存'; setTimeout(function() { btn.textContent = oldText; }, 1800); }
        showToast('个性培养布局已保存到我的画像', 'success');
    } catch (e) {
        showToast('保存布局失败：' + (e.message || e), 'error');
    }
}
// ===== 工具区拖拽源模块（技能 / 竞赛推荐）=====
function aiSrcDockWrap() {
    var body = document.getElementById('toolDockBody');
    if (!body) return null;
    var empty = document.getElementById('toolDockEmpty');
    var wrap = document.getElementById('aiSrcDockWrap');
    if (!wrap) {
        wrap = document.createElement('div');
        wrap.id = 'aiSrcDockWrap';
        wrap.style.display = 'none';
        wrap.setAttribute('ondragover', 'aiCanvasDragOver(event)');
        wrap.setAttribute('ondragleave', 'aiCanvasDragLeave(event)');
        wrap.setAttribute('ondrop', 'aiSrcDrop(event)');
        if (empty) body.insertBefore(wrap, empty); else body.appendChild(wrap);
    }
    return wrap;
}
// 添加块表单：回车提交
function aiCustomNameKey(e) {
    if (e && e.key === 'Enter') { e.preventDefault(); aiCustomBlockAdd(); }
}
// 添加自定义块（技能/竞赛/其他）到对应框，可拖入成长轴
function aiCustomBlockAdd() {
    var kindEl = document.getElementById('aiCustomKind');
    var nameEl = document.getElementById('aiCustomName');
    var kd = kindEl ? kindEl.value : 'skill';
    var kind = (kd === 'comp' || kd === 'other') ? kd : 'skill';
    var nm = ((nameEl && nameEl.value) || '').trim();
    if (!nm) { showToast('请输入名称', 'error'); return; }
    var dup = M4_CUSTOM_BLOCKS.some(function(b) { return b.kind === kind && b.name === nm; });
    if (dup) { showToast('「' + nm + '」已在列表中', 'info'); return; }
    M4_CUSTOM_BLOCKS.push({ kind: kind, name: nm });
    renderAiSrcDock();
    var boxName = kind === 'comp' ? '竞赛推荐' : (kind === 'other' ? '其他·自定义' : '技能·本专业所需');
    showToast('已添加「' + nm + '」到「' + boxName + '」框，可拖入成长轴', 'success');
}
// 删除自定义块（仅从工具区移除；已放入画布的不受影响）
function aiCustomBlockRemove(e, el) {
    if (e && e.stopPropagation) e.stopPropagation();
    var blk = el && el.closest ? el.closest('.ai-canvas-block') : null;
    if (!blk) return;
    var kd = blk.getAttribute('data-kind');
    var kind = (kd === 'comp' || kd === 'other') ? kd : 'skill';
    var name = String(blk.getAttribute('data-name') || '').trim();
    M4_CUSTOM_BLOCKS = M4_CUSTOM_BLOCKS.filter(function(b) { return !(b.kind === kind && b.name === name); });
    renderAiSrcDock();
}
function renderAiSrcDock() {
    var wrap = aiSrcDockWrap();
    if (!wrap) return;
    var a = lastAIResult || {};
    var skills = [], comps = [], seen = {};
    var _onCanvas = {}; // 已在成长轴画布上的块：工具区不再显示（拖回后重新出现）
    M4_CANVAS_STATE.blocks.forEach(function(b) {
        if (!b.off) _onCanvas[((b.kind === 'comp' || b.kind === 'other') ? b.kind : 'skill') + '|' + String(b.name || '').replace(/\s+/g, '')] = true;
    });
    var push = function(arr, kind, t) {
        t = String(t || '').trim();
        if (!t) return;
        var k = kind + '|' + t.replace(/\s+/g, '');
        if (seen[k] || _onCanvas[k]) return;
        seen[k] = true;
        arr.push(t);
    };
    (a.four_year_plan || []).forEach(function(p) {
        if (!p || typeof p !== 'object') return;
        var tag = String(p.tag || '').trim();
        if (tag === 'skill') push(skills, 'skill', p.text);
        else if (tag === 'comp') push(comps, 'comp', p.text);
    });
    (a.action_list || []).forEach(function(x) {
        if (!x || typeof x !== 'object') return;
        (((x || {}).comps) || []).forEach(function(t) { push(comps, 'comp', t); });
    });
    (((a.mindmap || {}).categories) || []).forEach(function(c) {
        if (!c || String(c.name || '').trim() !== '竞赛实践') return;
        (c.years || []).forEach(function(y) { ((y || {}).items || []).forEach(function(x) { push(comps, 'comp', x); }); });
    });
    if (skills.length === 0) (profileCache.known_skills || []).forEach(function(t) { push(skills, 'skill', t); });
    var chip = function(kind, t) {
        return '<div class="ai-canvas-block ai-canvas-' + kind + '" draggable="true" data-kind="' + kind + '" data-name="' + escapeHtml(t) + '" data-src="dock" ondragstart="aiBlockDragStart(event)" ondragend="aiBlockDragEnd(event)" onclick="openSmartAnalysis(this.getAttribute(\'data-kind\'), this.getAttribute(\'data-name\'))" title="点击查看智能分析 · 可拖入右侧时间轴画布">' +
            '<span class="ai-canvas-kind">' + (kind === 'comp' ? '赛' : (kind === 'other' ? '他' : '技')) + '</span>' +
            '<span class="ai-canvas-text">' + renderRefsAI(t) + '</span></div>';
    };
    var customChip = function(b) {
        return '<div class="ai-canvas-block ai-canvas-' + b.kind + '" draggable="true" data-kind="' + b.kind + '" data-name="' + escapeHtml(b.name) + '" data-src="dock" ondragstart="aiBlockDragStart(event)" ondragend="aiBlockDragEnd(event)" onclick="openSmartAnalysis(this.getAttribute(\'data-kind\'), this.getAttribute(\'data-name\'))" title="点击查看智能分析 · 可拖入右侧时间轴画布">' +
            '<span class="ai-canvas-kind">' + (b.kind === 'comp' ? '赛' : (b.kind === 'other' ? '他' : '技')) + '</span>' +
            '<span class="ai-canvas-text">' + escapeHtml(b.name) + '</span>' +
            '<span class="ai-canvas-x" draggable="false" onclick="aiCustomBlockRemove(event, this)" title="删除">×</span></div>';
    };
    var html = '<div class="ai-src-add-wrap">' +
        '<div class="ai-src-mod-title">添加块<span>（技能 / 竞赛 / 其他）</span></div>' +
        '<div class="pe-m4-add ai-src-add">' +
        '<select id="aiCustomKind"><option value="skill">技能</option><option value="comp">竞赛</option><option value="other">其他</option></select>' +
        '<input id="aiCustomName" type="text" placeholder="输入名称" maxlength="40" onkeydown="aiCustomNameKey(event)">' +
        '<button type="button" class="btn-pe-m4-add" onclick="aiCustomBlockAdd()">添加</button>' +
        '</div></div>';
    var _visOf = function(arr) {
        return arr.filter(function(b) { return !_onCanvas[b.kind + '|' + String(b.name || '').replace(/\s+/g, '')]; });
    };
    var _cSkills = _visOf(M4_CUSTOM_BLOCKS.filter(function(b) { return b.kind === 'skill'; }));
    var _cComps = _visOf(M4_CUSTOM_BLOCKS.filter(function(b) { return b.kind === 'comp'; }));
    var _cOthers = _visOf(M4_CUSTOM_BLOCKS.filter(function(b) { return b.kind === 'other'; }));
    var emp = function(t) { return '<div class="ai-src-empty">' + t + '</div>'; };
    var modBox = function(title, listHtml) {
        return '<div class="ai-src-mod" ondragover="aiCanvasDragOver(event)" ondragleave="aiCanvasDragLeave(event)" ondrop="aiSrcDrop(event)">' +
            '<div class="ai-src-mod-title">' + title + '<span>（可拖入画布 · 拖回此处可移除）</span></div>' +
            '<div class="ai-src-list">' + listHtml + '</div></div>';
    };
    html += modBox('技能 · 本专业所需',
        (skills.length > 0 ? skills.map(function(t) { return chip('skill', t); }).join('') : '') +
        _cSkills.map(customChip).join('') +
        (skills.length === 0 && _cSkills.length === 0 ? emp('暂无条目 · 可将画布上的技能块拖回此处') : ''));
    html += modBox('竞赛推荐',
        (comps.length > 0 ? comps.map(function(t) { return chip('comp', t); }).join('') : '') +
        _cComps.map(customChip).join('') +
        (comps.length === 0 && _cComps.length === 0 ? emp('暂无条目 · 可将画布上的竞赛块拖回此处') : ''));
    html += modBox('其他 · 自定义',
        _cOthers.map(customChip).join('') +
        (_cOthers.length === 0 ? emp('暂无条目 · 在上方选择「其他」添加') : ''));
    wrap.innerHTML = html;
    wrap.style.display = '';
}
function hideAiSrcDock() {
    var w = document.getElementById('aiSrcDockWrap');
    if (w) w.style.display = 'none';
}

// 从课程显示文本（slimCourseText 输出）提取要素：{name, credit, type}
function parseCourseText(t) {
    var s = String(t || '').replace(/★建议优先/g, '').trim();
    var m = s.match(/《([^》]+)》/);
    var name = m ? m[1] : s;
    var credit = '';
    var cm = s.match(/[（(](\d+(?:\.\d+)?)\s*学分[）)]/);
    if (cm) credit = cm[1];
    var pre = m ? s.slice(0, m.index) : '';
    var type = /必修/.test(pre) ? '必修' : (/选修/.test(pre) ? '选修' : '');
    return { name: name, credit: credit, type: type };
}

// 课程条目文本瘦身：去掉模型编造的"（学分未标，必修）"类元信息备注
function slimCourseText(t) {
    var s = String(t || '').trim();
    s = s.replace(/[（(][^（）()]*学分未标[^（）()]*[）)]/g, '');
    var src = '';
    var sm = s.match(/\s*\[来源[：:][^\[\]]+\]\s*$/);
    if (sm) { src = sm[0].trim(); s = s.slice(0, sm.index).trim(); }
    var m = s.match(/《[^》]+》/);
    if (!m) return (s + ' ' + src).trim();
    var name = m[0];
    var pre = s.slice(0, m.index).replace(/[：:]\s*$/, '').trim();
    var type = /必修/.test(pre) ? '必修' : (/选修/.test(pre) ? '选修' : '');
    var rest = s.slice(m.index + name.length);
    var cm = rest.match(/^[（(]\s*(\d+(?:\.\d+)?)\s*学分\s*[）)]/);
    return ((type ? type : '') + name + (cm ? '（' + cm[1] + '学分）' : '') + (src ? ' ' + src : '')).trim();
}

// 文本级去重：新条目与已有条目互为包含时视为重复
function dedupeByText(arr) {
    var seen = [];
    var out = [];
    (arr || []).forEach(function(t) {
        var k = String(t || '').replace(/\s+/g, '');
        if (!k) return;
        var dup = seen.some(function(s) { return s.indexOf(k) >= 0 || k.indexOf(s) >= 0; });
        if (!dup) { seen.push(k); out.push(String(t).trim()); }
    });
    return out;
}

function showAIResult() {
    if (!lastAIResult) return;
    var panel = document.getElementById('aiResultPanel');
    var body = document.getElementById('aiResultBody');
    var titleEl = document.getElementById('aiResultTitle');
    var a = lastAIResult;

    var dimNames = {
        'job_cluster': '专业岗位', 'skill_structure': '技能结构',
        'career_path': '发展路径', 'course_system': '课程体系',
        'job_coopetition': '岗位竞合', 'competition_practice': '竞赛实践',
        'all': '综合维度'
    };
    var dimName = dimNames[a._dim] || '综合维度';

    var modeLabels = { 'm1': '岗位对标', 'm2': '专业出口', 'm3': '技能评估', 'm4': '个性培养', 'm5': '职业方向' };
    var modeLabel = a._mode_label || modeLabels[a._mode] || '';

    var html = '';

    // 标题
    html += '<div class="ai-report-title">' + escapeHtml(a.title || '分析报告') + '</div>';
    html += '<div class="ai-report-meta">关键词：' + escapeHtml(a._keyword || '') + '　|　维度：' + dimName + (modeLabel ? '　|　报告模式：' + escapeHtml(modeLabel) : '') + '　|　生成时间：' + new Date().toLocaleString('zh-CN') + '</div>';
    aiRefList = [];
    if (a._validation) {
        var v = a._validation;
        html += '<div class="ai-report-meta" style="font-size:11px;opacity:.7;">接地校验：删除句子 ' + (v.deleted_sentences || 0) +
            '，补来源 ' + (v.patched_sources || 0) +
            '，补AI标签 ' + (v.patched_ai_tags || 0) +
            '，课程降级 ' + (v.course_downgraded || 0) +
            '，非法标签修正 ' + (v.bad_tags || 0) +
            '，学期归一 ' + (v.bad_stages || 0) +
            '，剔除无效项 ' + (v.invalid_items_removed || 0) +
            '，剔除建议编造 ' + (v.advice_items_removed || 0) +
            '，剔除陈词 ' + (v.cliche_sentences_removed || 0) +
            '，重写 ' + (v.rewritten_sentences || 0) +
            '，替换违规单元格 ' + (v.replaced_cells || 0) +
            '，裁剪超长 ' + (v.length_trimmed || 0) +
            '，行动项回填 ' + (v.action_backfilled || 0) +
            '，缺失学期 ' + (v.missing_semesters || []).length + '/' + (function() { var g = (document.getElementById('aiGradeSelect') || {}).value; var i = ['大一', '大二', '大三', '大四'].indexOf(g); return i >= 0 ? (4 - i) * 2 : 8; })() + '</div>';
    }
    html += '<div class="ai-report-meta" style="font-size:11px;opacity:.7;">三层标注：<span style="color:#1d4ed8;">L1 官方课程</span>（培养方案/职业标准权威骨架）· <span style="color:#047857;">L2 技能/竞赛</span>（多源抽取，正文上标[Sn]可对应文末数据来源）· <span style="color:#b45309;">L3 AI建议</span>（模型补全，仅供参考）</div>';

    // 总结
    if (a.summary) {
        html += '<div class="ai-report-summary">' + renderRefsAI(a.summary).replace(/\n/g, '<br>') + '</div>';
    }

    // ① 目标岗位画像（L1权威+L2抽取）
    var profile = a.target_profile || {};
    var profileItems = (profile.items || []).filter(function(i) { return i && String(i).trim(); });
    if (String(profile.title || '').trim() || profileItems.length > 0) {
        html += '<div class="ai-section-title">' + escapeHtml(String(profile.title || '目标岗位画像').trim()) + '<span class="ai-fact-chip">L1 权威 + L2 抽取</span></div>';
        if (profileItems.length > 0) {
            html += '<div class="ai-profile-card"><div class="ai-profile-label">画像要点</div><ul class="ai-profile-list">';
            profileItems.forEach(function(it) {
                html += '<li>' + renderRefsAI(String(it).trim()) + '</li>';
            });
            html += '</ul></div>';
        }
    }

    // ①-B0 M4：先从画像恢复过滤偏好与画布布局（必须在 mindmap/plan 课程解析之前，否则首渲染过滤不生效）
    if (!aiFilterRestored) {
        aiFilterRestored = true;
        var _savedLayout = profileCache.m4_layout || {};
        var _savedF = _savedLayout.filters;
        if (Array.isArray(_savedF)) M4_FILTER_CATS.forEach(function(c) { aiFilterOn[c] = _savedF.indexOf(c) >= 0; });
        var _savedCustom = _savedLayout.custom;
        if (Array.isArray(_savedCustom)) M4_CUSTOM_BLOCKS = _savedCustom.filter(function(b) { return b && String(b.name || '').trim(); }).map(function(b) {
            return { kind: (b.kind === 'comp' || b.kind === 'other') ? b.kind : 'skill', name: String(b.name).trim() };
        });
    }
    if (!m4CanvasRestored) {
        m4CanvasRestored = true;
        var _savedC = (profileCache.m4_layout || {}).canvas;
        if (Array.isArray(_savedC)) M4_CANVAS_STATE.blocks = _savedC.filter(function(b) { return b && String(b.name || '').trim(); }).map(function(b) {
            return { kind: (b.kind === 'comp' || b.kind === 'other') ? b.kind : 'skill', name: String(b.name).trim(), sem: String(b.sem || '').trim(), fromPlan: false };
        });
    }

    // ①-B M4附加：个性培养数据解析（不再按五大分类分块——课程按学期括注并入下方"四年培养路径总览"时间轴）
    var mindmap = a.mindmap || {};
    var mmCats = (mindmap.categories || []).filter(function(c) { return c && String(c.name || '').trim(); });
    var MM_TAG_PREFIX = { '大类通识课': '通识', '专业必修课': '必修', '专业选修课': '选修', '选修课': '选修' };
    var mmCourseBySem = {};
    var mmCompByYear = {};
    var mmRecNotes = [];
    mmCats.forEach(function(c) {
        var cname = String(c.name).trim();
        var rec = String(c.recommend_note || '').trim();
        if (rec && cname !== '竞赛实践') mmRecNotes.push(rec);
        if (cname === '竞赛实践') {
            (c.years || []).forEach(function(y) {
                var yn = String((y || {}).year || '').trim();
                if (!yn) return;
                (y.items || []).forEach(function(x) {
                    var s = String(x || '').trim();
                    if (s) (mmCompByYear[yn] = mmCompByYear[yn] || []).push(s);
                });
            });
            return;
        }
        var prefix = MM_TAG_PREFIX[cname] || '';
        (c.years || []).forEach(function(y) {
            var yn = String((y || {}).year || '').trim();
            (y.items || []).forEach(function(x) {
                var s0 = String(x || '').trim();
                if (!s0) return;
                var star = s0.indexOf('★') >= 0;
                var s = s0.replace(/★建议优先/g, '').trim();
                var cm = s.match(/《[^》]+》/);
                if (!cm) return;
                var disp = slimCourseText(s);
                if (prefix && !/^(必修|选修|通识)/.test(disp)) disp = prefix + disp;
                if (star) disp += '★建议优先';
                if (m4IsEnglish(disp)) return;
                var _mmCat = m4CatOf(disp, cname);
                if (_mmCat && !aiFilterOn[_mmCat]) return;
                var sems = s.match(/大[一二三四]·[秋春]/g) || [];
                if (sems.length === 0) sems = [(yn && yn.indexOf('大') === 0 ? yn : '其他') + '·未标注学期'];
                sems.forEach(function(sem) {
                    var slot = (mmCourseBySem[sem] = mmCourseBySem[sem] || []);
                    if (!slot.some(function(z) { return z.d === disp; })) slot.push({ d: disp, b: cname });
                });
            });
        });
    });
    var mmHasCourses = Object.keys(mmCourseBySem).length > 0;

    // ①-C M5附加：岗位分类总览（LLM生成；点击岗位 → 右侧工具栏展示九段式职业发展图）
    var cpData = a.career_paths || {};
    var cpMajor = String(cpData.root_major || '').trim() || '本专业';
    if (cpMajor && (String(cpData.title || '').trim() || (cpData.paths || []).length > 0)) {
        html += '<div id="aiJobOverview"><div class="ai-job-overview-loading">正在生成岗位总览…</div></div>';
    }

    // ② 四年培养路径总览（仅个性培养m4报告显示；我的画像使用独立编辑器，不经此处渲染）：左栏=课程主线时间轴（L1官方课程） 右栏=成长时间轴画布（L2+L3）
    var plan = (a.four_year_plan || []).filter(function(p) {
        return p && typeof p === 'object' && String(p.text || '').trim();
    });
    var actions = (a.action_list || []).filter(function(x) { return x && typeof x === 'object'; });
    if (a._mode === 'm4' && (plan.length > 0 || actions.length > 0 || mmHasCourses)) {
        var hasCourseItems = mmHasCourses || plan.some(function(p) { return String(p.tag || '').trim() === 'course'; });
        html += '<div class="ai-section-title">' + ((function() { var g = (document.getElementById('aiGradeSelect') || {}).value; return ['大一', '大二', '大三', '大四'].indexOf(g) >= 0 ? '培养路径总览（' + g + '—大四）' : '四年培养路径总览'; })()) + '<span class="ai-fact-chip">官方培养方案骨架 + 分层补全</span>' +
            (hasCourseItems ? '<span id="aiM4Filters" class="ai-filter-group">' + M4_FILTER_CATS.map(function(cat) { return '<label class="ai-filter-toggle"><input type="checkbox" class="ai-m4-cat" data-cat="' + cat + '"' + (aiFilterOn[cat] ? ' checked' : '') + '>' + cat + '</label>'; }).join('') + '<button class="ai-m4-save-btn" onclick="saveM4Layout()">保存布局到画像</button></span>' : '') + '</div>';
        html += '<div class="ai-plan-grid">';

        // ---- 左栏：课程主线（mindmap个性化培养 + tag=course官方课程，按学年→学期时间轴，按课名去重）----
        var courseBySem = {};
        var courseSeen = {};
        M4_COURSE_INDEX = [];
        var creditByCourse = {};
        (window.graphData && graphData.nodes || []).forEach(function(n) {
            if (n && n.type === '课程' && n.properties && n.properties['学分'] != null && String(n.properties['学分']).trim() !== '') {
                creditByCourse[String(n.name || '').replace(/\s+/g, '')] = String(n.properties['学分']).trim();
            }
        });
        var pushCourse = function(sem, disp, branch) {
            disp = String(disp || '').trim();
            if (!disp) return;
            if (m4IsEnglish(disp)) return;
            var _pCat = m4CatOf(disp, branch);
            if (_pCat && !aiFilterOn[_pCat]) return;
            var m = disp.match(/《([^》]+)》/);
            var key = m ? m[1] : disp.replace(/\s+/g, '');
            if (key) {
                if (courseSeen[key]) return;
                courseSeen[key] = 1;
            }
            var meta = parseCourseText(disp);
            if (!meta.credit) {
                var _cr = creditByCourse[String(meta.name || '').replace(/\s+/g, '')];
                if (_cr) meta.credit = _cr;
            }
            meta.branch = String(branch || '').trim();
            meta.cat = _pCat || M4_BRANCH_CAT[meta.branch] || '';
            if (!meta.type) meta.type = meta.branch === '专业必修课' ? '必修' : ((meta.branch === '专业选修课' || meta.branch === '选修课') ? '选修' : '');
            meta.sem = sem;
            meta.ci = M4_COURSE_INDEX.length;
            M4_COURSE_INDEX.push(meta);
            (courseBySem[sem] = courseBySem[sem] || []).push({ disp: disp, ci: meta.ci });
        };
        Object.keys(mmCourseBySem).forEach(function(sem) {
            mmCourseBySem[sem].forEach(function(x) { pushCourse(sem, x.d, x.b); });
        });
        plan.forEach(function(p) {
            if (String(p.tag || '').trim() !== 'course') return;
            var sem = String(p.stage || '').trim() || '未标注';
            var t0 = String(p.text || '').trim();
            var hasStar = t0.indexOf('★') >= 0;
            var disp = slimCourseText(t0);
            if (hasStar && disp.indexOf('★') < 0) disp += '★建议优先';
            pushCourse(sem, disp);
        });
        var csems = AI_SEM_ORDER.filter(function(s) { return courseBySem[s]; });
        Object.keys(courseBySem).forEach(function(s) { if (AI_SEM_ORDER.indexOf(s) < 0) csems.push(s); });
        var renderCourseSems = function(semList) {
            html += '<div class="ai-plan-timeline">';
            semList.forEach(function(sem) {
                html += '<div class="ai-sem-row"><div class="ai-sem">' + escapeHtml(sem) + '</div><div class="ai-sem-items">';
                (courseBySem[sem] || []).forEach(function(ce) {
                    if (!ce || !ce.disp) return;
                    var meta = M4_COURSE_INDEX[ce.ci] || {};
                    var base = String(ce.disp).replace(/★建议优先/g, '').replace(/^(必修|选修|通识)/, '').replace(/[《》]/g, '').trim();
                    var cr = meta.credit || '';
                    if (cr) base = base.replace(/[（(]\s*\d+(?:\.\d+)?\s*学分[）)]/, '').trim();
                    html += '<div class="ai-sem-item ai-course-item ai-course-click" data-ci="' + ce.ci + '" onclick="openCourseDetail(this)" title="点击查看详情与智能分析">' +
                        '<span class="ai-course-name">' + renderRefsAI(base) + '</span>' +
                        (cr ? '<span class="ai-course-credit">' + escapeHtml(String(cr)) + '学分</span>' : '') +
                        (String(ce.disp).indexOf('★') >= 0 ? '<span class="ai-mm-star">★建议优先</span>' : '') + '</div>';
                });
                html += '</div></div>';
            });
            html += '</div>';
        };
        html += '<div class="ai-plan-left"><div class="ai-plan-col-label">课程主线<span>（' + (mmHasCourses ? '官方课程 + 个性化培养' : 'L1 官方课程') + ' · 按学期）</span></div>';
        mmRecNotes.forEach(function(rec) {
            html += '<div class="ai-mm-rec" style="margin:0 0 10px;">选课策略（AI建议）：' + renderRefsAI(rec) + '</div>';
        });
        if (csems.length === 0) {
            html += '<div class="ai-plan-empty">本维度暂无课程条目</div>';
        } else {
            ['大一', '大二', '大三', '大四'].forEach(function(yr) {
                var yearSems = csems.filter(function(s) { return s.indexOf(yr) === 0; });
                if (yearSems.length === 0) return;
                html += '<div class="ai-year-head"><span class="ai-year">' + yr + '</span><span class="ai-year-line"></span></div>';
                renderCourseSems(yearSems);
            });
            var cOthers = csems.filter(function(s) {
                return ['大一', '大二', '大三', '大四'].every(function(yr) { return s.indexOf(yr) !== 0; });
            });
            if (cOthers.length > 0) {
                html += '<div class="ai-year-head"><span class="ai-year">其他</span><span class="ai-year-line"></span></div>';
                renderCourseSems(cOthers);
            }
        }
        html += '</div>';

        // ---- 右栏：成长时间轴画布（与左侧学年/学期路径对齐；竞赛/技能块可拖入拖动，点击块智能分析）----
        html += '<div class="ai-plan-right"><div class="ai-plan-col-label">成长时间轴画布<span>（竞赛/技能块可拖入学期格）</span></div>';
        // 每学年被动信息：核心目标 / AI建议 / 避坑提示（L3，渲染为建议行）
        var _yrInfo = {};
        ['大一', '大二', '大三', '大四', '其他'].forEach(function(yr) { _yrInfo[yr] = { goals: [], advices: [], pitfalls: [] }; });
        plan.forEach(function(p) {
            if (String(p.tag || '').trim() !== 'advice') return;
            var stage = String(p.stage || '').trim();
            var yr = '其他';
            ['大一', '大二', '大三', '大四'].forEach(function(y) { if (stage.indexOf(y) === 0) yr = y; });
            var t = String(p.text || '').trim();
            if (t) _yrInfo[yr].advices.push(t);
        });
        actions.forEach(function(x) {
            var stage = String((x || {}).stage || '').trim();
            var yr = '其他';
            ['大一', '大二', '大三', '大四'].forEach(function(y) { if (stage.indexOf(y) === 0) yr = y; });
            (((x || {}).goals) || []).forEach(function(t) { t = String(t || '').trim(); if (t) _yrInfo[yr].goals.push(t); });
            (((x || {}).pitfalls) || []).forEach(function(t) { t = String(t || '').trim(); if (t) _yrInfo[yr].pitfalls.push(t); });
        });
        // 画布分组：学年（按所选年级窗口）+ 学期格（优先与左栏 csems 对齐，无课程学年给标准学期兜底）
        var _gSel = (document.getElementById('aiGradeSelect') || {}).value || '';
        var _gIdx = ['大一', '大二', '大三', '大四'].indexOf(_gSel);
        var canvasGroups = [];
        (_gIdx >= 0 ? ['大一', '大二', '大三', '大四'].slice(_gIdx) : ['大一', '大二', '大三', '大四']).forEach(function(yr) {
            var sems = csems.filter(function(s) { return s.indexOf(yr) === 0; });
            if (sems.length === 0) sems = [yr + '·秋', yr + '·春'];
            canvasGroups.push({ yr: yr, sems: sems });
        });
        var otherSems = csems.filter(function(s) {
            return ['大一', '大二', '大三', '大四'].every(function(yr) { return s.indexOf(yr) !== 0; });
        });
        // 计划派生块合并进画布状态（已被用户拖动/移除过的不重复生成）
        var _normKey = function(kind, name) { return kind + '|' + String(name || '').replace(/\s+/g, ''); };
        var _yearDefaultSem = {};
        canvasGroups.forEach(function(g) { _yearDefaultSem[g.yr] = g.sems[0]; });
        _yearDefaultSem['其他'] = otherSems.length > 0 ? otherSems[0] : '其他·未标注';
        var _mergePlanBlock = function(sem, kind, name) {
            var key = _normKey(kind, name);
            var hit = null;
            for (var i = 0; i < M4_CANVAS_STATE.blocks.length; i++) {
                if (_normKey(M4_CANVAS_STATE.blocks[i].kind, M4_CANVAS_STATE.blocks[i].name) === key) { hit = M4_CANVAS_STATE.blocks[i]; break; }
            }
            if (hit) { if (!hit.sem) hit.sem = sem; return; }
            M4_CANVAS_STATE.blocks.push({ kind: kind, name: name, sem: sem, fromPlan: true });
        };
        plan.forEach(function(p) {
            var tag = String(p.tag || '').trim();
            if (tag !== 'skill' && tag !== 'comp') return;
            var stage = String(p.stage || '').trim();
            var yr = '其他';
            ['大一', '大二', '大三', '大四'].forEach(function(y) { if (stage.indexOf(y) === 0) yr = y; });
            var sem = AI_SEM_ORDER.indexOf(stage) >= 0 ? stage : (_yearDefaultSem[yr] || '其他·未标注');
            var t = String(p.text || '').trim();
            if (t) _mergePlanBlock(sem, tag, t);
        });
        actions.forEach(function(x) {
            var stage = String((x || {}).stage || '').trim();
            var yr = '其他';
            ['大一', '大二', '大三', '大四'].forEach(function(y) { if (stage.indexOf(y) === 0) yr = y; });
            var dsem = _yearDefaultSem[yr] || '其他·未标注';
            (((x || {}).comps) || []).forEach(function(t) { t = String(t || '').trim(); if (t) _mergePlanBlock(dsem, 'comp', t); });
        });
        Object.keys(mmCompByYear).forEach(function(yr) {
            var dsem = _yearDefaultSem[yr] || '其他·未标注';
            (mmCompByYear[yr] || []).forEach(function(t) { t = String(t || '').trim(); if (t) _mergePlanBlock(dsem, 'comp', t); });
        });
        // 游离学期归位：不落在当前画布任何学期格的块 → 其他·未标注
        M4_CANVAS_STATE.blocks.forEach(function(b) {
            if (b.off) return;
            var sem = String(b.sem || '');
            var known = sem && (canvasGroups.some(function(g) { return g.sems.indexOf(sem) >= 0; }) || otherSems.indexOf(sem) >= 0);
            if (!known) b.sem = '其他·未标注';
        });
        var _blocksOf = function(sem) {
            return M4_CANVAS_STATE.blocks.filter(function(b) { return !b.off && (b.sem || '') === sem; });
        };
        var _renderCanvasBlocks = function(sem) {
            var out = '';
            _blocksOf(sem).forEach(function(b) {
                out += '<div class="ai-canvas-block ai-canvas-' + b.kind + '" draggable="true" data-kind="' + b.kind + '" data-name="' + escapeHtml(b.name) + '" data-src="canvas" ondragstart="aiBlockDragStart(event)" ondragend="aiBlockDragEnd(event)" onclick="openSmartAnalysis(this.getAttribute(\'data-kind\'), this.getAttribute(\'data-name\'))" title="点击查看智能分析 · 可拖动到其他学期 · 拖回右侧工具区可移除">' +
                    '<span class="ai-canvas-kind">' + (b.kind === 'comp' ? '赛' : (b.kind === 'other' ? '他' : '技')) + '</span>' +
                    '<span class="ai-canvas-text">' + renderRefsAI(b.name) + '</span>' +
                    '<span class="ai-canvas-x" draggable="false" onclick="aiBlockRemove(event, this)" title="从画布移除">×</span></div>';
            });
            return out;
        };
        var renderCanvasGroup = function(yr, sems) {
            html += '<div class="ai-year-head"><span class="ai-year">' + yr + '</span><span class="ai-year-line"></span></div>';
            html += '<div class="ai-plan-timeline">';
            sems.forEach(function(sem) {
                var bl = _blocksOf(sem);
                html += '<div class="ai-sem-row"><div class="ai-sem">' + escapeHtml(sem) + '</div>' +
                    '<div class="ai-sem-items ai-canvas-drop" data-sem="' + escapeHtml(sem) + '" ondragover="aiCanvasDragOver(event)" ondragleave="aiCanvasDragLeave(event)" ondrop="aiCanvasDrop(event)">' +
                    _renderCanvasBlocks(sem) +
                    (bl.length === 0 ? '<div class="ai-canvas-hint">拖入竞赛/技能块</div>' : '') +
                    '</div></div>';
            });
            var info = _yrInfo[yr] || { goals: [], advices: [], pitfalls: [] };
            var infoItems = [];
            info.goals.forEach(function(t) { infoItems.push('<div class="ai-sem-item ai-item-advice"><b>【目标】</b>' + renderRefsAI(t) + '</div>'); });
            info.advices.forEach(function(t) { infoItems.push('<div class="ai-sem-item ai-item-advice"><b>【建议】</b>' + renderRefsAI(t) + '</div>'); });
            info.pitfalls.forEach(function(t) { infoItems.push('<div class="ai-sem-item ai-item-advice"><b>【避坑】</b>' + renderRefsAI(t) + '</div>'); });
            html += '<div class="ai-sem-row"><div class="ai-sem">' + yr + '·建议</div><div class="ai-sem-items">' + (infoItems.join('') || '<div class="ai-canvas-hint">—</div>') + '</div></div>';
            html += '</div>';
        };
        canvasGroups.forEach(function(g) { renderCanvasGroup(g.yr, g.sems); });
        var hasOther = otherSems.length > 0 ||
            M4_CANVAS_STATE.blocks.some(function(b) { return !b.off && b.sem === '其他·未标注'; }) ||
            _yrInfo['其他'].goals.length + _yrInfo['其他'].advices.length + _yrInfo['其他'].pitfalls.length > 0;
        if (hasOther) {
            if (otherSems.length === 0) otherSems = ['其他·未标注'];
            renderCanvasGroup('其他', otherSems);
        }
        html += '</div>';

        html += '</div>';
    }

    // 表格
    if (a.table && a.table.headers && a.table.headers.length > 0) {
        html += '<div class="ai-section-title">' + escapeHtml(a.table.title || '数据汇总') + '</div>';
        html += '<div class="ai-table-wrap">';
        html += '<table class="ai-table"><thead><tr>';
        a.table.headers.forEach(function(h) {
            html += '<th>' + escapeHtml(h) + '</th>';
        });
        html += '</tr></thead><tbody>';
        if (a.table.rows) {
            a.table.rows.forEach(function(row) {
                html += '<tr>';
                row.forEach(function(cell) {
                    html += '<td>' + renderRefsAI(String(cell)) + '</td>';
                });
                html += '</tr>';
            });
        }
        html += '</tbody></table>';
        html += '</div>';
    }

    // ③ 差距与下一步（AI建议层）——分阶段行动清单已并入上方总览右栏
    var gap = a.gap_next || {};
    var gapStatus = String(gap.status || '').trim();
    var gapTop3 = (gap.top3 || []).filter(function(i) { return i && String(i).trim(); });
    var gapAlts = (gap.alternatives || []).filter(function(i) { return i && String(i).trim(); });
    if (gapStatus || gapTop3.length > 0 || gapAlts.length > 0) {
        html += '<div class="ai-section-title">差距与下一步<span class="ai-advice-badge">AI 建议 · 仅供参考</span></div>';
        html += '<div class="ai-gap-card">';
        if (gapStatus) html += '<div class="ai-gap-status">' + renderRefsAI(gapStatus) + '</div>';
        if (gapTop3.length > 0) {
            html += '<div class="ai-gap-label"><b>下一步 Top3</b></div>';
            html += '<ol class="ai-gap-top3">';
            gapTop3.forEach(function(it) {
                html += '<li>' + renderRefsAI(String(it).trim()) + '</li>';
            });
            html += '</ol>';
        }
        if (gapAlts.length > 0) {
            html += '<div class="ai-gap-alt"><b>备选岗位 / 方向：</b>' + renderRefsAI(gapAlts.join('；')) + '</div>';
        }
        html += '</div>';
    }

    // 数据来源（正文上标[Sn]的溯源目标，统一汇总于末尾）
    if (aiRefList.length > 0) {
        html += '<div class="ai-section-title" id="aiReportRefs">数据来源<span class="ai-fact-chip">正文上标 [Sn] 对应以下来源，统一汇总于此</span></div>';
        html += '<div class="ai-ref-list"><ol>';
        aiRefList.forEach(function(src, i) {
            html += '<li>[S' + (i + 1) + ']　' + escapeHtml(src) + '</li>';
        });
        html += '</ol></div>';
    }

    body.innerHTML = html;
    // M4模式：显示工具区拖拽源模块（技能/竞赛推荐）；其他模式隐藏
    if (a._mode === 'm4') renderAiSrcDock(); else hideAiSrcDock();
    // M5附加：异步加载岗位分类总览（LLM生成，点击岗位联动右侧工具栏展示发展图）
    if (cpMajor) loadJobOverview(cpMajor);
    var fGroup = document.getElementById('aiM4Filters');
    if (fGroup) fGroup.addEventListener('change', function(e) {
        var cb = e.target;
        if (!cb || !cb.classList || !cb.classList.contains('ai-m4-cat')) return;
        aiFilterOn[cb.getAttribute('data-cat')] = cb.checked;
        showAIResult();
    });
    titleEl.textContent = a.title || '智能分析报告';
    // 隐藏图谱，显示报告面板
    document.getElementById('graphChart').style.display = 'none';
    document.querySelector('.legend').style.display = 'none';
    panel.style.display = 'flex';
    // 高亮报告按钮
    document.getElementById('aiResultBtn').style.fontWeight = '700';
    document.getElementById('graphViewBtn').style.fontWeight = '400';
}

function showGraphView() {
    // 隐藏报告/溯源/画像面板，显示图谱
    document.getElementById('aiResultPanel').style.display = 'none';
    var sp = document.getElementById('sourcesPanel');
    if (sp) sp.style.display = 'none';
    var pp = document.getElementById('profilePanel');
    if (pp) pp.style.display = 'none';
    document.getElementById('graphChart').style.display = 'block';
    document.querySelector('.legend').style.display = 'flex';
    // 高亮图谱按钮
    document.getElementById('aiResultBtn').style.fontWeight = '400';
    document.getElementById('graphViewBtn').style.fontWeight = '700';
    // 刷新图谱尺寸
    if (chart) {
        setTimeout(function() { chart.resize(); }, 100);
    }
}

// ===== 来源溯源（P4 双层接地） =====
let lastSourcesData = null;

async function toggleSourcesPanel() {
    var panel = document.getElementById('sourcesPanel');
    if (!panel) return;
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        return;
    }
    panel.style.display = 'flex';
    document.getElementById('aiResultPanel').style.display = 'none';
    if (lastSourcesData) {
        renderSourcesList();
        return;
    }
    await loadSources();
}

// ===== 我的画像（r6f：点亮已会技能 + AI差距分析对照） =====
var profileCache = { grade: '', major: '', target_jobs: [], target_cities: [], known_skills: [] };

function loadProfile(cb) {
    fetch('/api/profile').then(function(r) { return r.json(); }).then(function(d) {
        if (d.success && d.profile) {
            profileCache = d.profile;
            updateProfileBadge();
            if (cb) cb();
        }
    }).catch(function(e) { console.warn('画像加载失败', e); });
}

function updateProfileBadge() {
    var badge = document.getElementById('profileKnownCount');
    if (!badge) return;
    var n = (profileCache.known_skills || []).length;
    badge.textContent = n;
    badge.style.display = n > 0 ? 'inline-flex' : 'none';
}

// r8: 窄屏侧边栏抽屉开关
function toggleSidebar() {
    var sidebar = document.querySelector('.sidebar');
    var backdrop = document.querySelector('.sidebar-backdrop');
    if (!sidebar || !backdrop) return;
    var opened = sidebar.classList.toggle('open');
    backdrop.classList.toggle('show', opened);
}

function closeSidebar() {
    var sidebar = document.querySelector('.sidebar');
    var backdrop = document.querySelector('.sidebar-backdrop');
    if (sidebar) sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('show');
}

function toggleProfilePanel() {
    var panel = document.getElementById('profilePanel');
    if (!panel) return;
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        return;
    }
    panel.style.display = 'flex';
    document.getElementById('aiResultPanel').style.display = 'none';
    var sp = document.getElementById('sourcesPanel');
    if (sp) sp.style.display = 'none';
    var body = document.getElementById('profileBody');
    if (body) body.innerHTML = '<p style="color:var(--text-secondary)">正在加载画像…</p>';
    loadProfile(renderProfilePanel);
}

function renderProfilePanel() {
    var body = document.getElementById('profileBody');
    if (!body) return;
    var skills = profileCache.known_skills || [];
    var jobs = Array.isArray(profileCache.target_jobs) ? profileCache.target_jobs : [];
    var cities = Array.isArray(profileCache.target_cities) ? profileCache.target_cities : [];

    var html = '<div class="profile-overview">';
    // r15：专业/年级/意向未设置时显示问号占位（与意向岗位风格一致），不再留空或写死默认值
    function metaVal(v, ph) {
        return v ? escapeHtml(v) : '<span class="profile-meta-ph">' + ph + '</span>';
    }
    var metaParts = [];
    metaParts.push('专业：' + metaVal(profileCache.major, '?????'));
    metaParts.push('年级：' + metaVal(profileCache.grade, '??'));
    metaParts.push('意向岗位：' + (jobs.length ? jobs.map(escapeHtml).join('、') : '<span class="profile-meta-ph">???????</span>'));
    if (cities.length) metaParts.push('意向城市：' + cities.map(escapeHtml).join('、'));
    html += '<div class="profile-meta-row">';
    html += '<div class="profile-meta">' + metaParts.join('<span class="profile-meta-sep">·</span>') + '</div>';
    html += '<button class="btn-profile-edit" onclick="toggleProfileEdit()">编辑画像</button>';
    html += '</div>';
    html += '<div class="profile-stat"><strong>' + skills.length + '</strong> 项已点亮掌握</div>';
    html += '</div>';

    html += '<div id="profileEditForm" class="profile-edit-form" style="display:none">';
    html += '<div class="pe-row"><label>专业</label><input type="text" id="peMajor" value="' + escapeHtml(profileCache.major || '') + '" placeholder="支持任意专业，如：电气工程及其自动化"></div>';
    html += '<div class="pe-row"><label>年级</label><select id="peGrade">' + peGradeOptions(profileCache.grade || '') + '</select></div>';
    html += '<div class="pe-row"><label>意向岗位</label><input type="text" id="peJobs" value="' + escapeHtml(jobs.join('、')) + '" placeholder="多个岗位用、分隔"></div>';
    html += '<div class="pe-row"><label>意向城市</label><input type="text" id="peCities" value="' + escapeHtml(cities.join('、')) + '" placeholder="多个城市用、分隔"></div>';
    html += '<div class="pe-actions"><button class="btn-pe-save" onclick="saveProfileEdit()">保存</button><button class="btn-pe-cancel" onclick="toggleProfileEdit()">取消</button></div>';
    html += '</div>';

    if (skills.length === 0) {
        html += '<div class="profile-empty">还没有点亮任何已会技能。<br>在图谱中点击技能/课程/竞赛节点，点右侧信息栏的「点亮已会」按钮即可标记；AI 分析报告的能力差距部分将自动对照你的掌握情况。</div>';
    } else {
        html += '<div class="profile-skills-list">';
        skills.forEach(function(s) {
            var tagColor = nodeColors[s.type] || 'var(--accent-cyan)';
            html += '<span class="profile-skill-tag" style="border-color:' + tagColor + '55;color:' + tagColor + '">' +
                escapeHtml(s.name || s.id) +
                '<a class="profile-skill-remove" title="从画像移除" onclick="removeKnownSkill(\'' + escapeHtml(String(s.id).replace(/'/g, "\\'")) + '\')">×</a>' +
                '</span>';
        });
        html += '</div>';
        html += '<div class="profile-actions-row"><button class="btn-clear-profile" onclick="clearKnownSkills()">清空全部点亮</button></div>';
    }

    // 个性培养布局（m4 报告的类目过滤 + 时间轴画布块，可在此编辑保存）
    var m4l = profileCache.m4_layout || {};
    var m4fCount = Array.isArray(m4l.filters) ? m4l.filters.length : 0;
    var m4bCount = Array.isArray(m4l.canvas) ? m4l.canvas.length : 0;
    html += '<div class="profile-m4-sec">';
    html += '<div class="profile-m4-head"><span class="profile-m4-title">个性培养布局</span>' +
        '<span class="profile-m4-sum">' + (m4fCount || m4bCount ? '类目显示 ' + m4fCount + ' 项 · 画布块 ' + m4bCount + ' 个' : '尚未定制，报告中使用默认布局') + '</span>' +
        '<button class="btn-profile-edit" onclick="toggleM4Edit()">编辑布局</button></div>';
    html += '<div id="peM4Editor" style="display:none"></div>';
    html += '</div>';

    html += '<div class="profile-note">点亮清单会作为「已会」依据随 AI 分析请求提交：报告中能力差距不再列出已点亮技能，待补项围绕未点亮的关键技能/课程/竞赛展开。</div>';
    body.innerHTML = html;
}

function peGradeOptions(selected) {
    var opts = ['未选择', '大一', '大二', '大三', '大四'];
    return opts.map(function(g) {
        var v = g === '未选择' ? '' : g;
        return '<option value="' + v + '"' + (v === selected ? ' selected' : '') + '>' + g + '</option>';
    }).join('');
}

function toggleProfileEdit() {
    var form = document.getElementById('profileEditForm');
    if (form) form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

function saveProfileEdit() {
    var splitList = function(v) {
        return (v || '').split(/[、,，;；\s]+/).map(function(s) { return s.trim(); }).filter(function(s) { return s; });
    };
    fetch('/api/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            major: (document.getElementById('peMajor').value || '').trim(),
            grade: (document.getElementById('peGrade').value || '').trim(),
            target_jobs: splitList(document.getElementById('peJobs').value),
            target_cities: splitList(document.getElementById('peCities').value)
        })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            profileCache = d.profile;
            updateProfileBadge();
            renderProfilePanel();
            showToast('画像已保存，AI 报告将自动带入最新画像', 'success');
        } else {
            showToast(d.error || '保存失败', 'error');
        }
    }).catch(function(e) { showToast('保存失败: ' + e.message, 'error'); });
}

function closeGuideBar() {
    var bar = document.getElementById('guideBar');
    if (bar) bar.style.display = 'none';
}

function removeKnownSkill(nodeId) {
    var s = (profileCache.known_skills || []).find(function(x) { return x.id === nodeId; });
    fetch('/api/profile/skill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: nodeId, name: s ? s.name : '', type: s ? s.type : '' })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            profileCache = d.profile;
            updateProfileBadge();
            renderProfilePanel();
            showToast('已从画像移除「' + (s ? s.name : nodeId) + '」', 'success');
        } else {
            showToast(d.error || '操作失败', 'error');
        }
    }).catch(function(e) { showToast('操作失败: ' + e.message, 'error'); });
}

function clearKnownSkills() {
    if (!confirm('确定清空全部已点亮技能吗？')) return;
    fetch('/api/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ known_skills: [] })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            profileCache = d.profile;
            updateProfileBadge();
            renderProfilePanel();
            showToast('已清空点亮清单', 'success');
        } else {
            showToast(d.error || '操作失败', 'error');
        }
    }).catch(function(e) { showToast('操作失败: ' + e.message, 'error'); });
}

// ===== 画像：个性培养布局编辑（与 m4 报告画布同款数据结构） =====
var PE_M4 = null;

function peM4Load() {
    var m4 = profileCache.m4_layout || {};
    var fs = Array.isArray(m4.filters) ? m4.filters : [];
    var bs = Array.isArray(m4.canvas) ? m4.canvas : [];
    PE_M4 = {
        filters: M4_FILTER_CATS.filter(function(c) { return fs.indexOf(c) >= 0; }),
        blocks: bs.filter(function(b) { return b && String(b.name || '').trim(); }).map(function(b) {
            return { kind: b.kind === 'comp' ? 'comp' : 'skill', name: String(b.name).trim(), sem: String(b.sem || '').trim() || '其他·未标注' };
        })
    };
}

function toggleM4Edit() {
    var form = document.getElementById('peM4Editor');
    if (!form) return;
    if (form.style.display === 'none') {
        peM4Load();
        peM4Render();
        form.style.display = 'block';
    } else {
        form.style.display = 'none';
    }
}

function peM4SemList() {
    var extras = [];
    (PE_M4.blocks || []).forEach(function(b) {
        if (b.sem && AI_SEM_ORDER.indexOf(b.sem) < 0 && extras.indexOf(b.sem) < 0) extras.push(b.sem);
    });
    return AI_SEM_ORDER.concat(extras);
}

function peM4Render() {
    var box = document.getElementById('peM4Editor');
    if (!box || !PE_M4) return;
    var html = '<div class="pe-m4-label">通识类课程显示（点亮 = 报告中显示，默认全部隐藏）</div><div class="pe-m4-filters">';
    M4_FILTER_CATS.forEach(function(c) {
        var on = PE_M4.filters.indexOf(c) >= 0;
        html += '<span class="pe-m4-chip' + (on ? ' on' : '') + '" onclick="peM4ToggleFilter(\'' + c + '\')">' + (on ? '✓ ' : '') + escapeHtml(c) + '</span>';
    });
    html += '</div>';
    html += '<div class="pe-m4-label">时间轴画布块（拖动块可调整学期，×移除）</div><div class="pe-m4-canvas">';
    if (!PE_M4.blocks.length) {
        html += '<div class="pe-m4-zone-hint" style="display:block;padding:8px 0">暂无画布块，可在下方添加（技能/竞赛）</div>';
    } else {
        var groups = {};
        PE_M4.blocks.forEach(function(b, gi) {
            var k = b.sem || '其他·未标注';
            (groups[k] = groups[k] || []).push({ b: b, idx: gi });
        });
        peM4SemList().forEach(function(sem) {
            var bl = groups[sem] || [];
            html += '<div class="pe-m4-sem"><span class="pe-m4-sem-name">' + escapeHtml(sem) + '</span>' +
                '<div class="pe-m4-zone" data-sem="' + escapeHtml(sem) + '" ondragover="peM4ZoneDragOver(event)" ondragleave="peM4ZoneDragLeave(event)" ondrop="peM4ZoneDrop(event)">' +
                bl.map(function(x) {
                    return '<div class="ai-canvas-block ai-canvas-' + x.b.kind + '" draggable="true" ondragstart="peM4DragStart(event,' + x.idx + ')" ondragend="aiBlockDragEnd(event)">' +
                        '<span class="ai-canvas-kind">' + (x.b.kind === 'comp' ? '赛' : '技') + '</span>' +
                        '<span class="ai-canvas-text">' + escapeHtml(x.b.name) + '</span>' +
                        '<span class="ai-canvas-x" onclick="peM4RemoveBlock(' + x.idx + ')" title="移除">×</span></div>';
                }).join('') +
                (bl.length ? '' : '<span class="pe-m4-zone-hint">拖到此处</span>') + '</div></div>';
        });
    }
    html += '</div>';
    html += '<div class="pe-m4-label">添加画布块</div>';
    html += '<div class="pe-m4-add"><select id="peM4NewKind"><option value="skill">技能</option><option value="comp">竞赛</option></select>' +
        '<input type="text" id="peM4NewName" placeholder="名称，如：Python / 蓝桥杯">' +
        '<select id="peM4NewSem">' + peM4SemList().map(function(s) { return '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + '</option>'; }).join('') + '</select>' +
        '<button class="btn-pe-m4-add" onclick="peM4AddBlock()">添加</button></div>';
    html += '<div class="pe-actions"><button class="btn-pe-save" onclick="peM4Save()">保存布局</button><button class="btn-pe-cancel" onclick="toggleM4Edit()">取消</button></div>';
    box.innerHTML = html;
}

function peM4ToggleFilter(cat) {
    if (!PE_M4) return;
    var i = PE_M4.filters.indexOf(cat);
    if (i >= 0) PE_M4.filters.splice(i, 1); else PE_M4.filters.push(cat);
    peM4Render();
}

function peM4DragStart(e, idx) {
    var b = (PE_M4.blocks || [])[idx];
    if (!b) return;
    _aiDragPayload = { kind: b.kind, name: b.name, src: 'pe' };
    try { e.dataTransfer.setData('text/plain', JSON.stringify(_aiDragPayload)); } catch (err) {}
    e.dataTransfer.effectAllowed = 'move';
    if (e.currentTarget && e.currentTarget.classList) e.currentTarget.classList.add('ai-dragging');
}

function peM4ZoneDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (e.currentTarget) e.currentTarget.classList.add('ai-drop-hover');
}

function peM4ZoneDragLeave(e) {
    if (e.currentTarget) e.currentTarget.classList.remove('ai-drop-hover');
}

function peM4ZoneDrop(e) {
    e.preventDefault();
    if (e.currentTarget) e.currentTarget.classList.remove('ai-drop-hover');
    var p = aiParseDragPayload(e);
    if (!p || !String(p.name || '').trim()) return;
    var sem = e.currentTarget.getAttribute('data-sem') || '';
    var kind = p.kind === 'comp' ? 'comp' : 'skill';
    var nm = String(p.name).trim();
    var hit = PE_M4.blocks.find(function(b) { return b.kind === kind && b.name === nm; });
    if (hit) hit.sem = sem; else PE_M4.blocks.push({ kind: kind, name: nm, sem: sem });
    peM4Render();
}

function peM4RemoveBlock(idx) {
    if (!PE_M4 || !(PE_M4.blocks || [])[idx]) return;
    PE_M4.blocks.splice(idx, 1);
    peM4Render();
}

function peM4AddBlock() {
    if (!PE_M4) return;
    var kindEl = document.getElementById('peM4NewKind');
    var nameEl = document.getElementById('peM4NewName');
    var semEl = document.getElementById('peM4NewSem');
    var kind = kindEl && kindEl.value === 'comp' ? 'comp' : 'skill';
    var nm = ((nameEl && nameEl.value) || '').trim();
    var sem = ((semEl && semEl.value) || '').trim();
    if (!nm) { showToast('请输入技能或竞赛名称', 'error'); return; }
    var hit = PE_M4.blocks.find(function(b) { return b.kind === kind && b.name === nm; });
    if (hit) { hit.sem = sem; showToast('「' + nm + '」已存在，已移动到 ' + sem, 'info'); }
    else { PE_M4.blocks.push({ kind: kind, name: nm, sem: sem }); showToast('已添加「' + nm + '」', 'success'); }
    peM4Render();
}

async function peM4Save() {
    if (!PE_M4) return;
    var payload = {
        filters: PE_M4.filters.slice(),
        canvas: PE_M4.blocks.map(function(b) { return { kind: b.kind, name: b.name, sem: b.sem }; }),
        custom: M4_CUSTOM_BLOCKS.slice()
    };
    try {
        var res = await fetch('/api/profile', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ m4_layout: payload })
        });
        var d = await res.json();
        if (d.success) {
            profileCache = d.profile;
            // 同步内存态：下次打开/生成 m4 报告直接生效
            M4_FILTER_CATS.forEach(function(c) { aiFilterOn[c] = payload.filters.indexOf(c) >= 0; });
            M4_CANVAS_STATE.blocks = payload.canvas.map(function(b) {
                return { kind: b.kind, name: b.name, sem: b.sem, fromPlan: false };
            });
            m4CanvasRestored = true;
            aiFilterRestored = true;
            renderProfilePanel();
            showToast('个性培养布局已保存，报告将按此展示', 'success');
        } else {
            showToast(d.error || '保存失败', 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

function toggleKnownSkill() {
    if (!selectedNode) return;
    var node = selectedNode;
    fetch('/api/profile/skill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: node.id, name: node.name, type: node.type })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            profileCache = d.profile;
            updateProfileBadge();
            var isKnown = (profileCache.known_skills || []).some(function(s) { return s.id === node.id; });
            var btn = document.getElementById('btnMarkKnown');
            var txt = document.getElementById('markKnownText');
            if (btn) btn.classList.toggle('is-known', isKnown);
            if (txt) txt.textContent = isKnown ? '取消点亮' : '点亮已会';
            showToast(isKnown ? '已点亮「' + node.name + '」为已会技能' : '已取消点亮「' + node.name + '」', 'success');
        } else {
            showToast(d.error || '操作失败', 'error');
        }
    }).catch(function(e) { showToast('操作失败: ' + e.message, 'error'); });
}

async function loadSources() {
    var listEl = document.getElementById('sourcesList');
    if (!listEl) return;
    if (!lastSearchData || !lastSearchData.nodes || lastSearchData.nodes.length === 0) {
        listEl.innerHTML = '<p style="color:var(--text-secondary)">请先进行综合检索或目录探索，再查看来源溯源。</p>';
        return;
    }
    var q = '';
    if (lastSearchData.center_id) {
        q = 'center=' + encodeURIComponent(lastSearchData.center_id);
    } else {
        q = 'keyword=' + encodeURIComponent(lastSearchData.keyword);
    }
    q += '&depth=' + (lastSearchData.depth || 1) + '&dim=' + (lastSearchData.dim || 'all');
    listEl.innerHTML = '<p style="color:var(--text-secondary)">正在加载来源数据…</p>';
    try {
        var res = await fetch('/api/sources?' + q);
        var data = await res.json();
        if (!res.ok) {
            listEl.innerHTML = '<p style="color:var(--accent-red)">' + escapeHtml(data.error || '来源查询失败') + '</p>';
            return;
        }
        lastSourcesData = data;
        renderSourcesList();
    } catch (e) {
        listEl.innerHTML = '<p style="color:var(--accent-red)">来源查询失败: ' + escapeHtml(e.message) + '</p>';
    }
}

function renderSourcesList() {
    var listEl = document.getElementById('sourcesList');
    var data = lastSourcesData;
    if (!data || !data.sources || data.sources.length === 0) {
        listEl.innerHTML = '<p style="color:var(--text-secondary)">当前子图暂无来源标注数据。</p>';
        return;
    }
    var html = '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:14px">当前子图共 <b style="color:var(--accent-cyan)">' + data.sources.length +
        '</b> 个来源，覆盖 <b>' + (data.node_count || 0) + '</b> 个节点。点击文档名或页码可在新窗口定位原文（PDF 将打开对应页）。</p>';
    data.sources.forEach(function(s, idx) {
        var isUrl = s.type === 'url';
        html += '<div class="source-item">';
        html += '<div class="source-item-head">';
        if (isUrl) {
            html += '<a class="source-name" href="' + escapeHtml(s.name) + '" target="_blank" rel="noopener">' + escapeHtml(s.name) + '</a>';
        } else {
            html += '<a class="source-name" href="javascript:void(0)" onclick="openSource(' + idx + ', \'\')">' + escapeHtml(s.name) + '</a>';
        }
        html += '<span class="source-type-badge">' + (isUrl ? '网页' : '文档') + '</span>';
        html += '<span class="source-edge-count">支撑 ' + (s.edge_count || 0) + ' 条关系</span>';
        html += '</div>';
        if (!isUrl && s.pages && s.pages.length) {
            html += '<div class="source-pages">' + s.pages.map(function(p) {
                var pg = String(p === undefined || p === null ? '' : p).trim().replace(/[^0-9Pp\-~/]/g, '');
                if (!pg) return '';
                return '<span class="source-page-chip" onclick="openSource(' + idx + ', \'' + pg + '\')">P' + escapeHtml(pg) + '</span>';
            }).join('') + '</div>';
        }
        if (isUrl && s.urls && s.urls.length) {
            html += '<div class="source-pages">' + s.urls.map(function(u) {
                return '<a class="source-page-chip" href="' + escapeHtml(u) + '" target="_blank" rel="noopener">打开链接</a>';
            }).join('') + '</div>';
        }
        if (s.entities && s.entities.length) {
            html += '<div class="source-entities">关联实体：' + s.entities.map(escapeHtml).join('、') + '</div>';
        }
        html += '</div>';
    });
    listEl.innerHTML = html;
}

function openSource(idx, page) {
    var s = lastSourcesData && lastSourcesData.sources ? lastSourcesData.sources[idx] : null;
    if (!s) return;
    if (!s.name || s.name === '粘贴文本' || s.name === '粘贴的文本') {
        showToast('该来源为手动粘贴的文本材料，无本地原件可打开', 'info');
        return;
    }
    var url = '/api/source_file?name=' + encodeURIComponent(s.name);
    if (page) url += '#page=' + encodeURIComponent(page);
    window.open(url, '_blank');
}

// ===== 视图范围切换（文件夹/全局） =====
function switchViewScope(scope) {
    viewScope = scope;
    // 更新按钮状态
    var btnFolder = document.getElementById('scopeFolderBtn');
    var btnGlobal = document.getElementById('scopeGlobalBtn');
    if (btnFolder) btnFolder.classList.toggle('active', scope === 'folder');
    if (btnGlobal) btnGlobal.classList.toggle('active', scope === 'global');
    // 更新标题后缀
    var title = document.getElementById('graphTitle');
    var baseTitle = DIMENSION_TITLES[currentDimension] || currentDimension;
    title.textContent = scope === 'global' ? baseTitle + '（全局汇总）' : baseTitle;
    closeInfoPanel();
    loadDimension(currentDimension);
}

// ===== 重建全局库 =====
function rebuildGlobal() {
    if (!confirm('确定重建全局汇总库？将重新扫描所有文件夹的数据。')) return;
    showLoading(true);
    fetch(`/api/graph/${currentDimension}/rebuild-global`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast(`全局库重建成功：${data.folders}个文件夹，${data.nodes}个节点，${data.edges}条边`, 'success');
                if (viewScope === 'global') loadDimension(currentDimension);
            } else {
                showToast(data.error || '重建失败', 'error');
            }
            showLoading(false);
        })
        .catch(e => { showToast('重建失败: ' + e.message, 'error'); showLoading(false); });
}

// ===== 加载图谱数据 =====
async function loadDimension(dimension, opts) {
    var fromMenu = !!(opts && opts.fromMenu);
    showLoading(true);
    try {
        const scopeParam = viewScope === 'global' ? '?scope=global' : '';
        const res = await fetch(`/api/graph/${dimension}${scopeParam}`);
        if (!res.ok) {
            const err = await res.json();
            showToast(err.error || '加载失败', 'error');
            showLoading(false);
            return;
        }
        const data = await res.json();
        // 维度聚焦模式：只保留所属中心的节点（边保留，渲染层会自动过滤悬空边）
        if (dimensionCenter) {
            graphData = {
                nodes: (data.nodes || []).filter(function(n) {
                    return (HUB_TYPE_MAP[n.type] || 'skill') === dimensionCenter;
                }),
                edges: data.edges || [],
                headers: data.headers || [],
                raw_data: data.raw_data || []
            };
        } else {
            graphData = data;
        }
        hubFocus = null;
        clearTimeout(hubFocusTimer);
        // 仅用户主动切换维度时才调整布局：时序视图仅课程体系可用；首页初始化/视图范围切换保持当前布局
        if (fromMenu) {
            if (dimension !== 'course_system' && layoutMode === 'timeline') {
                layoutMode = 'force';
            }
            // 大数据集禁用力导向（数千节点会卡死页面），自动回落中心辐射视图
            if (layoutMode === 'force' && data.nodes.length > 1000) {
                layoutMode = 'hub';
                showToast('该维度节点较多，已自动切换为中心辐射视图', 'info');
            }
            // 力导向下维度聚焦同样生效（数据已过滤），提示当前为单中心视图
            if (dimensionCenter) {
                var centerName = (HUB_CENTERS[dimensionCenter] || {}).name || '';
                showToast('已聚焦' + centerName + '：仅显示该中心节点，综合检索可查看四中心全景', 'info');
            }
        }
        Object.keys(hubExpanded).forEach(k => { hubExpanded[k] = false; });
        // 单中心聚焦时自动展开该中心，直接呈现完整成员（上限 HUB_EXPAND_CAP）
        if (dimensionCenter && layoutMode === 'hub') {
            hubExpanded[dimensionCenter] = true;
        }
        Object.keys(nodeSizeCache).forEach(k => { delete nodeSizeCache[k]; });
        try {
            renderGraph();
        } catch (renderErr) {
            console.error('渲染错误:', renderErr);
            showToast('图谱渲染错误: ' + renderErr.message, 'error');
        }
        updateStats();
        updateLegend();
        // 同步标题（菜单切换维度 / 视图范围变化后保持一致）
        var titleEl = document.getElementById('graphTitle');
        if (titleEl) {
            var baseTitle = DIMENSION_TITLES[dimension] || dimension;
            titleEl.textContent = viewScope === 'global' ? baseTitle + '（全局汇总）' : baseTitle;
        }
    } catch (e) {
        console.error('加载错误:', e);
        showToast('加载图谱数据失败: ' + e.message, 'error');
    }
    showLoading(false);
}

// ===== 渲染图谱 =====
function renderGraph() {
    if (!graphData.nodes || graphData.nodes.length === 0) return;
    if (!chart) return;
    if (layoutMode === 'hub') {
        renderHubGraph();
    } else if (layoutMode === 'timeline') {
        renderTimelineGraph();
    } else {
        renderForceGraphBody();
    }
}

// 力导向全图（兜底视图：显示全部节点与全部关系）
function renderForceGraphBody() {
    currentRenderLinks = [];

    // 构建类别数组，节点类型 -> 索引映射
    const nodeTypes = [...new Set(graphData.nodes.map(n => n.type))];
    const categories = nodeTypes.map(type => ({
        name: type,
        itemStyle: { color: nodeColors[type] || '#888' }
    }));
    const typeIndexMap = {};
    nodeTypes.forEach((type, idx) => { typeIndexMap[type] = idx; });

    const nodes = graphData.nodes.map(node => {
        const color = nodeColors[node.type] || '#888';
        return {
            id: node.id,
            name: node.name,
            category: typeIndexMap[node.type] !== undefined ? typeIndexMap[node.type] : 0,
            symbolSize: getNodeSize(node),
            itemStyle: {
                color: color,
                shadowBlur: 10,
                shadowColor: color,
                borderColor: color,
                borderWidth: 2,
                opacity: 0.9
            },
            label: {
                show: true,
                position: 'bottom',
                color: '#1a202c',
                fontSize: 11,
                fontWeight: 500
            },
            nodeData: node
        };
    });

    // 构建边数据
    const edges = graphData.edges.map((edge, idx) => {
        const edgeColor = getEdgeColor(edge);
        // 有向边关系白名单：这些关系显示箭头，其余（如技能重叠）无向
        var isDirected = DIRECTED_RELATIONS.indexOf(edge.relation) !== -1;
        // 岗位与岗位之间的边（技能重叠）始终无向
        var sourceNode = graphData.nodes.find(function(n) { return n.id === edge.source; });
        var targetNode = graphData.nodes.find(function(n) { return n.id === edge.target; });
        if (sourceNode && targetNode && sourceNode.type === '岗位' && targetNode.type === '岗位') {
            isDirected = false;
        }
        return {
            source: edge.source,
            target: edge.target,
            edgeIndex: idx,
            edgeData: edge,
            // per-link 使用 symbol/symbolSize（非 edgeSymbol/edgeSymbolSize）
            // 有向边显示箭头，无向边不显示箭头
            symbol: isDirected ? ['none', 'arrow'] : ['none', 'none'],
            symbolSize: isDirected ? [0, 10] : [0, 0],
            label: {
                show: graphData.edges.length < 80,
                formatter: edge.relation,
                fontSize: 10,
                color: edgeColor,
                backgroundColor: 'rgba(255, 255, 255, 0.9)',
                padding: [2, 5],
                borderRadius: 3
            },
            lineStyle: {
                color: edgeColor,
                width: 2,
                curveness: 0.2,
                opacity: 0.7
            }
        };
    });

    const option = {
        backgroundColor: 'transparent',
        tooltip: {
            formatter: function(params) {
                if (params.dataType === 'node') {
                    const nd = params.data.nodeData || {};
                    return `<div style="font-size:13px;font-weight:600;color:#2c6ecb">${escapeHtml(params.data.name)}</div>
                            <div style="font-size:11px;color:#64748b;margin-top:4px">类型: ${escapeHtml(nd.type || '')}</div>`;
                } else if (params.dataType === 'edge') {
                    const rel = params.data.label ? params.data.label.formatter : '';
                    const ed = params.data.edgeData || {};
                    var sups = ed.support || [];
                    var pgs = ed.page || [];
                    if (typeof sups === 'string') sups = sups ? [sups] : [];
                    if (typeof pgs === 'string') pgs = pgs ? [pgs] : [];
                    var supLine = '';
                    if (sups.length) {
                        supLine = '<div style="font-size:11px;color:#64748b;margin-top:4px">依据: ' + sups.map(function(s, i) {
                            var pg = (pgs[i] !== undefined && pgs[i] !== null && String(pgs[i]).trim() !== '') ? ' P' + String(pgs[i]).trim() : '';
                            return escapeHtml(String(s).trim() || '未知来源') + escapeHtml(pg);
                        }).join('；') + '</div>';
                    }
                    return `<div style="font-size:12px;color:#2c6ecb">关系: ${escapeHtml(rel)}</div>` + supLine;
                }
            },
            backgroundColor: 'rgba(255, 255, 255, 0.98)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            padding: [8, 12],
            textStyle: { color: '#1a202c' }
        },
        series: [{
            type: 'graph',
            layout: 'force',
            data: nodes,
            links: edges,
            categories: categories,
            roam: true,
            draggable: true,
            edgeSymbol: ['none', 'arrow'],
            force: {
                repulsion: graphData.nodes.length > 500 ? 500 : 300,
                edgeLength: graphData.nodes.length > 500 ? [100, 220] : [80, 200],
                gravity: 0.08,
                friction: 0.6,
                layoutAnimation: true
            },
            label: {
                show: true,
                position: 'bottom',
                color: '#1a202c',
                fontSize: 11
            },
            labelLayout: {
                hideOverlap: true,
                moveOverlap: 'shiftY'
            },
            lineStyle: {
                color: 'source',
                curveness: 0.2
            },
            emphasis: {
                focus: 'adjacency',
                lineStyle: {
                    width: 3,
                    opacity: 1
                },
                itemStyle: {
                    shadowBlur: 20,
                    opacity: 1
                },
                label: {
                    fontSize: 13,
                    fontWeight: 700
                }
            },
            select: {
                itemStyle: {
                    shadowBlur: 30,
                    borderWidth: 3,
                    borderColor: '#fff'
                }
            },
            selectedMode: 'single',
            animationDuration: 1000,
            animationEasingUpdate: 'quinticInOut'
        }]
    };

    chart.setOption(option, true);
}

// ===== 四大中心辐射式布局（默认探索视图） =====
// 岗位（上）/ 技能（下）/ 课程（左）/ 竞赛（右）菱形分布；
// 收起态每中心只显示连接度 Top N 的核心成员，点击锚点展开全部；
// 跨中心关系线基态隐藏，悬停中心锚点或成员节点时按需点亮（信息不过载）
// ===== 培养时序视图（P1-1：培养方案转时序思维导图）=====
// 从课程节点 properties.开课学年/开课学期 构建树：根 -> 学年 -> 学期 -> 课程叶子
// 多年级课程（如"大二,大一"）拆分挂载到每个学年；"全部"单独成组；无值归入"未注明"
function buildTimelineTree() {
    const years = {};
    TIMELINE_YEAR_ORDER.forEach(y => { years[y] = {}; });
    let courseCount = 0;
    graphData.nodes.forEach(node => {
        if (node.type !== '课程') return;
        const props = node.properties || {};
        const rawYear = String(props['开课学年'] || '').trim();
        const rawSem = String(props['开课学期'] || '').trim();
        let yList;
        if (!rawYear || rawYear === '未注明') {
            yList = ['未注明'];
        } else if (rawYear === '全部') {
            yList = ['全部'];
        } else {
            yList = rawYear.split(',').map(s => s.trim())
                .filter(s => s && s !== '全部' && TIMELINE_YEAR_ORDER.indexOf(s) !== -1);
            if (!yList.length) yList = ['未注明'];
        }
        let semList;
        if (!rawSem || rawSem === '未注明') {
            semList = ['未注明'];
        } else if (rawSem === '全部') {
            semList = ['全部'];
        } else {
            semList = rawSem.split(',').map(s => s.trim()).filter(Boolean);
            if (!semList.length) semList = ['未注明'];
        }
        courseCount++;
        yList.forEach(y => {
            semList.forEach(sem => {
                (years[y][sem] = years[y][sem] || []).push(node);
            });
        });
    });

    const root = {
        name: '培养时序', isTimelineRoot: true, symbolSize: 30,
        itemStyle: { color: '#f59e0b' },
        label: { position: 'bottom', distance: 8, fontSize: 15, fontWeight: 'bold', color: '#fbbf24' },
        children: []
    };
    let yearCount = 0;
    TIMELINE_YEAR_ORDER.forEach(y => {
        const sems = years[y];
        const semNames = TIMELINE_SEM_ORDER.filter(s => sems[s] && sems[s].length);
        // 防御：未收录的学期值兜底排在最后
        Object.keys(sems).forEach(s => {
            if (TIMELINE_SEM_ORDER.indexOf(s) === -1 && sems[s] && sems[s].length) semNames.push(s);
        });
        if (!semNames.length) return;
        yearCount++;
        const color = TIMELINE_YEAR_COLORS[y] || '#94a3b8';
        let yearTotal = 0;
        const semChildren = semNames.map(s => {
            yearTotal += sems[s].length;
            const label = s === '未注明' ? '未注明学期' : (s === '全部' ? '全学期' : s);
            return {
                name: label + ' ' + sems[s].length + '门',
                isTimelineSem: true, symbolSize: 13,
                itemStyle: { color: color, opacity: 0.75 },
                label: { color: '#cbd5e1' },
                children: sems[s].map(node => ({
                    name: node.name,
                    nodeData: node, symbolSize: 7,
                    itemStyle: { color: color, opacity: 0.55 },
                    label: { color: '#e2e8f0' }
                }))
            };
        });
        root.children.push({
            name: (y === '全部' ? '全部年级' : y) + ' ' + yearTotal + '门',
            isTimelineYear: true, symbolSize: 20,
            itemStyle: { color: color },
            label: { fontWeight: 'bold' },
            children: semChildren
        });
    });
    timelineCourseCount = courseCount;
    timelineYearCount = yearCount;
    return root;
}

function timelineTooltipFormatter(p) {
    const d = p.data || {};
    if (d.nodeData) {
        const props = d.nodeData.properties || {};
        const rows = [];
        if (props['学分']) rows.push('学分：' + props['学分']);
        if (props['课程性质']) rows.push('性质：' + props['课程性质']);
        if (props['开课学年']) rows.push('学年：' + props['开课学年']);
        if (props['开课学期']) rows.push('学期：' + props['开课学期']);
        return '<b>' + d.nodeData.name + '</b>' + (rows.length ? '<br/>' + rows.join('<br/>') : '');
    }
    if (d.isTimelineRoot) return '培养时序思维导图<br/>按 学年 → 学期 → 课程 组织培养方案';
    if (d.isTimelineYear) return '<b>' + d.name + '</b><br/>点击展开/收起学期与课程明细';
    if (d.isTimelineSem) return '<b>' + d.name + '</b><br/>点击展开/收起课程';
    return d.name;
}

// 时序思维导图（LR树：根在左，学年/学期向右展开；默认仅到学年层，信息按需触发）
function renderTimelineGraph() {
    currentRenderLinks = [];
    currentNodeCenter = {};
    hubVisibleNodes = 0;
    hubVisibleEdges = 0;
    const tree = buildTimelineTree();
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'item',
            textStyle: { fontSize: 12 },
            formatter: timelineTooltipFormatter
        },
        series: [{
            type: 'tree',
            data: [tree],
            orient: 'LR',
            left: 90,
            right: 230,
            top: 12,
            bottom: 12,
            roam: true,
            expandAndCollapse: true,
            initialTreeDepth: 1,
            animationDuration: 350,
            animationDurationUpdate: 450,
            symbol: 'circle',
            lineStyle: { color: '#334155', width: 1.4, curveness: 0.5 },
            label: {
                position: 'left',
                verticalAlign: 'middle',
                align: 'right',
                distance: 8,
                fontSize: 13,
                color: '#e2e8f0'
            },
            leaves: {
                label: {
                    position: 'right',
                    verticalAlign: 'middle',
                    align: 'left',
                    distance: 8,
                    fontSize: 12,
                    color: '#cbd5e1'
                }
            },
            emphasis: { focus: 'ancestor', lineStyle: { width: 3 } }
        }]
    }, true);
}

const HUB_CENTER_COLORS = { job: '#2c6ecb', skill: '#10b981', course: '#f59e0b', comp: '#8b5cf6' };

let hubShownByKey = { job: [], skill: [], course: [], comp: [] };
let hubFocus = null;
let hubFocusTimer = null;
const HUB_EXPAND_CAP = 48;
const HUB_FOCUS_NEIGHBORS = 12;

function buildNodeFocus(sourceId) {
    const srcCenter = currentNodeCenter[sourceId];
    if (layoutMode !== 'hub' || !srcCenter) return null;
    const nb = {};
    graphData.edges.forEach(e => {
        let other = null;
        if (e.source === sourceId) other = e.target;
        else if (e.target === sourceId) other = e.source;
        if (other && currentNodeCenter[other] && currentNodeCenter[other] !== srcCenter) {
            nb[other] = (nb[other] || 0) + 1;
        }
    });
    const ids = Object.keys(nb).sort((a, b) => nb[b] - nb[a]).slice(0, HUB_FOCUS_NEIGHBORS);
    if (!ids.length) return null;
    return { mode: 'member', sourceId: sourceId, sourceCenter: srcCenter, neighborIds: ids, neighborSet: new Set(ids), neighborCount: nb };
}

function buildAnchorFocus(hubKey) {
    if (layoutMode !== 'hub') return null;
    const memberSet = new Set((hubShownByKey[hubKey] || []).map(m => m.id));
    const nb = {};
    graphData.edges.forEach(e => {
        const cs = currentNodeCenter[e.source];
        const ct = currentNodeCenter[e.target];
        if (!cs || !ct) return;
        if (cs === hubKey && memberSet.has(e.source) && ct !== hubKey) {
            nb[e.target] = (nb[e.target] || 0) + 1;
        } else if (ct === hubKey && memberSet.has(e.target) && cs !== hubKey) {
            nb[e.source] = (nb[e.source] || 0) + 1;
        }
    });
    const ids = Object.keys(nb).sort((a, b) => nb[b] - nb[a]).slice(0, HUB_FOCUS_NEIGHBORS);
    if (!ids.length) return null;
    return { mode: 'anchor', sourceId: '__hub_' + hubKey + '__', sourceCenter: hubKey, neighborIds: ids, neighborSet: new Set(ids), neighborCount: nb };
}

function scheduleHubFocus(focus) {
    if (layoutMode !== 'hub') return;
    clearTimeout(hubFocusTimer);
    hubFocusTimer = setTimeout(function() {
        const same = (focus === null && hubFocus === null) ||
            (focus && hubFocus && focus.sourceId === hubFocus.sourceId);
        if (same) return;
        hubFocus = focus;
        renderGraph();
    }, focus ? 160 : 240);
}

// 连线层级配色：蓝=权威L1 / 绿=多源抽取L2 / 黄=AI建议L3（信息面板内仍按关系语义着色）
function getEdgeLevelColor(edge) {
    if (edge && edge.source_level === 'L1') return '#2563eb';
    if (edge && edge.source_level === 'L3') return '#eab308';
    return '#16a34a';
}

function toggleHubCenter(hubKey) {
    if (!HUB_CENTERS[hubKey]) return;
    hubExpanded[hubKey] = !hubExpanded[hubKey];
    renderGraph();
    showToast(hubExpanded[hubKey]
        ? `${HUB_CENTERS[hubKey].name}：已展开核心 ${Math.min(hubTotalMembers[hubKey], HUB_EXPAND_CAP)}/${hubTotalMembers[hubKey]} 个成员`
        : `${HUB_CENTERS[hubKey].name}：已收起，仅显示核心成员`, 'success');
}

function renderHubGraph() {
    const W = chart.getWidth();
    const H = chart.getHeight();
    const cx = W / 2;
    const cy = H / 2;

    // 1) 节点归入四大中心 + 连接度统计（未识别类型兜底归入技能中心）
    const nodeCenter = {};
    const nodeById = {};
    const membersBy = { job: [], skill: [], course: [], comp: [] };
    const collegeNodes = []; // 学院节点延后裁决：仅当其仍开设可见课程时才进入课程中心
    graphData.nodes.forEach(n => {
        if (isPublicCourseNode(n)) return; // 公共课（体育/军事/思政/数理基础等）不进课程中心
        if (n.type === '学院') { nodeById[n.id] = n; collegeNodes.push(n); return; }
        const key = HUB_TYPE_MAP[n.type] || 'skill';
        nodeCenter[n.id] = key;
        nodeById[n.id] = n;
        (membersBy[key] = membersBy[key] || []).push(n);
    });
    // 开设学院若只关联已过滤的公共课（如体育部/马克思主义学院/学生工作部），视为孤立节点一并隐藏
    const visibleCourseIds = new Set(membersBy.course.filter(c => c.type === '课程').map(c => c.id));
    collegeNodes.forEach(col => {
        const opensVisibleCourse = graphData.edges.some(e =>
            (e.source === col.id && visibleCourseIds.has(e.target)) ||
            (e.target === col.id && visibleCourseIds.has(e.source)));
        if (opensVisibleCourse) {
            nodeCenter[col.id] = 'course';
            membersBy.course.push(col);
        } else {
            delete nodeById[col.id];
        }
    });
    const degree = {};
    const xdeg = {};
    graphData.edges.forEach(e => {
        degree[e.source] = (degree[e.source] || 0) + 1;
        degree[e.target] = (degree[e.target] || 0) + 1;
        const cs = nodeCenter[e.source];
        const ct = nodeCenter[e.target];
        if (cs && ct && cs !== ct) {
            xdeg[e.source] = (xdeg[e.source] || 0) + 1;
            xdeg[e.target] = (xdeg[e.target] || 0) + 1;
        }
    });
    // 层级优先排序：浅层节点（与搜索中心更相关）稳定占据前排，
    // 深层新增节点只往后排——保证 3跳 ⊇ 2跳 视觉单调（修复"2跳有、3跳没有"）
    const lvlOf = n => (n.level == null ? 99 : n.level);
    const byDegree = arr => arr.slice().sort((a, b) =>
        (lvlOf(a) - lvlOf(b)) ||
        ((xdeg[b.id] || 0) - (xdeg[a.id] || 0)) || ((degree[b.id] || 0) - (degree[a.id] || 0)));

    let relatedNodeIds = null;
    if (hubFocus) {
        relatedNodeIds = new Set(hubFocus.neighborIds);
        if (hubFocus.mode === 'anchor') {
            graphData.nodes.forEach(n => {
                if (nodeCenter[n.id] === hubFocus.sourceCenter) relatedNodeIds.add(n.id);
            });
        } else {
            relatedNodeIds.add(hubFocus.sourceId);
        }
    }

    // 2) 各中心：锚点菱形坐标 + 可见成员（收起态Top N / 展开态全部）
    const anchors = {
        job:    { x: cx,            y: cy - H * 0.21 },
        skill:  { x: cx,            y: cy + H * 0.21 },
        course: { x: cx - W * 0.26, y: cy },
        comp:   { x: cx + W * 0.26, y: cy }
    };
    // 维度聚焦模式：单中心锚点居中，成员全向环绕
    if (dimensionCenter) anchors[dimensionCenter] = { x: cx, y: cy };
    // 锚点向外辐射的方向角（画布y轴向下）
    const anchorAngle = { job: -Math.PI / 2, skill: Math.PI / 2, course: Math.PI, comp: 0 };

    const nodes = [];
    const visibleIds = new Set();
    const shownByKey = {};

    Object.keys(HUB_CENTERS).forEach(key => {
        const members = byDegree(membersBy[key]);
        hubTotalMembers[key] = members.length;

        const shown = hubExpanded[key] ? members.slice(0, HUB_EXPAND_CAP) : members.slice(0, HUB_TOP_N);
        shownByKey[key] = shown;
        hubShownByKey[key] = shown;
        // 空中心不渲染锚点：避免 0/0 占位锚点让维度切换"看起来没反应"
        if (members.length === 0) return;

        const a = anchors[key];
        const color = HUB_CENTER_COLORS[key];

        // 中心锚点（点击展开/收起，悬停点亮跨中心关系线）
        nodes.push({
            id: '__hub_' + key + '__',
            name: HUB_CENTERS[key].name,
            isHub: true,
            hubKey: key,
            x: a.x,
            y: a.y,
            symbol: 'circle',
            symbolSize: 72,
            z: 100,
            itemStyle: {
                color: color,
                shadowBlur: 18,
                shadowColor: color,
                borderColor: '#ffffff',
                borderWidth: 3
            },
            label: {
                show: true,
                position: 'inside',
                formatter: `{name|${HUB_CENTERS[key].name}}\n{cnt|${hubExpanded[key] ? '全部' : '核心'} ${shown.length}/${members.length}}`,
                rich: {
                    name: { fontSize: 13, fontWeight: 700, color: '#ffffff', lineHeight: 18 },
                    cnt: { fontSize: 10, color: 'rgba(255,255,255,0.85)', lineHeight: 14 }
                }
            },
            nodeData: null
        });

        // 成员节点：沿锚点向外扇形排布（超过7个分内外两圈）；维度聚焦时锚点居中、成员全向环绕
        const solo = !!dimensionCenter;
        const outward = solo ? 0 : anchorAngle[key];
        const halfSpread = solo ? Math.PI : Math.PI * (65 / 180);
        const rMaxOut = solo ? Math.min(W, H) / 2 - 80
            : key === 'job' ? a.y - 52
            : key === 'skill' ? H - a.y - 52
            : key === 'course' ? a.x - 52
            : W - a.x - 52;
        const rOuter = Math.min(Math.max(rMaxOut, 88), solo ? 300 : 210);
        const rInner = rOuter * 0.58;
        const twoRows = shown.length > 7;
        const rows = twoRows ? [rOuter, rInner] : [(rOuter + rInner) / 2];
        const rowCounts = twoRows
            ? [Math.ceil(shown.length / 2), shown.length - Math.ceil(shown.length / 2)]
            : [shown.length];

        let idx = 0;
        rows.forEach((radius, r) => {
            const count = rowCounts[r];
            for (let i = 0; i < count; i++) {
                const n = shown[idx++];
                if (!n) break;
                const theta = outward - halfSpread + (2 * halfSpread) * ((i + 0.5) / count);
                visibleIds.add(n.id);
                const c = nodeColors[n.type] || '#888';
                const isRel = !hubFocus || (relatedNodeIds && relatedNodeIds.has(n.id));
                const labelPos = (theta >= -3 * Math.PI / 4 && theta < -Math.PI / 4) ? 'top'
                    : (theta >= -Math.PI / 4 && theta < Math.PI / 4) ? 'right'
                    : (theta >= Math.PI / 4 && theta < 3 * Math.PI / 4) ? 'bottom' : 'left';
                nodes.push({
                    id: n.id,
                    name: n.name,
                    x: a.x + radius * Math.cos(theta),
                    y: a.y + radius * Math.sin(theta),
                    symbolSize: Math.min(getNodeSize(n), 46),
                    itemStyle: {
                        color: c,
                        shadowBlur: 8,
                        shadowColor: c,
                        borderColor: c,
                        borderWidth: 2,
                        opacity: hubFocus ? (isRel ? 0.95 : 0.12) : 0.92
                    },
                    label: {
                        show: isRel,
                        position: labelPos,
                        color: '#1a202c',
                        fontSize: 10,
                        fontWeight: 500
                    },
                    nodeData: n
                });
            }
        });
    });

    if (hubFocus && hubFocus.neighborIds.length) {
        const a1 = anchors[hubFocus.sourceCenter] || { x: cx, y: cy };
        const groups = {};
        hubFocus.neighborIds.forEach(id => {
            if (!nodeById[id] || isPublicCourseNode(nodeById[id])) return; // 聚焦邻居同样不显示公共课/被修剪节点
            const c2 = nodeCenter[id] || 'skill';
            (groups[c2] = groups[c2] || []).push(id);
        });
        Object.keys(groups).forEach(c2 => {
            const a2 = anchors[c2];
            if (!a2) return;
            const dx = a1.x - a2.x;
            const dy = a1.y - a2.y;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const px = -dy / len;
            const py = dx / len;
            const ids = groups[c2];
            ids.forEach((id, i) => {
                if (visibleIds.has(id)) return;
                const n = nodeById[id];
                if (!n) return;
                const off = (i - (ids.length - 1) / 2) * 38;
                const x = a2.x + dx * 0.30 + px * off;
                const y = a2.y + dy * 0.30 + py * off;
                visibleIds.add(id);
                const c = nodeColors[n.type] || '#888';
                nodes.push({
                    id: n.id,
                    name: n.name,
                    x: x,
                    y: y,
                    symbolSize: 30,
                    isFocusNeighbor: true,
                    itemStyle: {
                        color: c,
                        shadowBlur: 14,
                        shadowColor: c,
                        borderColor: '#ffffff',
                        borderWidth: 2.5,
                        opacity: 1
                    },
                    label: {
                        show: true,
                        position: 'bottom',
                        color: '#1a202c',
                        fontSize: 10,
                        fontWeight: 600
                    },
                    nodeData: n
                });
            });
        });
    }

    currentNodeCenter = nodeCenter;

    // 3) 边：结构辐条（锚点→成员，常显）+ 真实关系（中心内常显 / 跨中心基态隐藏）
    const links = [];

    // 3a) 辐条：锚点与可见成员的从属结构线
    Object.keys(shownByKey).forEach(key => {
        const anchorId = '__hub_' + key + '__';
        shownByKey[key].forEach(m => {
            links.push({
                source: anchorId,
                target: m.id,
                isSpoke: true,
                silent: true,
                symbol: ['none', 'none'],
                symbolSize: [0, 0],
                label: { show: false },
                lineStyle: { color: '#cbd5e1', width: 1.2, opacity: 0.5, curveness: 0 },
                emphasis: { lineStyle: { width: 2, opacity: 0.9 } }
            });
        });
    });

    // 3b) 真实边：两端均在可见集合内才渲染；跨中心边基态隐藏（悬停点亮）
    const realEdges = graphData.edges.filter(e => visibleIds.has(e.source) && visibleIds.has(e.target));
    hubVisibleEdges = realEdges.length;
    realEdges.forEach(edge => {
        const cross = nodeCenter[edge.source] !== nodeCenter[edge.target];
        const color = getEdgeLevelColor(edge);
        let focusRel = false;
        if (hubFocus) {
            focusRel = hubFocus.mode === 'anchor'
                ? cross && (hubFocus.neighborSet.has(edge.source) || hubFocus.neighborSet.has(edge.target))
                : (edge.source === hubFocus.sourceId && hubFocus.neighborSet.has(edge.target)) ||
                  (edge.target === hubFocus.sourceId && hubFocus.neighborSet.has(edge.source));
        }
        const cnt = hubFocus ? (hubFocus.neighborCount[edge.source] || hubFocus.neighborCount[edge.target] || 1) : 1;
        links.push({
            source: edge.source,
            target: edge.target,
            isCross: cross,
            centerA: nodeCenter[edge.source],
            centerB: nodeCenter[edge.target],
            edgeData: edge,
            symbol: ['none', 'none'],
            symbolSize: [0, 0],
            label: {
                show: hubFocus ? (focusRel && realEdges.length <= 20) : (!cross && realEdges.length < 60),
                formatter: edge.relation,
                fontSize: 10,
                color: color,
                backgroundColor: 'rgba(255, 255, 255, 0.92)',
                padding: [2, 5],
                borderRadius: 3
            },
            lineStyle: {
                color: color,
                width: hubFocus ? (focusRel ? Math.min(1.6 + cnt * 0.4, 4) : 1.2) : (cross ? 2 : 1.8),
                curveness: 0.18,
                opacity: hubFocus ? (focusRel ? 0.9 : (cross ? 0 : 0.18)) : (cross ? 0 : 0.65),
                type: (hubFocus && focusRel) || !cross ? 'solid' : 'dashed'
            },
            emphasis: {
                lineStyle: { width: 2.6, opacity: 0.95 },
                label: { show: true }
            }
        });
    });

    currentRenderLinks = links;
    hubVisibleNodes = visibleIds.size;

    const option = {
        backgroundColor: 'transparent',
        tooltip: {
            formatter: function(params) {
                if (params.dataType === 'node') {
                    if (params.data.isHub) {
                        const k = params.data.hubKey;
                        const hoverTip = dimensionCenter ? '' : '<div style="font-size:11px;color:#94a3b8;margin-top:2px">悬停查看跨中心关联</div>';
                        return `<div style="font-size:13px;font-weight:600;color:${HUB_CENTER_COLORS[k]}">${escapeHtml(params.data.name)}</div>
                                <div style="font-size:11px;color:#64748b;margin-top:4px">成员 ${hubTotalMembers[k]} 个 · 点击${hubExpanded[k] ? '收起' : '展开'}全部成员</div>${hoverTip}`;
                    }
                    const nd = params.data.nodeData || {};
                    const ck = currentNodeCenter[params.data.id];
                    return `<div style="font-size:13px;font-weight:600;color:#2c6ecb">${escapeHtml(params.data.name)}</div>
                            <div style="font-size:11px;color:#64748b;margin-top:4px">类型: ${escapeHtml(nd.type || '')}${ck ? ' · ' + escapeHtml(HUB_CENTERS[ck].name) : ''}</div>
                            <div style="font-size:11px;color:#94a3b8;margin-top:2px">悬停查看关联的技能/课程/竞赛/岗位</div>`;
                } else if (params.dataType === 'edge') {
                    if (params.data.isSpoke) return '';
                    const rel = params.data.label ? params.data.label.formatter : '';
                    const ed = params.data.edgeData || {};
                    const lv = ed.source_level === 'L1' ? '权威来源 L1'
                        : ed.source_level === 'L3' ? 'AI建议 L3' : '多源抽取 L2';
                    var sups = ed.support || [];
                    var pgs = ed.page || [];
                    if (typeof sups === 'string') sups = sups ? [sups] : [];
                    if (typeof pgs === 'string') pgs = pgs ? [pgs] : [];
                    var supLine = '';
                    if (sups.length) {
                        supLine = '<div style="font-size:11px;color:#64748b;margin-top:4px">依据: ' + sups.map(function(s, i) {
                            var pg = (pgs[i] !== undefined && pgs[i] !== null && String(pgs[i]).trim() !== '') ? ' P' + String(pgs[i]).trim() : '';
                            return escapeHtml(String(s).trim() || '未知来源') + escapeHtml(pg);
                        }).join('；') + '</div>';
                    }
                    return `<div style="font-size:12px;color:#2c6ecb">关系: ${escapeHtml(rel)}</div>
                            <div style="font-size:11px;color:#64748b;margin-top:2px">层级: ${lv}</div>` + supLine;
                }
            },
            backgroundColor: 'rgba(255, 255, 255, 0.98)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            padding: [8, 12],
            textStyle: { color: '#1a202c' }
        },
        series: [{
            type: 'graph',
            layout: 'none',
            data: nodes,
            links: links,
            roam: true,
            draggable: false,
            label: {
                show: true,
                position: 'bottom',
                color: '#1a202c',
                fontSize: 10
            },
            lineStyle: {
                curveness: 0.18
            },
            emphasis: {
                focus: 'adjacency',
                lineStyle: {
                    width: 3,
                    opacity: 1
                },
                itemStyle: {
                    shadowBlur: 20,
                    opacity: 1
                },
                label: {
                    fontSize: 12,
                    fontWeight: 700
                }
            },
            select: {
                itemStyle: {
                    shadowBlur: 30,
                    borderWidth: 3,
                    borderColor: '#fff'
                }
            },
            selectedMode: 'single',
            animationDuration: 600,
            animationEasingUpdate: 'quinticInOut'
        }]
    };

    chart.setOption(option, true);
}

const nodeSizeCache = {};
function getNodeSize(node) {
    if (nodeSizeCache[node.id] !== undefined) return nodeSizeCache[node.id];
    // 根据连接数和类型确定大小
    let size = 30;
    const connections = graphData.edges.filter(e =>
        e.source === node.id || e.target === node.id
    ).length;
    size = Math.max(25, Math.min(60, 25 + connections * 5));
    nodeSizeCache[node.id] = size;
    return size;
}

function getEdgeColor(edgeOrRelation) {
    var relation = typeof edgeOrRelation === 'string' ? edgeOrRelation : (edgeOrRelation && edgeOrRelation.relation) || '';
    // 岗位竞合：按 attrs 的重叠度数值/关系判定分三档着色（高重叠红/部分互补橙/差异化互补蓝）
    if (relation === '技能重叠' && typeof edgeOrRelation === 'object' && edgeOrRelation) {
        var attrs = edgeOrRelation.attrs || {};
        var num = parseFloat(attrs['重叠度数值']);
        var judge = String(attrs['关系判定'] || '');
        if (!isNaN(num) && num >= 60) return '#ef4444';
        if (!isNaN(num) && num >= 30) return '#f97316';
        if (judge.indexOf('高重叠') !== -1) return '#ef4444';
        if (judge.indexOf('部分互补') !== -1) return '#f97316';
        return '#3b82f6';
    }
    const colorMap = {
        '招聘于': '#f59e0b',
        '位于': '#06b6d4',
        '属于': '#10b981',
        '要求': '#ef4444',
        '使用': '#14b8a6',
        '可考取': '#8b5cf6',
        '可替代': '#f97316',
        '晋升/转岗': '#ec4899',
        '需补充技能': '#eab308',
        '培养': '#65a30d',
        '胜任': '#3b82f6',
        '开设': '#0ea5e9',
        '核心技能': '#ef4444',
        '技能重叠': '#64748b',
        '包含': '#6366f1',
        '锻炼': '#22c55e',
        '面向': '#a855f7',
    };
    return colorMap[relation] || '#64748b';
}

// ===== 图例 =====
function updateLegend() {
    const legend = document.getElementById('legend');
    var html = '';
    if (layoutMode === 'hub') {
        // 四大中心视图：中心锚点配色
        html += Object.keys(HUB_CENTERS).map(function(key) {
            const c = HUB_CENTERS[key];
            return `<div class="legend-item">
                <div class="legend-dot" style="background:${HUB_CENTER_COLORS[key]}"></div>
                <span>${c.name}</span>
            </div>`;
        }).join('');
    } else if (layoutMode === 'timeline') {
        // 培养时序视图：学年色带
        html += TIMELINE_YEAR_ORDER.map(function(y) {
            const label = y === '全部' ? '全部年级' : y;
            return `<div class="legend-item">
                <div class="legend-dot" style="background:${TIMELINE_YEAR_COLORS[y] || '#94a3b8'}"></div>
                <span>${label}</span>
            </div>`;
        }).join('');
    } else {
        // 力导向全图：按节点类型着色（"院校"枢纽节点不进图例）
        const types = [...new Set(graphData.nodes.map(n => n.type))].filter(t => t !== '院校');
        html += types.map(type => {
            const color = nodeColors[type] || '#888';
            return `<div class="legend-item">
                <div class="legend-dot" style="background:${color};color:${color}"></div>
                <span>${type}</span>
            </div>`;
        }).join('');
    }
    // 连线数据层级：蓝=权威L1 / 绿=抽取L2 / 黄=AI建议L3
    html += `
        <div class="legend-item legend-line-item"><span class="legend-line" style="background:#2563eb"></span><span>权威来源 L1</span></div>
        <div class="legend-item legend-line-item"><span class="legend-line" style="background:#16a34a"></span><span>多源抽取 L2</span></div>
        <div class="legend-item legend-line-item"><span class="legend-line" style="background:#eab308"></span><span>AI建议 L3</span></div>`;
    legend.innerHTML = html;
}

// ===== 统计 =====
function updateStats() {
    const nodeEl = document.getElementById('nodeCount');
    const edgeEl = document.getElementById('edgeCount');
    if (layoutMode === 'hub') {
        // 四大中心视图：显示 已展示/总数（默认只呈现核心节点）
        nodeEl.textContent = `${hubVisibleNodes}/${graphData.nodes.length}`;
        edgeEl.textContent = `${hubVisibleEdges}/${graphData.edges.length}`;
    } else if (layoutMode === 'timeline') {
        // 培养时序视图：节点位显示 已收录课程/全部节点，关系位显示 学年分组数
        nodeEl.textContent = `${timelineCourseCount}/${graphData.nodes.length}`;
        edgeEl.textContent = timelineYearCount;
    } else {
        nodeEl.textContent = graphData.nodes.length;
        edgeEl.textContent = graphData.edges.length;
    }
}

// ===== 节点点击 =====
function handleNodeClick(data) {
    // 四大中心锚点：点击展开/收起该中心全部成员（不进入节点详情）
    if (data && data.isHub) {
        toggleHubCenter(data.hubKey);
        return;
    }
    selectedNode = data.nodeData || graphData.nodes.find(n => n.id === data.id);
    if (!selectedNode) return;

    // 时序视图为树形结构（series.data非扁平id列表），跳过逐点高亮，直接打开信息面板
    if (layoutMode !== 'timeline') {
        highlightNode(selectedNode.id);
    }

    // 显示信息面板
    showNodeInfo(selectedNode);
}

function highlightNode(nodeId) {
    if (!chart) return;
    const option = chart.getOption();
    const series = option.series[0];
    if (series && series.data) {
        series.data.forEach(node => {
            if (node.id === nodeId) {
                node.itemStyle = node.itemStyle || {};
                node.itemStyle.shadowBlur = 30;
                node.itemStyle.borderWidth = 4;
                node.itemStyle.borderColor = '#ffffff';
                node.symbolSize = (node.symbolSize || 30) + 10;
            }
        });
    }
    chart.setOption(option);

    // 重新渲染恢复正常大小（延迟）
    setTimeout(() => {
        // 恢复
    }, 100);
}

// ===== 节点信息展示 =====
function showNodeInfo(node) {
    selectedNode = node;
    selectedEdge = null;
    const panel = document.getElementById('infoPanel');
    const content = document.getElementById('infoContent');
    const actions = document.getElementById('infoActions');

    const color = nodeColors[node.type] || '#888';
    const icon = NODE_ICONS[node.type] || node.name.charAt(0);

    // 找到关联边
    const relations = graphData.edges.map(function(e, idx) {
        if (e.source !== node.id && e.target !== node.id) return null;
        var isSource = e.source === node.id;
        var relatedId = isSource ? e.target : e.source;
        var relatedNode = graphData.nodes.find(function(n) { return n.id === relatedId; });
        if (!relatedNode) return null;
        return {
            edgeIndex: idx,
            direction: isSource ? 'out' : 'in',
            relation: e.relation,
            source: e.source,
            target: e.target,
            node: relatedNode,
            support: e.support || '',
            page: e.page || '',
            product: e.product || '',
            attrs: e.attrs || {}
        };
    }).filter(function(r) { return r; });

    var html = '<div class="node-info-card">' +
        '<div class="node-info-header">' +
            '<div class="node-info-icon" style="background:' + color + ';box-shadow:0 0 15px ' + color + '">' + icon + '</div>' +
            '<div class="node-info-title">' +
                '<h4>' + escapeHtml(node.name) + '</h4>' +
                '<span class="node-info-type" style="background:' + color + '22;color:' + color + ';border-color:' + color + '55">' + node.type + '</span>' +
            '</div>' +
        '</div>' +
        '<div class="node-info-properties">';

    // 显示属性
    if (node.properties && Object.keys(node.properties).length > 0) {
        for (var key in node.properties) {
            if (!node.properties.hasOwnProperty(key)) continue;
            var value = node.properties[key];
            if (value === null || value === undefined || value === '') continue;
            // 列表类型属性（如多条生成产品）
            var displayValue;
            if (Array.isArray(value)) {
                if (value.length === 0) continue;
                displayValue = value.map(function(v) { return escapeHtml(String(v)); }).join('<br>');
            } else {
                displayValue = escapeHtml(String(value));
            }
            var isLong = String(value).length > 80;
            html += '<div class="property-item">' +
                '<div class="property-key">' + escapeHtml(key) + '</div>' +
                '<div class="property-value ' + (isLong ? 'long-text' : '') + '">' + displayValue + '</div>' +
            '</div>';
        }
    } else {
        html += '<div style="color:var(--text-muted);font-size:13px;text-align:center;padding:20px">暂无属性信息</div>';
    }

    html += '</div>';

    // 显示关联路径（三元组形式）
    html += '<div class="relation-section">' +
        '<div class="relation-section-header">' +
            '<h5>关联路径 (' + relations.length + ')</h5>' +
            '<button class="btn-add-edge-inline" onclick="addEdgeFromNode(\'' + node.id + '\')">+ 添加</button>' +
        '</div>';

    if (relations.length > 0) {
        relations.forEach(function(r) {
            var rColor = nodeColors[r.node.type] || '#888';
            var eColor = getEdgeColor({ relation: r.relation, attrs: r.attrs });
            // 三元组：源节点 - 关系 -> 目标节点
            var sourceNode, targetNode;
            if (r.direction === 'out') {
                sourceNode = node;
                targetNode = r.node;
            } else {
                sourceNode = r.node;
                targetNode = node;
            }
            var sColor = nodeColors[sourceNode.type] || '#888';
            var tColor = nodeColors[targetNode.type] || '#888';
            // 构建三元组文本
            var triplet = escapeHtml(sourceNode.name) +
                ' <span class="triplet-rel" style="color:' + eColor + '">' + escapeHtml(r.relation) + '</span> ' +
                escapeHtml(targetNode.name);

            // 竞争产品/合作产品
            var productLabel = '';
            var productText = '';
            if (r.product && r.relation === '竞争') {
                productLabel = '竞争产品';
                productText = escapeHtml(String(r.product));
            } else if (r.product && r.relation === '合作') {
                productLabel = '合作产品';
                productText = escapeHtml(String(r.product));
            }

            // 原文支撑内容（截断显示）
            var supportText = r.support ? escapeHtml(String(r.support)) : '-';
            var supportShort = String(r.support || '');
            var supportDisplay = supportShort.length > 40 ? escapeHtml(supportShort.substring(0, 40)) + '...' : supportText;
            var pageText = r.page ? escapeHtml(String(r.page)) : '-';

            html += '<div class="relation-item-edge triplet-card">' +
                '<div class="triplet-row">' +
                    '<div class="relation-item-main" onclick="jumpToNode(\'' + r.node.id + '\')">' +
                        '<div class="legend-dot" style="background:' + sColor + ';color:' + sColor + ';width:8px;height:8px;min-width:8px"></div>' +
                        '<span class="triplet-text">' + triplet + '</span>' +
                        '<div class="legend-dot" style="background:' + tColor + ';color:' + tColor + ';width:8px;height:8px;min-width:8px"></div>' +
                    '</div>' +
                    '<div class="relation-item-actions">' +
                        '<button class="btn-edge-edit" title="修改" onclick="editEdge(' + r.edgeIndex + ')">' +
                            '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>' +
                        '</button>' +
                        '<button class="btn-edge-delete" title="删除" onclick="deleteEdge(' + r.edgeIndex + ')">' +
                            '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>' +
                        '</button>' +
                    '</div>' +
                '</div>';

            // 竞争产品/合作产品 单独一栏
            if (productLabel) {
                html += '<div class="triplet-product-row">' +
                    '<span class="triplet-product-label" style="color:' + eColor + '">' + productLabel + '</span>' +
                    '<span class="triplet-product-value">' + productText + '</span>' +
                '</div>';
            }

            html += '<div class="triplet-meta">' +
                    '<div class="triplet-meta-col">' +
                        '<span class="triplet-meta-label">原文支撑</span>' +
                        '<span class="triplet-meta-value" title="' + supportText + '">' + supportDisplay + '</span>' +
                    '</div>' +
                    '<div class="triplet-meta-col triplet-meta-page">' +
                        '<span class="triplet-meta-label">页号</span>' +
                        '<span class="triplet-meta-value">' + pageText + '</span>' +
                    '</div>' +
                '</div>' +
            '</div>';
        });
    } else {
        html += '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">暂无关联路径</div>';
    }
    html += '</div></div>';

    content.innerHTML = html;
    var isKnown = (profileCache.known_skills || []).some(function(s) { return s.id === node.id; });
    actions.innerHTML =
        '<button class="btn-known' + (isKnown ? ' is-known' : '') + '" id="btnMarkKnown" onclick="toggleKnownSkill()" title="标记为已掌握，AI差距分析将自动对照">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>' +
            '<span id="markKnownText">' + (isKnown ? '取消点亮' : '点亮已会') + '</span>' +
        '</button>' +
        '<button class="btn-edit" onclick="editNode()">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>修改节点' +
        '</button>' +
        '<button class="btn-delete" onclick="deleteNode()">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>删除节点' +
        '</button>';
    actions.style.display = 'flex';
    panel.classList.add('show');
}

// ===== 边信息展示（点击边时） =====
function handleEdgeClick(data) {
    var edgeIdx = data.edgeIndex !== undefined ? data.edgeIndex : graphData.edges.findIndex(function(e) {
        return e.source === data.source && e.target === data.target;
    });
    if (edgeIdx < 0 || edgeIdx >= graphData.edges.length) return;

    selectedEdge = graphData.edges[edgeIdx];
    selectedNode = null;

    var panel = document.getElementById('infoPanel');
    var content = document.getElementById('infoContent');
    var actions = document.getElementById('infoActions');

    var sourceNode = graphData.nodes.find(function(n) { return n.id === selectedEdge.source; });
    var targetNode = graphData.nodes.find(function(n) { return n.id === selectedEdge.target; });
    var eColor = getEdgeColor(selectedEdge);

    var html = '<div class="node-info-card">' +
        '<div class="node-info-header">' +
            '<div class="node-info-icon" style="background:' + eColor + ';box-shadow:0 0 15px ' + eColor + '">' +
                '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="white" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>' +
            '</div>' +
            '<div class="node-info-title">' +
                '<h4>' + escapeHtml(selectedEdge.relation) + '</h4>' +
                '<span class="node-info-type" style="background:' + eColor + '22;color:' + eColor + ';border-color:' + eColor + '55">关系</span>' +
            '</div>' +
        '</div>' +
        '<div class="node-info-properties">' +
            '<div class="property-item"><div class="property-key">源节点</div>' +
                '<div class="property-value">' + (sourceNode ? escapeHtml(sourceNode.name) + ' (' + escapeHtml(sourceNode.type) + ')' : '未知') + '</div></div>' +
            '<div class="property-item"><div class="property-key">目标节点</div>' +
                '<div class="property-value">' + (targetNode ? escapeHtml(targetNode.name) + ' (' + escapeHtml(targetNode.type) + ')' : '未知') + '</div></div>' +
            '<div class="property-item"><div class="property-key">关系类型</div>' +
                '<div class="property-value" style="color:' + eColor + '">' + escapeHtml(selectedEdge.relation) + '</div></div>' +
        '</div></div>';

    content.innerHTML = html;
    actions.innerHTML =
        '<button class="btn-edit" onclick="editEdge(' + edgeIdx + ')">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>修改关系' +
        '</button>' +
        '<button class="btn-delete" onclick="deleteEdge(' + edgeIdx + ')">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>删除关系' +
        '</button>';
    actions.style.display = 'flex';
    panel.classList.add('show');
}

function closeInfoPanel() {
    document.getElementById('infoPanel').classList.remove('show');
    selectedNode = null;
    selectedEdge = null;
    if (chart) {
        chart.dispatchAction({ type: 'unhighlight', seriesIndex: 0 });
    }
}

function jumpToNode(nodeId) {
    var node = graphData.nodes.find(function(n) { return n.id === nodeId; });
    if (!node) return;
    handleNodeClick({ data: { id: nodeId, nodeData: node } });
}

// ===== 节点CRUD =====
function addNodeMode() {
    document.getElementById('newNodeName').value = '';
    document.getElementById('newNodeType').value = '岗位';
    document.getElementById('newNodeProps').value = '';
    showModal('addNodeModal');
}

function confirmAddNode() {
    const name = document.getElementById('newNodeName').value.trim();
    const type = document.getElementById('newNodeType').value;
    const propsStr = document.getElementById('newNodeProps').value.trim();

    if (!name) {
        showToast('请输入节点名称', 'error');
        return;
    }

    let properties = {};
    if (propsStr) {
        try {
            properties = JSON.parse(propsStr);
        } catch (e) {
            showToast('属性JSON格式错误', 'error');
            return;
        }
    }

    const newNode = {
        id: `${type}:${name}`,
        name: name,
        type: type,
        category: type,
        properties: properties
    };

    // 全局ID规范（类型:名称）：同名同类型节点跨维度自动合并，禁止重复添加
    if (graphData.nodes.some(function(n) { return n.id === newNode.id; })) {
        showToast('已存在同类型同名节点「' + name + '」，如需修改请点击该节点编辑', 'error');
        return;
    }

    // 调用API持久化
    fetch(`/api/graph/${currentDimension}/node`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newNode)
    }).then(r => r.json()).then(data => {
        if (data.success) {
            graphData.nodes.push(data.node);
            renderGraph();
            updateStats();
            updateLegend();
            closeModal('addNodeModal');
            showToast('节点添加成功', 'success');
        } else {
            showToast(data.error || '添加失败', 'error');
        }
    }).catch(e => showToast('添加失败: ' + e.message, 'error'));
}

function editNode() {
    if (!selectedNode) return;
    const body = document.getElementById('editNodeBody');
    currentEditProperties = Object.entries(selectedNode.properties || {});

    let html = `
        <div class="form-group">
            <label>节点名称</label>
            <input type="text" id="editNodeName" value="${escapeHtml(selectedNode.name)}" />
        </div>
        <div class="form-group">
            <label>节点类型</label>
            <select id="editNodeType">`;

    Object.keys(NODE_ICONS).forEach(type => {
        html += `<option value="${type}" ${selectedNode.type === type ? 'selected' : ''}>${type}</option>`;
    });

    html += `</select>
        </div>
        <div class="form-group">
            <label>节点属性</label>
            <div id="editPropsContainer">`;

    currentEditProperties.forEach((prop, idx) => {
        html += `
            <div class="edit-prop-row">
                <input type="text" value="${escapeHtml(prop[0])}" placeholder="属性名" data-prop-key="${idx}" style="flex:1" />
                <input type="text" value="${escapeHtml(String(prop[1] || ''))}" placeholder="属性值" data-prop-val="${idx}" style="flex:2" />
                <div class="edit-prop-remove" onclick="removeEditProp(this)">&times;</div>
            </div>`;
    });

    html += `</div>
            <button class="btn-add-prop" onclick="addEditProp()">+ 添加属性</button>
        </div>`;

    body.innerHTML = html;
    showModal('editNodeModal');
}

function addEditProp() {
    const container = document.getElementById('editPropsContainer');
    const idx = container.children.length;
    const row = document.createElement('div');
    row.className = 'edit-prop-row';
    row.innerHTML = `
        <input type="text" placeholder="属性名" data-prop-key="new" style="flex:1" />
        <input type="text" placeholder="属性值" data-prop-val="new" style="flex:2" />
        <div class="edit-prop-remove" onclick="removeEditProp(this)">&times;</div>
    `;
    container.appendChild(row);
}

function removeEditProp(btn) {
    btn.parentElement.remove();
}

function confirmEditNode() {
    if (!selectedNode) return;

    const name = document.getElementById('editNodeName').value.trim();
    const type = document.getElementById('editNodeType').value;

    if (!name) {
        showToast('请输入节点名称', 'error');
        return;
    }

    // 收集属性
    const properties = {};
    const container = document.getElementById('editPropsContainer');
    container.querySelectorAll('.edit-prop-row').forEach(row => {
        const keyInput = row.querySelector('[data-prop-key]');
        const valInput = row.querySelector('[data-prop-val]');
        const key = keyInput.value.trim();
        const val = valInput.value.trim();
        if (key) {
            properties[key] = val;
        }
    });

    // 更新节点
    selectedNode.name = name;
    selectedNode.type = type;
    selectedNode.category = type;
    selectedNode.properties = properties;

    // 调用API持久化
    fetch(`/api/graph/${currentDimension}/node/${encodeURIComponent(selectedNode.id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(selectedNode)
    }).then(r => r.json()).then(data => {
        if (data.success) {
            // 更新graphData中的节点
            const idx = graphData.nodes.findIndex(n => n.id === selectedNode.id);
            if (idx !== -1) {
                graphData.nodes[idx] = selectedNode;
            }
            renderGraph();
            updateLegend();
            showNodeInfo(selectedNode);
            closeModal('editNodeModal');
            showToast('节点修改成功', 'success');
        } else {
            showToast(data.error || '修改失败', 'error');
        }
    }).catch(e => showToast('修改失败: ' + e.message, 'error'));
}

function deleteNode() {
    if (!selectedNode) return;
    if (!confirm(`确定删除节点 "${selectedNode.name}" 吗？\n相关的关系也会被删除。`)) return;

    // 调用API持久化
    fetch(`/api/graph/${currentDimension}/node/${encodeURIComponent(selectedNode.id)}`, {
        method: 'DELETE'
    }).then(r => r.json()).then(data => {
        if (data.success) {
            graphData.nodes = graphData.nodes.filter(n => n.id !== selectedNode.id);
            graphData.edges = graphData.edges.filter(e =>
                e.source !== selectedNode.id && e.target !== selectedNode.id
            );
            closeInfoPanel();
            renderGraph();
            updateStats();
            updateLegend();
            showToast('节点删除成功', 'success');
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    }).catch(e => showToast('删除失败: ' + e.message, 'error'));
}

// ===== 添加关系 =====
function addEdgeMode() {
    if (graphData.nodes.length < 2) {
        showToast('至少需要2个节点才能添加关系', 'error');
        return;
    }

    const sourceSelect = document.getElementById('edgeSource');
    const targetSelect = document.getElementById('edgeTarget');

    sourceSelect.innerHTML = graphData.nodes.map(n =>
        `<option value="${n.id}">${n.name} (${n.type})</option>`
    ).join('');
    targetSelect.innerHTML = graphData.nodes.map(n =>
        `<option value="${n.id}">${n.name} (${n.type})</option>`
    ).join('');

    if (graphData.nodes.length > 1) {
        targetSelect.selectedIndex = 1;
    }

    document.getElementById('edgeRelation').value = '';
    showModal('addEdgeModal');
}

function confirmAddEdge() {
    const source = document.getElementById('edgeSource').value;
    const target = document.getElementById('edgeTarget').value;
    const relation = document.getElementById('edgeRelation').value.trim() || '关联';

    if (source === target) {
        showToast('源节点和目标节点不能相同', 'error');
        return;
    }

    // 检查是否已存在
    const exists = graphData.edges.some(e =>
        e.source === source && e.target === target && e.relation === relation
    );
    if (exists) {
        showToast('该关系已存在', 'error');
        return;
    }

    var newEdge = {
        source: source,
        target: target,
        relation: relation,
        label: relation
    };

    // 调用API持久化
    fetch(`/api/graph/${currentDimension}/edge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newEdge)
    }).then(r => r.json()).then(data => {
        if (data.success) {
            graphData.edges.push(data.edge);
            renderGraph();
            updateStats();
            closeModal('addEdgeModal');
            showToast('关系添加成功', 'success');
        } else {
            showToast(data.error || '添加失败', 'error');
        }
    }).catch(e => showToast('添加失败: ' + e.message, 'error'));
}

// ===== 从节点面板添加关系 =====
function addEdgeFromNode(nodeId) {
    if (graphData.nodes.length < 2) {
        showToast('至少需要2个节点才能添加关系', 'error');
        return;
    }

    var sourceSelect = document.getElementById('edgeSource');
    var targetSelect = document.getElementById('edgeTarget');

    var options = graphData.nodes.map(function(n) {
        return '<option value="' + n.id + '">' + n.name + ' (' + n.type + ')</option>';
    }).join('');
    sourceSelect.innerHTML = options;
    targetSelect.innerHTML = options;

    // 默认选当前节点为源
    sourceSelect.value = nodeId;
    // 选另一个节点为目标
    for (var i = 0; i < targetSelect.options.length; i++) {
        if (targetSelect.options[i].value !== nodeId) {
            targetSelect.selectedIndex = i;
            break;
        }
    }

    document.getElementById('edgeRelation').value = '';
    showModal('addEdgeModal');
}

// ===== 修改关系 =====
function editEdge(edgeIndex) {
    var edge = graphData.edges[edgeIndex];
    if (!edge) return;

    var sourceSelect = document.getElementById('editEdgeSource');
    var targetSelect = document.getElementById('editEdgeTarget');

    var options = graphData.nodes.map(function(n) {
        return '<option value="' + n.id + '">' + n.name + ' (' + n.type + ')</option>';
    }).join('');
    sourceSelect.innerHTML = options;
    targetSelect.innerHTML = options;

    sourceSelect.value = edge.source;
    targetSelect.value = edge.target;
    document.getElementById('editEdgeRelation').value = edge.relation || '';
    document.getElementById('editEdgeSupport').value = edge.support || '';
    document.getElementById('editEdgePage').value = edge.page || '';
    document.getElementById('editEdgeProduct').value = edge.product || '';

    // 竞合关系时显示竞争/合作产品字段
    updateEditEdgeProductField(edge.relation);

    // 存储当前编辑的边ID（数据库id）和索引
    document.getElementById('editEdgeModal').dataset.edgeId = edge.id || '';
    document.getElementById('editEdgeModal').dataset.edgeIndex = edgeIndex;
    showModal('editEdgeModal');
}

// 根据关系类型显隐竞争/合作产品字段并更新标签
function updateEditEdgeProductField(relation) {
    var group = document.getElementById('editEdgeProductGroup');
    var label = document.getElementById('editEdgeProductLabel');
    var rel = (relation || '').trim();
    if (rel === '竞争') {
        group.style.display = '';
        label.textContent = '竞争产品';
    } else if (rel === '合作') {
        group.style.display = '';
        label.textContent = '合作产品';
    } else {
        group.style.display = 'none';
    }
}

function confirmEditEdge() {
    var modal = document.getElementById('editEdgeModal');
    var edgeId = modal.dataset.edgeId;
    var edgeIndex = parseInt(modal.dataset.edgeIndex);
    if (isNaN(edgeIndex) || edgeIndex < 0 || edgeIndex >= graphData.edges.length) {
        showToast('无效的边', 'error');
        return;
    }

    var source = document.getElementById('editEdgeSource').value;
    var target = document.getElementById('editEdgeTarget').value;
    var relation = document.getElementById('editEdgeRelation').value.trim() || '关联';
    var support = document.getElementById('editEdgeSupport').value;
    var page = document.getElementById('editEdgePage').value;
    var product = document.getElementById('editEdgeProduct').value.trim();

    if (source === target) {
        showToast('源节点和目标节点不能相同', 'error');
        return;
    }

    var edgeData = {
        source: source,
        target: target,
        relation: relation,
        label: relation,
        support: support,
        page: page,
        product: (relation === '竞争' || relation === '合作') ? product : (graphData.edges[edgeIndex].product || '')
    };

    // 调用API持久化
    fetch(`/api/graph/${currentDimension}/edge/${edgeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(edgeData)
    }).then(r => r.json()).then(data => {
        if (data.success) {
            // 更新前端数据
            graphData.edges[edgeIndex].source = source;
            graphData.edges[edgeIndex].target = target;
            graphData.edges[edgeIndex].relation = relation;
            graphData.edges[edgeIndex].label = relation;
            graphData.edges[edgeIndex].support = support;
            graphData.edges[edgeIndex].page = page;
            graphData.edges[edgeIndex].product = edgeData.product;

            renderGraph();
            closeModal('editEdgeModal');

            if (selectedNode) {
                showNodeInfo(selectedNode);
            } else if (selectedEdge) {
                handleEdgeClick({ edgeIndex: edgeIndex });
            }
            showToast('关系修改成功', 'success');
        } else {
            showToast(data.error || '修改失败', 'error');
        }
    }).catch(e => showToast('修改失败: ' + e.message, 'error'));
}

// ===== 删除关系 =====
function deleteEdge(edgeIndex) {
    if (edgeIndex < 0 || edgeIndex >= graphData.edges.length) return;

    var edge = graphData.edges[edgeIndex];
    var edgeId = edge.id;
    var sourceNode = graphData.nodes.find(function(n) { return n.id === edge.source; });
    var targetNode = graphData.nodes.find(function(n) { return n.id === edge.target; });
    var desc = (sourceNode ? sourceNode.name : '?') + ' --' + edge.relation + '-> ' + (targetNode ? targetNode.name : '?');

    if (!confirm('确定删除关系 "' + desc + '" 吗？')) return;

    // 调用API持久化
    fetch(`/api/graph/${currentDimension}/edge/${edgeId}`, {
        method: 'DELETE'
    }).then(r => r.json()).then(data => {
        if (data.success) {
            graphData.edges.splice(edgeIndex, 1);
            renderGraph();
            updateStats();
            if (selectedNode) {
                showNodeInfo(selectedNode);
            } else {
                closeInfoPanel();
            }
            showToast('关系删除成功', 'success');
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    }).catch(e => showToast('删除失败: ' + e.message, 'error'));
}

// ===== 图谱操作 =====
function resetGraph() {
    // 调用重新导入API（从Excel重新导入到SQLite）
    fetch(`/api/graph/${currentDimension}/reimport`, {
        method: 'POST'
    }).then(r => r.json()).then(data => {
        if (data.success) {
            loadDimension(currentDimension);
            showToast('图谱已重置', 'success');
        } else {
            showToast(data.error || '重置失败', 'error');
        }
    }).catch(e => showToast('重置失败: ' + e.message, 'error'));
}

function zoomIn() {
    if (!chart) return;
    const option = chart.getOption();
    const zoom = option.series[0].zoom || 1;
    chart.setOption({ series: [{ zoom: zoom * 1.2 }] });
}

function zoomOut() {
    if (!chart) return;
    const option = chart.getOption();
    const zoom = option.series[0].zoom || 1;
    chart.setOption({ series: [{ zoom: zoom / 1.2 }] });
}

function resetZoom() {
    if (!chart) return;
    chart.setOption({ series: [{ zoom: 1, center: undefined }] });
}

function toggleLayout() {
    // 培养时序视图仅在课程体系维度开放
    const modes = currentDimension === 'course_system'
        ? ['hub', 'force', 'timeline']
        : ['hub', 'force'];
    const next = modes[(modes.indexOf(layoutMode) + 1) % modes.length];
    layoutMode = next;
    hubFocus = null;
    clearTimeout(hubFocusTimer);
    renderGraph();
    updateLegend();
    updateStats();
    const msgs = {
        hub: '四大中心视图：默认只显示核心成员，悬停中心锚点查看跨中心关联',
        force: '力导向全图：显示全部节点与关系',
        timeline: '培养时序视图：按学年学期组织课程，点击学年/学期节点展开课程明细'
    };
    showToast(msgs[layoutMode], 'success');
}

// ===== 工具函数 =====
function showModal(id) {
    document.getElementById(id).style.display = 'flex';
}

function closeModal(id) {
    document.getElementById(id).style.display = 'none';
}

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = 'toast ' + type;
    toast.classList.add('show');
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

function showLoading(show) {
    document.getElementById('loadingOverlay').style.display = show ? 'flex' : 'none';
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

// ===== 网页导入（通道A/B）=====
let importPreviewData = null;

function toggleImportTextBox() {
    const box = document.getElementById('importTextBox');
    box.style.display = box.style.display === 'none' ? 'block' : 'none';
}

async function handleImportUrl() {
    const url = document.getElementById('importUrlInput').value.trim();
    if (!url) {
        showToast('请输入网页URL', 'error');
        return;
    }
    showToast('正在抓取网页并抽取，约需10-60秒…', 'info');
    showLoading(true);
    try {
        const res = await fetch('/api/import_url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, doc_type: document.getElementById('importDocType').value })
        });
        const data = await res.json();
        if (data.success) {
            openImportPreview(data);
        } else {
            showToast(data.error || '抽取失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    } finally {
        showLoading(false);
    }
}

// ===== 职业方向：岗位分类总览（图二）+ 九段式职业发展图（图三，右侧工具栏联动） =====
var _jobOverviewCache = {};
var _careerDetailCache = {};
var _planDetailCache = {};
var _planPolicyCache = {};
// 发展路线 → 政策库检索词（多词逗号分隔，命中本地 L1/L2 升学政策条目）
var _PLAN_POLICY_Q = {
    '保研': '推免,保研,推免率,优秀加分,夏令营,预推免',
    '考研': '考研,研究生考试',
    '选调': '选调,央选,定向选调',
    '参军入伍': '入伍,征兵',
    '西部计划': '西部计划,基层项目',
    '三支一扶': '三支一扶,基层项目',
    '特岗计划': '特岗,基层项目',
    '国际组织任职实习': '国际组织,交流',
    '英联邦方向（英港新澳）': '留学,交流,语言成绩',
    '北美方向（美加）': '留学,交流,语言成绩',
    '欧亚方向（欧日韩）': '留学,交流,语言成绩'
};
var _jobOverviewCats = [];
var _careerMajor = '';
var _careerDetailJob = '';
var _planDetailItem = '';
// 发展画像 4 大块中的固定规划块（读研/政策性就业/出国），items 与后端 _PLAN_ITEM_NOTES 一一对应
var _PLAN_BLOCKS = [
    { name: '读研（保研考研）', desc: '升学深造双路线', tag: '升学', tagCls: 'ajo-cat-tag-study', items: ['保研', '考研'] },
    { name: '政策性就业', desc: '选调 · 参军入伍 · 基层项目 · 国际组织', tag: '政策', tagCls: 'ajo-cat-tag-policy', items: ['选调', '参军入伍', '西部计划', '三支一扶', '特岗计划', '国际组织任职实习'] },
    { name: '出国（留学深造）', desc: '主流留学目的地路线', tag: '留学', tagCls: 'ajo-cat-tag-abroad', items: ['英联邦方向（英港新澳）', '北美方向（美加）', '欧亚方向（欧日韩）'] }
];

async function loadJobOverview(major) {
    var wrap = document.getElementById('aiJobOverview');
    if (!wrap || !major) return;
    _careerMajor = major;
    if (_jobOverviewCache[major]) {
        renderJobOverview(major, _jobOverviewCache[major]);
        return;
    }
    try {
        var res = await fetch('/api/career_overview?major=' + encodeURIComponent(major));
        var data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || '生成失败');
        _jobOverviewCache[major] = data.data;
        renderJobOverview(major, data.data);
    } catch (e) {
        var w = document.getElementById('aiJobOverview');
        if (w) w.innerHTML = '<div class="ai-job-overview-loading">岗位总览生成失败：' + escapeHtml(e.message) + '</div>';
    }
}

function renderJobOverview(major, ov) {
    var wrap = document.getElementById('aiJobOverview');
    if (!wrap) return;
    // 发展画像 4 大块：①岗位总览（AI 生成）②读研 ③政策性就业 ④出国，平级独立展示
    var cats = (ov.categories || []).filter(function(c) { return c && String(c.name || '').trim(); });
    _jobOverviewCats = cats;
    var totalJobs = cats.reduce(function(n, c) { return n + (c.jobs || []).length; }, 0);
    var planPaths = _PLAN_BLOCKS.reduce(function(n, b) { return n + b.items.length; }, 0);
    var html = '<div class="ai-job-overview">';
    html += '<div class="ajo-root"><span class="ajo-root-badge">发展画像 · 4 大方向</span>';
    html += '<div class="ajo-root-name">' + escapeHtml(String(ov.major || major)) + ' 发展画像</div>';
    html += '<span class="ajo-root-sub">' + totalJobs + ' 个岗位 · ' + planPaths + ' 条规划路径</span></div>';
    // 块一：岗位总览（点击岗位 → 九段式职业发展图）
    html += '<div class="ajo-block"><div class="ajo-block-head"><span class="ajo-block-idx">1</span>';
    html += '<span class="ajo-block-name">岗位总览</span><span class="ajo-block-tag">AI 生成</span>';
    html += '<span class="ajo-block-desc">点击岗位查看职业发展图</span></div>';
    html += '<div class="ajo-cats">';
    cats.forEach(function(c, ci) {
        var jobs = (c.jobs || []).map(function(j) { return String(j || '').trim(); }).filter(Boolean);
        var chips = jobs.map(function(j, ji) {
            return '<span class="ajo-job" data-cat="' + ci + '" data-idx="' + ji + '" onclick="openCareerDetailIdx(this)">' + escapeHtml(j) + '</span>';
        }).join('');
        html += '<div class="ajo-cat"><div class="ajo-cat-head"><span class="ajo-cat-idx">' + (ci + 1) + '</span>';
        html += '<span class="ajo-cat-name">' + escapeHtml(String(c.name).trim()) + '</span>';
        if (String(c.desc || '').trim()) html += '<span class="ajo-cat-desc">' + escapeHtml(String(c.desc).trim()) + '</span>';
        html += '</div><div class="ajo-jobs">' + chips + '</div></div>';
    });
    html += '</div></div>';
    // 块二/三/四：读研 / 政策性就业 / 出国（点击路径 → 分学年时间准备规划）
    _PLAN_BLOCKS.forEach(function(b, bi) {
        var chips = b.items.map(function(it, ii) {
            return '<span class="ajo-job ajo-plan-job" data-bi="' + bi + '" data-ii="' + ii + '" onclick="openPlanDetail(this)">' + escapeHtml(it) + '</span>';
        }).join('');
        html += '<div class="ajo-block"><div class="ajo-block-head"><span class="ajo-block-idx">' + (bi + 2) + '</span>';
        html += '<span class="ajo-block-name">' + escapeHtml(b.name) + '</span>';
        html += '<span class="ajo-block-tag ' + b.tagCls + '">' + b.tag + '</span>';
        html += '<span class="ajo-block-desc">' + b.desc + '</span></div>';
        html += '<div class="ajo-jobs">' + chips + '</div></div>';
    });
    html += '</div>';
    wrap.innerHTML = html;
}

function openCareerDetailIdx(el) {
    var cat = _jobOverviewCats[parseInt(el.dataset.cat, 10)];
    var job = cat && (cat.jobs || [])[parseInt(el.dataset.idx, 10)];
    if (!job) return;
    document.querySelectorAll('.ajo-job-active').forEach(function(x) { x.classList.remove('ajo-job-active'); });
    el.classList.add('ajo-job-active');
    openCareerDetail(job);
}

function careerDockWrap() {
    var body = document.getElementById('toolDockBody');
    if (!body) return null;
    var empty = document.getElementById('toolDockEmpty');
    var wrap = document.getElementById('careerDockWrap');
    if (!wrap) {
        wrap = document.createElement('div');
        wrap.id = 'careerDockWrap';
        wrap.style.display = 'none';
        if (empty) body.insertBefore(wrap, empty); else body.appendChild(wrap);
    }
    return wrap;
}

async function openCareerDetail(job) {
    var wrap = careerDockWrap();
    if (!wrap) return;
    closeSmartAnalysis();
    var empty = document.getElementById('toolDockEmpty');
    var back = document.getElementById('toolDockBack');
    var title = document.getElementById('toolDockTitle');
    _careerDetailJob = job;
    _planDetailItem = '';
    if (empty) empty.style.display = 'none';
    wrap.style.display = '';
    if (back) back.style.display = 'flex';
    if (title) title.textContent = job;
    var cacheKey = _careerMajor + '|' + job;
    if (_careerDetailCache[cacheKey]) {
        renderCareerDetail(job, _careerDetailCache[cacheKey]);
        return;
    }
    wrap.innerHTML = '<div class="cd-loading">正在生成「' + escapeHtml(job) + '」的九段式职业发展图…<br><span style="font-size:11px;opacity:.75">首次生成约需30-60秒，生成后自动缓存</span></div>';
    try {
        var res = await fetch('/api/career_detail?major=' + encodeURIComponent(_careerMajor) + '&job=' + encodeURIComponent(job));
        var data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || '生成失败');
        _careerDetailCache[cacheKey] = data.data;
        renderCareerDetail(job, data.data);
    } catch (e) {
        wrap.innerHTML = '<div class="cd-loading">职业发展图生成失败：' + escapeHtml(e.message) + '<br><span class="cd-retry" onclick="retryCareerDetail()">点击重试</span></div>';
    }
}

function retryCareerDetail() {
    if (_careerDetailJob) openCareerDetail(_careerDetailJob);
}

// ===== 发展规划（读研/政策性就业/出国 → 分学年时间准备规划，区别于九段式职业发展图）=====

function openPlanDetail(el) {
    var block = _PLAN_BLOCKS[parseInt(el.dataset.bi, 10)];
    var item = block && block.items[parseInt(el.dataset.ii, 10)];
    if (!item) return;
    document.querySelectorAll('.ajo-job-active').forEach(function(x) { x.classList.remove('ajo-job-active'); });
    el.classList.add('ajo-job-active');
    openPlanDetailItem(item);
}

function _renderPlanPolicyHtml(item) {
    var hits = _planPolicyCache[item] || [];
    if (!hits.length) return '';
    var html = '<div class="plan-policy"><div class="plan-policy-head">📋 本校政策文件速览<span>（来源：学校/学院官方文件 · 本地知识库 L1/L2）</span></div>';
    hits.forEach(function(h) {
        html += '<div class="plan-policy-item">';
        html += '<div class="plan-policy-title">' + escapeHtml(h.title || '') + '<span class="plan-policy-layer">' + escapeHtml(h.layer || 'L1') + '</span></div>';
        var fs = (h.facts || []).slice(0, 5);
        if (fs.length === 0 && h.summary) fs = [h.summary];
        fs.forEach(function(f) {
            html += '<div class="plan-policy-fact">· ' + escapeHtml(String(f)) + '</div>';
        });
        if (h.url) html += '<a class="plan-policy-src" href="' + escapeHtml(h.url) + '" target="_blank" rel="noopener">查看原文件 ↗</a>';
        html += '</div>';
    });
    html += '</div>';
    return html;
}

async function fetchPlanPolicy(item) {
    if (_planPolicyCache[item]) return;
    var q = _PLAN_POLICY_Q[item] || '';
    try {
        var res = await fetch('/api/policy_hits?item=' + encodeURIComponent(item) + '&q=' + encodeURIComponent(q));
        var data = await res.json();
        if (data && data.success) _planPolicyCache[item] = data.hits || [];
    } catch (err) { _planPolicyCache[item] = []; }
}

async function openPlanDetailItem(item) {
    var wrap = careerDockWrap();
    if (!wrap) return;
    closeSmartAnalysis();
    var empty = document.getElementById('toolDockEmpty');
    var back = document.getElementById('toolDockBack');
    var title = document.getElementById('toolDockTitle');
    _careerDetailJob = '';
    _planDetailItem = item;
    if (empty) empty.style.display = 'none';
    wrap.style.display = '';
    if (back) back.style.display = 'flex';
    if (title) title.textContent = item;
    var cacheKey = _careerMajor + '|' + item;
    if (_planDetailCache[cacheKey]) {
        renderPlanDetail(item, _planDetailCache[cacheKey]);
        return;
    }
    var policyHtml = '<div class="cd-loading">正在加载本校政策资料…</div>';
    wrap.innerHTML = policyHtml + '<div class="cd-loading">正在生成「' + escapeHtml(item) + '」分学年时间准备规划…<br><span style="font-size:11px;opacity:.75">首次生成约需30-60秒，生成后自动缓存</span></div>';
    fetchPlanPolicy(item).then(function() {
        if (_planDetailItem === item && !_planDetailCache[cacheKey]) {
            wrap.innerHTML = _renderPlanPolicyHtml(item) + '<div class="cd-loading">正在生成「' + escapeHtml(item) + '」分学年时间准备规划…<br><span style="font-size:11px;opacity:.75">首次生成约需30-60秒，生成后自动缓存</span></div>';
        }
    });
    try {
        var res = await fetch('/api/plan_detail?major=' + encodeURIComponent(_careerMajor) + '&item=' + encodeURIComponent(item));
        var data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || '生成失败');
        _planDetailCache[cacheKey] = data.data;
        renderPlanDetail(item, data.data);
    } catch (e) {
        wrap.innerHTML = _renderPlanPolicyHtml(item) + '<div class="cd-loading">规划生成失败：' + escapeHtml(e.message) + '<br><span class="cd-retry" onclick="retryPlanDetail()">点击重试</span></div>';
    }
}

function retryPlanDetail() {
    if (_planDetailItem) openPlanDetailItem(_planDetailItem);
}

function renderPlanDetail(item, plan) {
    var wrap = document.getElementById('careerDockWrap');
    if (!wrap) return;
    if (wrap.style.display === 'none' || _planDetailItem !== item) return;
    var title = String(plan.title || (_careerMajor + '专业' + item + '时间准备')).trim();
    var sections = (plan.sections || []).filter(function(s) { return s && String(s.name || '').trim() && (s.groups || []).length; });
    var html = '<div class="cd-head"><span class="cd-job">' + escapeHtml(item) + '</span>';
    html += '<div class="cd-sub">' + escapeHtml(_careerMajor) + ' · 分学年时间准备规划 · AI 生成</div></div>';
    html += '<div class="plan-title">' + escapeHtml(title) + '</div>';
    html += _renderPlanPolicyHtml(item);
    var idx = 0;
    sections.forEach(function(sec) {
        var groups = (sec.groups || []).filter(function(g) { return g && String(g.name || '').trim() && (g.items || []).length; });
        if (!groups.length) return;
        idx++;
        var isYear = /^大[一二三四]/.test(String(sec.name).trim());
        html += '<div class="plan-section' + (isYear ? ' plan-section-year' : '') + '"><div class="cd-sec-head"><span class="cd-sec-num">' + idx + '</span>';
        html += '<span class="cd-sec-name">' + escapeHtml(String(sec.name).trim()) + '</span>';
        if (isYear) html += '<span class="plan-year-badge">学年</span>';
        html += '</div><div class="plan-groups">';
        groups.forEach(function(g) {
            html += '<div class="plan-group"><div class="plan-group-name">' + escapeHtml(String(g.name).trim()) + '</div><div class="plan-group-items">';
            (g.items || []).forEach(function(it) {
                var t = String(it || '').trim();
                if (t) html += '<div class="plan-item">' + escapeHtml(t) + '</div>';
            });
            html += '</div></div>';
        });
        html += '</div></div>';
        if (idx < sections.length) html += '<div class="cd-arrow">▼</div>';
    });
    html += '<div style="font-size:11px;color:var(--text-muted);text-align:center;padding:10px 0 4px">由 AI 生成 · 时间线仅供参考，请以目标院校/招录单位最新公告为准</div>';
    wrap.innerHTML = html;
    var body = document.getElementById('toolDockBody');
    if (body) body.scrollTop = 0;
}

function renderCareerDetail(job, detail) {
    var wrap = document.getElementById('careerDockWrap');
    if (!wrap) return;
    if (wrap.style.display === 'none' || _careerDetailJob !== job) return;
    var sections = (detail.sections || []).filter(function(s) { return s && String(s.name || '').trim() && (s.items || []).length; });
    var html = '<div class="cd-head"><span class="cd-job">' + escapeHtml(job) + '</span>';
    html += '<div class="cd-sub">' + escapeHtml(String(detail.major || _careerMajor)) + ' · 九段式职业发展图 · AI 生成</div>';
    html += '<button class="cd-sa-btn" onclick="openSmartAnalysis(\'job\', _careerDetailJob)" title="查看该岗位的智能分析（知识搜索 + 大模型介绍）">🔍 智能分析该岗位</button></div>';
    var idx = 0;
    sections.forEach(function(sec) {
        var items = sec.items || [];
        if (!items.length) return;
        idx++;
        var isStage = String(sec.name).indexOf('学习阶段') >= 0;
        html += '<div class="cd-section"><div class="cd-sec-head"><span class="cd-sec-num">' + idx + '</span>';
        html += '<span class="cd-sec-name">' + escapeHtml(String(sec.name).trim()) + '</span></div>';
        html += '<div class="cd-items' + (isStage ? ' cd-items-stage' : '') + '">';
        items.forEach(function(it) {
            if (it && typeof it === 'object') {
                html += '<div class="cd-item-stage"><span class="cd-stage">' + escapeHtml(String(it.stage || '').trim() || '—') + '</span>';
                html += '<span class="cd-stage-text">' + escapeHtml(String(it.text || '').trim()) + '</span></div>';
            } else if (String(it || '').trim()) {
                html += '<div class="cd-item">' + escapeHtml(String(it).trim()) + '</div>';
            }
        });
        html += '</div></div>';
        if (idx < sections.length) html += '<div class="cd-arrow">▼</div>';
    });
    html += '<div style="font-size:11px;color:var(--text-muted);text-align:center;padding:10px 0 4px">由 AI 生成 · 内容仅供参考，建议结合培养方案核对</div>';
    wrap.innerHTML = html;
    var body = document.getElementById('toolDockBody');
    if (body) body.scrollTop = 0;
}

function closeCareerDetail() {
    var wrap = document.getElementById('careerDockWrap');
    var empty = document.getElementById('toolDockEmpty');
    var back = document.getElementById('toolDockBack');
    var title = document.getElementById('toolDockTitle');
    if (wrap) { wrap.style.display = 'none'; wrap.innerHTML = ''; }
    if (empty) empty.style.display = '';
    if (back) back.style.display = 'none';
    if (title) title.textContent = '工具区';
    _careerDetailJob = '';
    _planDetailItem = '';
    document.querySelectorAll('.ajo-job-active').forEach(function(x) { x.classList.remove('ajo-job-active'); });
    closeSmartAnalysis();
}

// ===== 智能分析 dock（点击课程/竞赛/技能/岗位 → 本地要素 + 知识搜索 + 大模型介绍）=====
var _SA_TYPE_LABEL = { 'course': '课程', 'comp': '竞赛', 'skill': '技能', 'job': '岗位' };
var _saCache = {};
var _saCur = null;

function saDockWrap() {
    var body = document.getElementById('toolDockBody');
    if (!body) return null;
    var empty = document.getElementById('toolDockEmpty');
    var wrap = document.getElementById('saDockWrap');
    if (!wrap) {
        wrap = document.createElement('div');
        wrap.id = 'saDockWrap';
        wrap.style.display = 'none';
        if (empty) body.insertBefore(wrap, empty); else body.appendChild(wrap);
    }
    return wrap;
}

function closeSmartAnalysis() {
    var wrap = document.getElementById('saDockWrap');
    if (wrap) { wrap.style.display = 'none'; wrap.innerHTML = ''; }
    _saCur = null;
}

function openCourseDetail(el) {
    var ci = parseInt(el.getAttribute('data-ci'), 10);
    var meta = M4_COURSE_INDEX[ci];
    if (!meta || !meta.name) return;
    openSmartAnalysis('course', meta.name, { cat: meta.cat, type: meta.type, credit: meta.credit, sem: meta.sem });
}

async function openSmartAnalysis(type, name, extra) {
    var wrap = saDockWrap();
    if (!wrap) return;
    name = String(name || '').trim();
    if (!name) return;
    closeCareerDetail();
    var empty = document.getElementById('toolDockEmpty');
    var back = document.getElementById('toolDockBack');
    var title = document.getElementById('toolDockTitle');
    if (empty) empty.style.display = 'none';
    wrap.style.display = '';
    if (back) back.style.display = 'flex';
    if (title) title.textContent = name;
    _saCur = { type: type, name: name };
    var meta = extra || {};
    var rows = [];
    if (meta.cat) rows.push('<div class="sa-kv"><span class="sa-k">分类</span><span class="sa-v">' + escapeHtml(meta.cat) + '</span></div>');
    if (meta.type) rows.push('<div class="sa-kv"><span class="sa-k">性质</span><span class="sa-v">' + escapeHtml(meta.type) + '</span></div>');
    if (meta.credit) rows.push('<div class="sa-kv"><span class="sa-k">学分</span><span class="sa-v">' + escapeHtml(String(meta.credit)) + '</span></div>');
    if (meta.sem) rows.push('<div class="sa-kv"><span class="sa-k">学期</span><span class="sa-v">' + escapeHtml(meta.sem) + '</span></div>');
    wrap.innerHTML = '<div class="sa-head"><span class="sa-name">' + escapeHtml(name) + '</span><span class="sa-type-badge">' + escapeHtml(_SA_TYPE_LABEL[type] || '条目') + '</span></div>' +
        (rows.length ? '<div class="sa-kvs">' + rows.join('') + '</div>' : '') +
        '<div class="sa-ai-box"><div class="cd-loading">正在结合知识库与大模型分析「' + escapeHtml(name) + '」…<br><span style="font-size:11px;opacity:.75">首次分析约需10-30秒，结果自动缓存</span></div></div>';
    if (type === 'comp') {
        fetch('/api/tuimian_bonus?name=' + encodeURIComponent(name)).then(function(r) { return r.json(); }).then(function(d) {
            if (!_saCur || _saCur.type !== type || _saCur.name !== name || !d || !d.success) return;
            if (document.getElementById('saBonusBadge')) return;
            var div = document.createElement('div');
            div.id = 'saBonusBadge';
            var college = escapeHtml(d.college || '本学院');
            var yr = d.year || '';
            var matched = (d.matched || []).slice(0, 3).join(' / ');
            if (d.in_list) {
                var r = d.rules || {};
                div.className = 'sa-bonus sa-bonus-ok';
                div.innerHTML = '<span class="sa-bonus-icon">✓</span><span>在推免加分名单内（' + college + yr + '年推免细则）</span>' +
                    '<span class="sa-bonus-sub">' + escapeHtml([r.subject_cap, r.level_weights, r.team_rule, r.regional_rule].filter(Boolean).join('；')) +
                    (matched && matched !== name ? '。匹配名单项：' + escapeHtml(matched) : '') + '</span>';
            } else {
                div.className = 'sa-bonus sa-bonus-no';
                div.innerHTML = '<span class="sa-bonus-icon">—</span><span>暂不在推免加分名单内（' + college + yr + '年推免细则，共20项）</span>' +
                    '<span class="sa-bonus-sub">名单见学院推免实施细则附件；文体竞赛按细则另行认定，以学院当年最新名单为准</span>';
            }
            var aiBox = wrap.querySelector('.sa-ai-box');
            if (aiBox) aiBox.parentNode.insertBefore(div, aiBox);
        }).catch(function() {});
    }
    var cacheKey = (profileCache.major || '') + '|' + type + '|' + name;
    if (_saCache[cacheKey]) {
        fillSmartAnalysis(_saCache[cacheKey], wrap);
        return;
    }
    try {
        var res = await fetch('/api/smart_analysis?type=' + encodeURIComponent(type) + '&name=' + encodeURIComponent(name) + '&major=' + encodeURIComponent(profileCache.major || ''));
        var data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || '分析失败');
        _saCache[cacheKey] = data.data;
        if (!_saCur || _saCur.type !== type || _saCur.name !== name) return;
        fillSmartAnalysis(data.data, wrap);
    } catch (e) {
        if (!_saCur || _saCur.type !== type || _saCur.name !== name) return;
        var box = wrap.querySelector('.sa-ai-box');
        if (box) box.innerHTML = '<div class="cd-loading">智能分析失败：' + escapeHtml(e.message) + '<br><span class="cd-retry" onclick="retrySmartAnalysis()">点击重试</span></div>';
    }
}

function retrySmartAnalysis() {
    if (_saCur) openSmartAnalysis(_saCur.type, _saCur.name);
}

function fillSmartAnalysis(d, wrap) {
    var box = wrap.querySelector('.sa-ai-box');
    if (!box) return;
    var html = '';
    var hits = (d.knowledge_hits || []).filter(function(h) { return h && String(h.title || '').trim(); });
    if (hits.length) {
        html += '<div class="sa-sec"><div class="sa-sec-label">知识库匹配</div>';
        hits.forEach(function(h) {
            html += '<div class="sa-hit"><a href="' + escapeHtml(String(h.url || '#')) + '" target="_blank" rel="noopener">' + escapeHtml(String(h.title).trim()) + '</a>';
            if (String(h.snippet || '').trim()) html += '<div class="sa-hit-text">' + escapeHtml(String(h.snippet).trim()) + '</div>';
            html += '</div>';
        });
        html += '</div>';
    }
    if (String(d.intro || '').trim()) html += '<div class="sa-sec"><div class="sa-sec-label">条目简介</div><div class="sa-text">' + escapeHtml(String(d.intro).trim()) + '</div></div>';
    if (String(d.difficulty || '').trim()) html += '<div class="sa-sec"><div class="sa-sec-label">难度分析</div><div class="sa-text sa-diff">' + escapeHtml(String(d.difficulty).trim()) + '</div></div>';
    var prereq = (d.prereq || []).map(function(x) { return String(x || '').trim(); }).filter(Boolean);
    if (prereq.length) html += '<div class="sa-sec"><div class="sa-sec-label">先修基础</div><div class="sa-tags">' + prereq.map(function(x) { return '<span class="sa-tag">' + escapeHtml(x) + '</span>'; }).join('') + '</div></div>';
    var advice = (d.advice || []).map(function(x) { return String(x || '').trim(); }).filter(Boolean);
    if (advice.length) html += '<div class="sa-sec"><div class="sa-sec-label">学习建议</div><ul class="sa-list">' + advice.map(function(x) { return '<li>' + escapeHtml(x) + '</li>'; }).join('') + '</ul></div>';
    if (!html) html = '<div class="sa-empty">暂无分析结果</div>';
    box.innerHTML = '<div class="sa-ai-badge">智能分析 · 知识库 + 大模型</div>' + html;
    var body = document.getElementById('toolDockBody');
    if (body) body.scrollTop = 0;
}

async function handleImportText() {
    const text = document.getElementById('importTextArea').value.trim();
    if (!text) {
        showToast('请粘贴网页正文', 'error');
        return;
    }
    const title = document.getElementById('importTitleInput').value.trim();
    showToast('正在抽取，约需10-60秒…', 'info');
    showLoading(true);
    try {
        const res = await fetch('/api/import_text', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text, title: title, doc_type: document.getElementById('importDocType').value })
        });
        const data = await res.json();
        if (data.success) {
            openImportPreview(data);
        } else {
            showToast(data.error || '抽取失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    } finally {
        showLoading(false);
    }
}

function openImportPreview(data) {
    importPreviewData = data;
    const typeTag = data.doc_type_label ? `（${data.doc_type_label}）` : '';
    const channelTag = data.channel === 'image' ? '图片OCR识别预览' : '网页抽取预览';
    document.getElementById('importPreviewTitle').textContent = `${channelTag}：${data.title}${typeTag}`;
    const body = document.getElementById('importPreviewBody');
    let html = '';
    if (data.truncated) {
        html += '<div class="import-empty">⚠️ 材料过长，已截断前30000字参与抽取</div>';
    }
    let totalRows = 0;
    for (const dim of Object.keys(data.tables)) {
        const t = data.tables[dim];
        totalRows += t.rows.length;
        html += `<div class="import-dim-block"><div class="import-dim-title">${escapeHtml(dim)}（${t.rows.length} 行）</div>`;
        if (t.rows.length === 0) {
            html += '<div class="import-empty">该材料未抽取到此维度数据</div></div>';
            continue;
        }
        html += '<div class="import-table-wrap"><table class="import-table"><thead><tr>';
        for (const h of t.headers) html += `<th>${escapeHtml(h)}</th>`;
        html += '</tr></thead><tbody>';
        for (const row of t.rows) {
            html += '<tr>';
            for (const cell of row) html += `<td>${escapeHtml(cell)}</td>`;
            html += '</tr>';
        }
        html += '</tbody></table></div></div>';
    }
    body.innerHTML = html;
    document.getElementById('importPreviewSummary').textContent =
        totalRows > 0 ? `共抽取 ${totalRows} 行数据，确认后写入新文件夹并导入图谱` : '未抽取到任何数据';
    document.getElementById('importConfirmBtn').disabled = totalRows === 0;
    const shareRow = document.getElementById('importShareRow');
    shareRow.style.display = currentUser ? '' : 'none';
    document.getElementById('importShareCheckbox').checked = false;
    showModal('importPreviewModal');
}

async function confirmImport() {
    if (!importPreviewData) return;
    const btn = document.getElementById('importConfirmBtn');
    btn.disabled = true;
    btn.textContent = '导入中…';
    try {
        const res = await fetch('/api/import_confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: importPreviewData.title,
                source_label: importPreviewData.source_label,
                doc_type: importPreviewData.doc_type || 'auto',
                doc_type_label: importPreviewData.doc_type_label || '',
                channel: importPreviewData.channel || 'web',
                image_token: importPreviewData.image_token || '',
                share: document.getElementById('importShareCheckbox').checked,
                tables: importPreviewData.tables
            })
        });
        const data = await res.json();
        if (data.success) {
            const dims = Object.keys(data.results || {})
                .map(d => `${d}(${data.results[d].nodes}节点/${data.results[d].edges}边)`)
                .join('、');
            showToast(`已创建文件夹「${data.folder}」并导入：${dims}`, 'success');
            closeModal('importPreviewModal');
            await loadFolders();
            const select = document.getElementById('folderSelect');
            for (const opt of select.options) {
                if (opt.textContent.indexOf(data.folder) === 0) {
                    select.value = opt.value;
                    await selectFolder(opt.value);
                    break;
                }
            }
        } else {
            showToast(data.error || '导入失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '确认导入';
    }
}

// ===== 账号体系（r13：登录/注册/退出，个人数据隔离）=====
var currentUser = null;

async function checkAuth() {
    try {
        const res = await fetch('/api/auth/me');
        const data = await res.json();
        currentUser = data.user || null;
    } catch (e) {
        currentUser = null;
    }
    updateAccountUI();
}

function updateAccountUI() {
    const loggedOut = document.getElementById('accountLoggedOut');
    const loggedIn = document.getElementById('accountLoggedIn');
    if (!loggedOut || !loggedIn) return;
    if (currentUser) {
        loggedOut.style.display = 'none';
        loggedIn.style.display = 'block';
        document.getElementById('accountWelcome').textContent = `👤 已登录：${currentUser.username}（数据独立保存）`;
    } else {
        loggedOut.style.display = 'block';
        loggedIn.style.display = 'none';
    }
}

async function handleLogin() {
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value;
    if (!username || !password) {
        showToast('请输入用户名和密码', 'error');
        return;
    }
    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, password: password })
        });
        const data = await res.json();
        if (data.success) {
            currentUser = data.user;
            document.getElementById('loginPassword').value = '';
            updateAccountUI();
            showToast(`欢迎回来，${data.user.username}`, 'success');
            await loadFolders();
        } else {
            showToast(data.error || '登录失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    }
}

async function handleRegister() {
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value;
    if (!username || !password) {
        showToast('请输入用户名和密码', 'error');
        return;
    }
    try {
        const res = await fetch('/api/auth/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, password: password })
        });
        const data = await res.json();
        if (data.success) {
            currentUser = data.user;
            document.getElementById('loginPassword').value = '';
            updateAccountUI();
            showToast('注册成功，已为你建立独立个人数据库', 'success');
            await loadFolders();
        } else {
            showToast(data.error || '注册失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    }
}

async function handleLogout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (e) { /* 本地仍按游客处理 */ }
    currentUser = null;
    updateAccountUI();
    showToast('已退出登录，切换为游客模式', 'info');
    await loadFolders();
}

// ===== 通道C · 图片OCR上传 =====
async function handleImportImage(input) {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    const title = document.getElementById('importImageTitleInput').value.trim();
    showToast('正在OCR识别图片文字并抽取，约需30-90秒…', 'info');
    showLoading(true);
    try {
        const fd = new FormData();
        fd.append('file', file, file.name);
        fd.append('title', title);
        fd.append('doc_type', document.getElementById('importDocType').value);
        const res = await fetch('/api/import_image', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.success) {
            const rows = Object.values(data.tables).reduce((n, t) => n + t.rows.length, 0);
            showToast(`OCR识别到约${data.ocr_chars}字，抽取${rows}行`, 'success');
            openImportPreview(data);
        } else {
            showToast(data.error || '图片识别失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    } finally {
        showLoading(false);
    }
}

// ===== 通道D · Excel六表直传 =====
async function handleUploadExcel(input) {
    const files = Array.from(input.files || []);
    input.value = '';
    if (files.length === 0) return;
    const folderName = document.getElementById('importExcelNameInput').value.trim();
    const share = document.getElementById('excelShareCheckbox').checked;
    showLoading(true);
    try {
        const fd = new FormData();
        for (const f of files) fd.append('files', f, f.name);
        if (folderName) fd.append('folder_name', folderName);
        if (share) fd.append('share', '1');
        const res = await fetch('/api/upload', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.success) {
            const dims = (data.dimensions || []).map(d => d.dimension).join('、');
            const zone = (currentUser && !share) ? '个人资料区' : '公共资料库';
            showToast(`已上传到「${data.folder_name}」（${zone}）${dims ? '，识别到维度：' + dims : ''}`, 'success');
            document.getElementById('excelShareCheckbox').checked = false;
            await loadFolders();
            if (data.folder) {
                const select = document.getElementById('folderSelect');
                for (const opt of select.options) {
                    if (opt.value === data.folder) {
                        select.value = opt.value;
                        await selectFolder(opt.value);
                        break;
                    }
                }
            }
        } else {
            showToast(data.error || '上传失败', 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    } finally {
        showLoading(false);
    }
}
