/* =============================================================================
   共享前端脚本：ECharts 主题、图表辅助函数、SQL 高亮
   ============================================================================= */

/* ---------------------------------------------------------------- 配色
   参考农业数据可视化的常用色板：绿色系为主（作物/生长），
   橙色系作对比（成本/风险），灰色系作背景维度。 */
const AGRI = {
    green:  ['#1b5e20', '#2e7d32', '#43a047', '#66bb6a', '#a5d6a7', '#c8e6c9'],
    orange: ['#e65100', '#ef6c00', '#f57c00', '#fb8c00', '#ffa726', '#ffcc80'],
    blue:   ['#0d47a1', '#1565c0', '#1976d2', '#42a5f5', '#90caf9', '#bbdefb'],
    gray:   '#7b8a7e',
    text:   '#3a4a3c',
    axis:   '#dfe5e0'
};

/* ---------------------------------------------------------------- 通用配置 */
const CHART_BASE = {
    textStyle: { fontFamily: '"Microsoft YaHei", "PingFang SC", sans-serif', fontSize: 12 },
    color: [].concat(AGRI.green, AGRI.orange, AGRI.blue),
    grid: { left: 56, right: 34, top: 42, bottom: 42, containLabel: true },
    tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(31,45,36,.94)',
        borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
        padding: [8, 12]
    },
    legend: { top: 6, itemWidth: 11, itemHeight: 11, textStyle: { color: AGRI.text, fontSize: 12 } }
};

/** 合并基础配置，避免每张图重复写公共部分 */
function chartOpt(extra) {
    return Object.assign({}, CHART_BASE, extra);
}

/** 初始化一个 ECharts 实例并绑定响应式 resize。
 *  ⚠️ window.resize 里如果不调用 instance.resize()，侧边栏折叠或
 *     窗口缩放后图表会保持旧尺寸，出现空白或拉伸。 */
function initChart(elId, option) {
    const el = document.getElementById(elId);
    if (!el) return null;
    const chart = echarts.init(el);
    chart.setOption(option);
    window.addEventListener('resize', () => chart.resize());
    return chart;
}

/** 坐标轴通用样式 */
function axisStyle(name) {
    return {
        type: 'category',
        name: name || '',
        nameTextStyle: { color: AGRI.gray, fontSize: 11 },
        axisLine: { lineStyle: { color: AGRI.axis } },
        axisLabel: { color: AGRI.gray, fontSize: 11 },
        axisTick: { show: false }
    };
}

function valueAxis(name, formatter) {
    return {
        type: 'value',
        name: name || '',
        nameTextStyle: { color: AGRI.gray, fontSize: 11 },
        axisLine: { show: false },
        axisLabel: { color: AGRI.gray, fontSize: 11, formatter: formatter },
        splitLine: { lineStyle: { color: '#eef2ee' } }
    };
}

/* ---------------------------------------------------------------- SQL 高亮
   页面上展示的 SQL 就是实际执行的 SQL（后端用注册表保证），
   这里只负责着色。highlight.js 的 sql 语言包在 common 构建里已包含。 */
function renderSql() {
    if (typeof hljs === 'undefined') return;
    document.querySelectorAll('.sql-block pre code').forEach(el => {
        hljs.highlightElement(el);
    });
}

/* ---------------------------------------------------------------- 复制 SQL */
function copySql(btn) {
    const pre = btn.closest('.sql-block').querySelector('pre');
    const text = pre.innerText;
    navigator.clipboard.writeText(text).then(() => {
        const old = btn.innerHTML;
        btn.innerHTML = '<i class="bi bi-check2"></i> 已复制';
        setTimeout(() => { btn.innerHTML = old; }, 1600);
    }).catch(() => {
        alert('复制失败，请手动选择文本');
    });
}

/* ---------------------------------------------------------------- 数字格式 */
const fmt = {
    num: (v, d = 0) => (v === null || v === undefined || isNaN(v))
        ? '-' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }),
    pct: (v, d = 1) => (v === null || v === undefined || isNaN(v)) ? '-' : Number(v).toFixed(d) + '%',
    signed: (v, d = 1) => (v === null || v === undefined || isNaN(v))
        ? '-' : (v >= 0 ? '+' : '') + Number(v).toFixed(d) + '%'
};

/* ---------------------------------------------------------------- 同比环比着色 */
function signedColor(v) {
    if (v === null || v === undefined) return AGRI.gray;
    return Number(v) >= 0 ? '#2e7d32' : '#c62828';
}

document.addEventListener('DOMContentLoaded', () => {
    renderSql();
    // 侧边栏当前项的 active 状态由模板设置，这里处理锚点导航
    document.querySelectorAll('.anchor-nav a').forEach(a => {
        a.addEventListener('click', e => {
            e.preventDefault();
            const t = document.querySelector(a.getAttribute('href'));
            if (t) window.scrollTo({ top: t.offsetTop - 70, behavior: 'smooth' });
            document.querySelectorAll('.anchor-nav a').forEach(x => x.classList.remove('active'));
            a.classList.add('active');
        });
    });
});
