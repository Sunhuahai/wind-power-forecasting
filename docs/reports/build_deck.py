#!/usr/bin/env python3
"""Build the database selection report deck (20 slides, 16:9).

Content source: docs/database-selection.md (merged report, 2026-11-16).
"""
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

FONT = "PingFang SC"
NAVY = RGBColor(0x1B, 0x2A, 0x41)
BLUE = RGBColor(0x2F, 0x5D, 0x8A)
TEAL = RGBColor(0x2A, 0x7F, 0x7F)
ORANGE = RGBColor(0xC0, 0x72, 0x3C)
GRAY = RGBColor(0x5A, 0x62, 0x70)
LIGHT = RGBColor(0xF2, 0xF5, 0xF8)
LINE = RGBColor(0xD8, 0xDF, 0xE6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
RED = RGBColor(0xA8, 0x3A, 0x32)

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]

PAGE = {"n": 0}


def box(slide, x, y, w, h):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    return tf


def east_asian(run):
    """Make CJK use the same typeface as latin (python-pptx only sets a:latin)."""
    rPr = run._r.get_or_add_rPr()
    latin = rPr.find(qn("a:latin"))
    if latin is None:
        return
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        from pptx.oxml import parse_xml
        from pptx.oxml.ns import nsdecls
        ea = parse_xml(f'<a:ea {nsdecls("a")} typeface="{FONT}"/>')
        latin.addnext(ea)


def para(tf, text, size=16, color=GRAY, bold=False, space_after=6, space_before=0,
         first=False, line=None, align=PP_ALIGN.LEFT):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_after = Pt(space_after)
    p.space_before = Pt(space_before)
    if line:
        p.line_spacing = line
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.color.rgb = color
    r.font.name = FONT
    east_asian(r)
    return p


def rect(slide, x, y, w, h, fill=None, line=None, line_w=1.0):
    from pptx.enum.shapes import MSO_SHAPE
    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.shadow.inherit = False
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid()
        sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
        sh.line.width = Pt(line_w)
    # Drop the theme style so LibreOffice does not re-apply effectRef shadows.
    style = sh._element.find(qn("p:style"))
    if style is not None:
        sh._element.remove(style)
    return sh


def footer(slide, note=""):
    PAGE["n"] += 1
    if note:
        tf = box(slide, 0.75, 6.92, 10.0, 0.32)
        para(tf, note, 9.5, GRAY, first=True)
    tf = box(slide, 11.9, 6.92, 0.7, 0.32)
    para(tf, f"{PAGE['n']:02d}", 10, GRAY, first=True, align=PP_ALIGN.RIGHT)


def content_slide(title, kicker="", note=""):
    s = prs.slides.add_slide(BLANK)
    rect(s, 0, 0, 0.13, 7.5, fill=TEAL)
    tf = box(s, 0.75, 0.42, 11.6, 0.55)
    para(tf, title, 27, NAVY, bold=True, first=True)
    if kicker:
        tf = box(s, 0.75, 1.02, 11.6, 0.4)
        para(tf, kicker, 14, GRAY, first=True)
    rect(s, 0.75, 1.42, 11.83, 0.014, fill=LINE)
    footer(s, note)
    return s


def section_slide(num, title, items):
    s = prs.slides.add_slide(BLANK)
    rect(s, 0, 0, 13.333, 7.5, fill=NAVY)
    rect(s, 0.9, 2.35, 0.08, 2.1, fill=TEAL)
    tf = box(s, 1.25, 2.2, 3.0, 1.0)
    para(tf, num, 54, TEAL, bold=True, first=True)
    tf = box(s, 1.25, 3.25, 10.5, 0.9)
    para(tf, title, 34, WHITE, bold=True, first=True)
    tf = box(s, 1.25, 4.35, 10.5, 1.4)
    first = True
    for it in items:
        para(tf, it, 15, RGBColor(0xC9, 0xD3, 0xDE), first=first, space_after=4)
        first = False
    PAGE["n"] += 1
    tf = box(s, 11.9, 6.92, 0.7, 0.32)
    para(tf, f"{PAGE['n']:02d}", 10, RGBColor(0x7F, 0x8C, 0x9C), first=True, align=PP_ALIGN.RIGHT)
    return s


def table(slide, data, x, y, w, col_ratios, size=12, header_size=12, row_h=0.36,
          header_fill=NAVY, zebra=True, align_center_from=None, bold_first_col=False):
    rows, cols = len(data), len(data[0])
    shp = slide.shapes.add_table(rows, cols, Inches(x), Inches(y), Inches(w), Inches(row_h * rows))
    tbl = shp.table
    tbl.first_row = True
    tbl.horz_banding = False
    total = sum(col_ratios)
    for i, r in enumerate(col_ratios):
        tbl.columns[i].width = Inches(w * r / total)
    for ri, row in enumerate(data):
        tbl.rows[ri].height = Inches(row_h)
        for ci, val in enumerate(row):
            cell = tbl.cell(ri, ci)
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.08)
            cell.margin_top = Inches(0.03)
            cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT
            if align_center_from is not None and ci >= align_center_from and ri > 0:
                p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = str(val)
            r.font.name = FONT
            east_asian(r)
            if ri == 0:
                r.font.size = Pt(header_size)
                r.font.bold = True
                r.font.color.rgb = WHITE
                cell.fill.solid()
                cell.fill.fore_color.rgb = header_fill
            else:
                r.font.size = Pt(size)
                r.font.color.rgb = NAVY if (bold_first_col and ci == 0) else RGBColor(0x3A, 0x42, 0x50)
                r.font.bold = bool(bold_first_col and ci == 0)
                cell.fill.solid()
                cell.fill.fore_color.rgb = LIGHT if (zebra and ri % 2 == 0) else WHITE
    return tbl


# ---------------------------------------------------------------- 01 cover
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, fill=NAVY)
rect(s, 0, 5.55, 13.333, 0.05, fill=TEAL)
tf = box(s, 1.0, 1.55, 11.3, 1.0)
para(tf, "风电功率预测系统", 20, TEAL, bold=True, first=True)
tf = box(s, 1.0, 2.15, 11.3, 1.5)
para(tf, "数据库选型与部署方案", 44, WHITE, bold=True, first=True)
tf = box(s, 1.0, 3.35, 11.3, 0.6)
para(tf, "从「有哪些类型」到「装在什么设备上」——一份可决策的汇报", 18, RGBColor(0xC9, 0xD3, 0xDE), first=True)
tf = box(s, 1.0, 5.85, 11.3, 1.2)
para(tf, "一、数据库类型    二、我们的需求    三、为什么选它    四、如何应用    五、部署设备",
     15, RGBColor(0x9F, 0xAE, 0xBF), first=True)
tf = box(s, 1.0, 6.55, 11.3, 0.4)
para(tf, "2026-11-16", 13, RGBColor(0x7F, 0x8C, 0x9C), first=True)
PAGE["n"] += 1

# ---------------------------------------------------------------- 02 summary
s = content_slide("一页结论", "读完这一页，后面五章都是它的展开", "数据来源：本项目实测 + TimescaleDB 官方文档（2026-11-16）")
kpis = [
    ("生产主库", "PostgreSQL 18.6\n+ TimescaleDB 2.32", "Linux 部署，不追最新小版本", BLUE),
    ("存储收益", "约 −90%\n（列存压缩实测）", "三年数据单机放得下", TEAL),
    ("生产口径评分", "93.6\n（裸 PG 为 83.2）", "同权重对比，非性能排名", ORANGE),
    ("当前动作", "备而不发\n等真实数据触发", "迁移草案已备，未执行", GRAY),
]
for i, (label, value, caption, color) in enumerate(kpis):
    x = 0.78 + i * 3.02
    rect(s, x, 1.72, 2.86, 1.85, fill=LIGHT)
    rect(s, x, 1.72, 0.05, 1.85, fill=color)
    tf = box(s, x + 0.22, 1.86, 2.55, 0.3)
    para(tf, label, 11.5, GRAY, first=True)
    tf = box(s, x + 0.22, 2.2, 2.55, 1.2)
    lines = value.split("\n")
    para(tf, lines[0], 21 if len(lines[0]) < 12 else 16, NAVY, bold=True, first=True, space_after=2)
    if len(lines) > 1:
        para(tf, lines[1], 12.5, GRAY, space_after=0)
    tf = box(s, x + 0.22, 3.08, 2.55, 0.35)
    para(tf, caption, 9.5, RGBColor(0x8A, 0x93, 0xA0), first=True)

tf = box(s, 0.8, 3.95, 11.7, 0.4)
para(tf, "三句话讲完", 15, NAVY, bold=True, first=True)
rows = [
    ("不是二选一", "TimescaleDB 是 PostgreSQL 扩展。事务、外键、JSONB、备份、HA、ORM 全部沿用，应用代码一行不改。"),
    ("代价只有两条", "扩展与 PG 小版本耦合（2.32.0 只支持到 18.6）；TSL 许可下不能把数据库能力当服务对外售卖。"),
    ("现在就装、暂不迁移", "生产实例直接发行 TimescaleDB；本机实验库不动。真实数据到位、满足三条启动条件再执行迁移草案。"),
]
y = 4.42
for tag, text in rows:
    rect(s, 0.8, y, 1.95, 0.42, fill=NAVY)
    tf = box(s, 0.9, y + 0.08, 1.8, 0.3)
    para(tf, tag, 11.5, WHITE, bold=True, first=True)
    tf = box(s, 2.95, y + 0.03, 9.6, 0.6)
    para(tf, text, 13, RGBColor(0x3A, 0x42, 0x50), first=True, line=1.15)
    y += 0.78
s.notes_slide.notes_text_frame.text = (
    "开场先给结论，避免听众在细节里失焦。三个数字：主库形态、约 90% 的存储收益、"
    "生产口径 93.6 对 83.2。最后一行强调：这不是一次迁移项目，现在不做任何动作。"
)

# ---------------------------------------------------------------- 03 section 1
section_slide("01", "数据库有哪些类型",
              ["按存储布局分：行存、列存、LSM 树",
               "按用途分：业务主库、时序库、分析库、缓存、文件格式",
               "类型只决定「擅长什么负载」，不决定「能不能做业务库」"])

# ---------------------------------------------------------------- 04 types
s = content_slide("六类数据库，各有各的活", "先看全图，再判断我们在哪一格", "分类为工程视角的归纳，非严格学术分类")
cards = [
    ("通用关系型（OLTP）", "PostgreSQL、MySQL、SQLite", "行存 + 事务 + 约束", "业务数据、关系、幂等写入", BLUE),
    ("时序数据库（TSDB）", "TimescaleDB、InfluxDB、QuestDB", "按时间分块 + 压缩 + 保留策略", "高频打点、按时间窗查询", TEAL),
    ("列式分析型（OLAP）", "ClickHouse、DuckDB、StarRocks", "列存 + 向量化 + 高压缩", "海量扫描、报表聚合", ORANGE),
    ("嵌入式 / 单文件", "SQLite、DuckDB 嵌入模式", "库即文件，零运维", "单机工具、边缘、离线分析", RGBColor(0x6B, 0x7A, 0x8C)),
    ("文档 / 键值", "MongoDB、Redis", "无固定 schema、内存优先", "半结构化数据、缓存", RGBColor(0x8A, 0x6D, 0x9E)),
    ("列式文件格式（非数据库）", "Parquet、ORC", "静态列式文件，无服务无事务", "数据交换、训练样本快照", RGBColor(0x7A, 0x8B, 0x5A)),
]
for i, (name, products, mech, use, color) in enumerate(cards):
    col, row = i % 3, i // 3
    x = 0.78 + col * 4.02
    y = 1.75 + row * 2.42
    rect(s, x, y, 3.82, 2.15, fill=WHITE, line=LINE)
    rect(s, x, y, 3.82, 0.055, fill=color)
    tf = box(s, x + 0.22, y + 0.18, 3.4, 0.36)
    para(tf, name, 14.5, NAVY, bold=True, first=True)
    tf = box(s, x + 0.22, y + 0.62, 3.4, 0.3)
    para(tf, products, 10.5, color, bold=True, first=True)
    tf = box(s, x + 0.22, y + 1.02, 3.4, 0.6)
    para(tf, mech, 11.5, GRAY, first=True, line=1.1)
    tf = box(s, x + 0.22, y + 1.62, 3.4, 0.4)
    para(tf, "适合：" + use, 11, RGBColor(0x3A, 0x42, 0x50), first=True)
tf = box(s, 0.8, 6.5, 11.7, 0.35)
para(tf, "注意：TimescaleDB 不是「另一个数据库」，它是 PostgreSQL 的扩展——这决定了我们不面临「换库」。", 12.5, TEAL, bold=True, first=True)

# ---------------------------------------------------------------- 05 mechanism
s = content_slide("四种存储布局，决定了各自擅长的负载", "选型本质上是选「哪一种物理布局更贴合我们的读写模式」")
data = [
    ["维度", "行存关系库（PG / MySQL）", "时序库（TimescaleDB）", "列式分析库（ClickHouse）", "LSM 时序库（InfluxDB）"],
    ["存储布局", "按行连续存放", "按时间切片 + 冷数据转列存", "全部列存", "LSM 树，写入即追加"],
    ["单条更新 / 订正", "原生支持，MVCC", "原生支持（压缩块内有代价）", "弱：靠后台合并，读到重复", "弱：不可变文件，覆盖不自然"],
    ["按时间清理历史", "DELETE + VACUUM，会膨胀", "DROP CHUNK，瞬时", "DROP PARTITION", "按保留策略删分片"],
    ["事务 / 外键 / 约束", "完整", "完整（继承 PG）", "弱，无标准外键", "无完整 ACID"],
    ["最擅长", "业务数据、关系、实验追溯", "时间序列 + 关系 + 压缩", "海量扫描聚合", "超高频遥测写入"],
]
table(s, data, 0.75, 1.66, 11.85, [1.5, 2.6, 2.8, 2.5, 2.45], size=11, header_size=11, row_h=0.78)
tf = box(s, 0.8, 6.55, 11.7, 0.4)
para(tf, "结论：只有关系型（含其扩展）能当业务主库；时序与分析型要么是同一库里的增强，要么是副库。", 13, TEAL, bold=True, first=True)

# ---------------------------------------------------------------- 06 section 2
section_slide("02", "我们的需求是什么",
              ["不是「存时间序列」，而是「管关系 + 幂等写入 + 可回放 + 可追溯 + 能长大」",
               "数据量现在很小，但增长曲线由 NWP 与预测明细决定"])

# ---------------------------------------------------------------- 07 needs
s = content_slide("七条硬需求，决定了候选范围", "左侧是需求，右侧是现在必须满足的程度", "数据统计：2026-11-16 实测 pg_stat_user_tables")
data = [
    ["需求", "具体场景", "对库的要求"],
    ["关系与完整性", "功率属场站、预测属实验、实验关联模型", "主外键、约束、事务"],
    ["幂等写入", "重复导入、任务重试、预测重跑", "唯一键 + ON CONFLICT"],
    ["时间窗查询", "取某站某段时间的历史与预报", "时间索引 + 分区裁剪"],
    ["历史回放", "还原「那一刻真实可用的预报」", "发布 / 到达 / 有效 三种时间"],
    ["实验追溯", "每条预测追到模型、参数、数据版本", "实验表 + 输入版本标识"],
    ["历史清理", "按年限保留、按期归档", "按块删除，不膨胀"],
    ["在线服务", "每日出预报、看板查询、失败要能查", "并发、备份、可恢复"],
]
table(s, data, 0.75, 1.68, 8.55, [1.55, 3.7, 3.3], size=11.5, header_size=11.5, row_h=0.62, bold_first_col=True)
box_x = 9.65
rect(s, box_x, 1.68, 2.95, 3.3, fill=LIGHT)
tf = box(s, box_x + 0.22, 1.82, 2.55, 0.3)
para(tf, "现在的规模", 12, GRAY, bold=True, first=True)
stack = [("整库", "115 MB"), ("三张主表", "47.8 万行"), ("场站", "10 个"), ("覆盖", "2012–2014")]
yy = 2.22
for label, value in stack:
    tf = box(s, box_x + 0.22, yy, 1.35, 0.3)
    para(tf, label, 11, GRAY, first=True)
    tf = box(s, box_x + 1.55, yy - 0.04, 1.25, 0.34)
    para(tf, value, 15, NAVY, bold=True, first=True, align=PP_ALIGN.RIGHT)
    yy += 0.62
tf = box(s, box_x, 5.15, 2.95, 1.2)
para(tf, "规模不是理由，增长曲线才是。", 12, TEAL, bold=True, first=True, line=1.2)
para(tf, "见下一页。", 11, GRAY)

# ---------------------------------------------------------------- 08 growth
s = content_slide("真正会涨的是 NWP 和预测明细，不是功率观测", "同一套「按时间查、按时间删、按时间压」的需求，落在这两张表上")
data = [
    ["场景", "计算口径", "每年行数", "量级"],
    ["现在（10 站，每小时）", "10 × 8,760", "87,600", "十万级"],
    ["50 站，15 分钟功率观测", "50 × 96 × 365", "175 万", "百万级"],
    ["50 站，NWP 每天 4 批 × 72 提前量", "50 × 4 × 72 × 365", "526 万", "百万级（最大）"],
    ["50 站，8 次预测 × 24 小时", "50 × 8 × 24 × 365", "350 万", "百万级"],
    ["机组级遥测（2000 台，仅作对照）", "2000 × 96 × 365", "7,000 万", "千万级"],
]
table(s, data, 0.75, 1.68, 8.3, [3.4, 2.3, 1.5, 1.4], size=11.5, header_size=11.5, row_h=0.72,
      align_center_from=2, bold_first_col=True)
rect(s, 9.45, 1.68, 3.15, 4.3, fill=RGBColor(0xFB, 0xF3, 0xEC), line=ORANGE)
tf = box(s, 9.68, 1.85, 2.7, 4.0)
para(tf, "这张表的读法", 12.5, ORANGE, bold=True, first=True)
para(tf, "NWP 是功率观测的 3 倍行数。", 12.5, RGBColor(0x3A, 0x42, 0x50), space_before=4, line=1.25)
para(tf, "所以「才百万行、不用着急」看错了增长最快的那张表。", 12.5, RGBColor(0x3A, 0x42, 0x50), space_before=4, line=1.25)
para(tf, "这三张表的共同点：按时间查、按时间删、按时间压。", 12.5, NAVY, bold=True, space_before=6, line=1.25)
tf = box(s, 0.8, 6.35, 11.7, 0.45)
para(tf, "推论：分块、裁剪、列存压缩、按块删除——不是「未来才需要」，而是这三张表的标配。", 13, TEAL, bold=True, first=True)

# ---------------------------------------------------------------- 09 section 3
section_slide("03", "为什么选这个数据库",
              ["生产口径重打分：TimescaleDB 93.6，裸 PostgreSQL 83.2",
               "两条代价必须一起讲：版本耦合、许可证"])

# ---------------------------------------------------------------- 10 scoring
s = content_slide("把权重换成生产口径，结论就变了", "旧口径按「本机 115MB」打分；新口径按「多年、多站、在线服务」打分")
data = [
    ["数据库", "关系完整性", "事务与幂等", "时序能力", "分析聚合", "迁移与生态", "运维成本", "总分"],
    ["TimescaleDB", "5", "5", "5", "5", "5", "3", "93.6"],
    ["PostgreSQL（裸）", "5", "5", "3", "3", "5", "4", "83.2"],
    ["MySQL", "5", "5", "2", "2", "3", "4", "70.0"],
    ["ClickHouse", "1", "2", "4", "5", "2", "2", "55.6"],
    ["InfluxDB 3", "1", "2", "4", "3", "2", "3", "50.8"],
]
table(s, data, 0.75, 1.68, 8.2, [2.1, 1.15, 1.15, 1.0, 1.0, 1.25, 1.05, 0.9],
      size=11.5, header_size=10.5, row_h=0.72, align_center_from=1, bold_first_col=True)
rect(s, 9.3, 1.68, 3.3, 1.62, fill=LIGHT)
tf = box(s, 9.52, 1.8, 2.9, 0.3)
para(tf, "权重为什么改", 11.5, GRAY, bold=True, first=True)
tf = box(s, 9.52, 2.12, 2.9, 1.15)
para(tf, "时序能力 15 → 22（增长型负载）", 11, RGBColor(0x3A, 0x42, 0x50), first=True, line=1.2, space_after=2)
para(tf, "迁移生态 10 → 12（不换库）", 11, RGBColor(0x3A, 0x42, 0x50), line=1.2, space_after=2)
para(tf, "运维成本 10 → 8（同一套技能）", 11, RGBColor(0x3A, 0x42, 0x50), line=1.2)
rect(s, 9.3, 3.45, 3.3, 2.55, fill=RGBColor(0xEC, 0xF4, 0xF4), line=TEAL)
tf = box(s, 9.52, 3.6, 2.9, 2.25)
para(tf, "一句话", 11.5, TEAL, bold=True, first=True)
para(tf, "分数差不是性能差；真正的取舍只有版本耦合与许可证两条。", 11.5, RGBColor(0x3A, 0x42, 0x50), space_before=3, line=1.25)
para(tf, "换句话说：把「以后可能要换库」的成本折算进去，裸 PG 并不便宜。", 11, GRAY, space_before=8, line=1.25)
tf = box(s, 0.8, 6.35, 11.7, 0.5)
para(tf, "旧口径 89 / 84、新口径 83.2 / 93.6——同一组能力，权重一变，排序就变。", 12.5, GRAY, first=True)

# ---------------------------------------------------------------- 11 capabilities
s = content_slide("它多出来的四组能力，正好对上我们的四件事", "机制层面：不是「更快的数据库」，而是「同一个库里补上时序能力」")
blocks = [
    ("自动分块 + 裁剪", "按时间自动切 chunk，查询只扫相关块；每块索引小、常驻内存", "对上：按时间窗取历史"),
    ("列存压缩", "冷数据整块转列存，按类型选编码；官方口径最高约 90%", "对上：存储与备份费用"),
    ("增量物化视图", "连续聚合只重算被影响的时间桶，迟到数据自动重算", "对上：误差报表与看板"),
    ("保留与调度", "DROP CHUNK 瞬时清理；后台作业有运行历史与失败记录", "对上：按年限保留、失败可查"),
]
for i, (name, mech, match) in enumerate(blocks):
    y = 1.72 + i * 1.28
    rect(s, 0.78, y, 6.5, 1.12, fill=WHITE, line=LINE)
    rect(s, 0.78, y, 0.05, 1.12, fill=TEAL)
    tf = box(s, 1.0, y + 0.12, 6.1, 0.35)
    para(tf, name, 14, NAVY, bold=True, first=True)
    tf = box(s, 1.0, y + 0.5, 6.1, 0.3)
    para(tf, mech, 10.5, GRAY, first=True)
    tf = box(s, 1.0, y + 0.82, 6.1, 0.28)
    para(tf, match, 10.5, ORANGE, bold=True, first=True)

chart_data = CategoryChartData()
chart_data.categories = ["NWP 表", "预测表", "功率表"]
chart_data.add_series("行存（含索引）MB", (56.4, 30.0, 20.4))
chart_data.add_series("列存+压缩 MB", (4.65, 1.50, 1.42))
gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(7.6), Inches(1.62),
                        Inches(5.05), Inches(3.5), chart_data)
ch = gf.chart
ch.has_legend = True
ch.legend.position = XL_LEGEND_POSITION.TOP
ch.legend.include_in_layout = False
ch.legend.font.size = Pt(10)
ch.legend.font.name = FONT
ch.font.size = Pt(10)
ch.font.name = FONT
ser1, ser2 = ch.series[0], ch.series[1]
ser1.format.fill.solid()
ser1.format.fill.fore_color.rgb = RGBColor(0x9A, 0xAB, 0xBD)
ser2.format.fill.solid()
ser2.format.fill.fore_color.rgb = TEAL
ch.plots[0].gap_width = 60
ch.plots[0].overlap = -10
ch.value_axis.has_major_gridlines = True
ch.value_axis.major_gridlines.format.line.color.rgb = LINE
ch.value_axis.tick_labels.font.size = Pt(9)
ch.value_axis.has_title = False
ch.category_axis.tick_labels.font.size = Pt(10)
ser2.data_labels.show_value = True
ser2.data_labels.font.size = Pt(9)
ser2.data_labels.font.color.rgb = TEAL
ser2.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END

tf = box(s, 7.62, 5.2, 5.05, 1.1)
para(tf, "本项目实测（列存压缩代理测量）", 11, GRAY, bold=True, first=True)
para(tf, "NWP −91.8%　预测 −95.0%　功率 −93.0%", 12.5, TEAL, bold=True, space_before=2)
para(tf, "预测表的索引（17MB）比数据本身（13MB）还大，列存把这块开销直接消掉。", 10, GRAY, space_before=2, line=1.2)

# ---------------------------------------------------------------- 12 costs
s = content_slide("两条代价与四条排除理由，一起讲才诚实", "选型报告最容易被跳过的一页")
rect(s, 0.78, 1.66, 5.75, 1.72, fill=RGBColor(0xFB, 0xF3, 0xEC), line=ORANGE)
tf = box(s, 1.0, 1.82, 5.3, 0.35)
para(tf, "代价一：版本耦合", 14, ORANGE, bold=True, first=True)
tf = box(s, 1.0, 2.2, 5.3, 1.1)
para(tf, "TimescaleDB 2.32.0 官方兼容上限：PG 18.6 / 17.11 / 16.15。", 11.5, RGBColor(0x3A, 0x42, 0x50), first=True, line=1.25, space_after=3)
para(tf, "本机是 18.7 —— 装不上，必须钉版本。", 11.5, RGBColor(0x3A, 0x42, 0x50), line=1.25, space_after=3)
para(tf, "升级顺序：先升扩展，再升 PG。", 11.5, NAVY, bold=True, line=1.25)

rect(s, 0.78, 3.58, 5.75, 1.72, fill=RGBColor(0xF6, 0xF2, 0xFA), line=RGBColor(0x8A, 0x6D, 0x9E))
tf = box(s, 1.0, 3.74, 5.3, 0.35)
para(tf, "代价二：许可证", 14, RGBColor(0x6B, 0x53, 0x84), bold=True, first=True)
tf = box(s, 1.0, 4.12, 5.3, 1.1)
para(tf, "压缩、连续聚合、保留策略都在 TSL 版里。", 11.5, RGBColor(0x3A, 0x42, 0x50), first=True, line=1.25, space_after=3)
para(tf, "自建自用免费、可改源码；但不能把数据库能力当服务对外售卖。", 11.5, RGBColor(0x3A, 0x42, 0x50), line=1.25, space_after=3)
para(tf, "做客户交付平台前，先确认这条边界。", 11.5, NAVY, bold=True, line=1.25)

tf = box(s, 6.95, 1.62, 5.6, 0.35)
para(tf, "为什么不选它们当主库", 14, NAVY, bold=True, first=True)
excl = [
    ("ClickHouse", "ReplacingMergeTree 只在后台合并去重，合并前会读到重复；无标准外键。适合当分析副库。"),
    ("InfluxDB 3", "底层不可变 Parquet，幂等覆盖订正不是它的设计目标；无完整 ACID。"),
    ("MySQL", "分区表不支持外键、唯一索引必须含分区键；没有「时序能力平滑演进」的路线。"),
    ("DuckDB", "单进程写入、乐观并发，不能当在线服务；但它是离线分析的好工具。"),
]
y = 2.05
for name, why in excl:
    tf = box(s, 6.95, y, 5.55, 0.34)
    para(tf, name, 12, RGBColor(0x3A, 0x42, 0x50), bold=True, first=True)
    tf = box(s, 6.95, y + 0.32, 5.55, 0.62)
    para(tf, why, 10.5, GRAY, first=True, line=1.2)
    y += 1.12
tf = box(s, 6.95, 6.55, 5.55, 0.4)
para(tf, "它们不是「不好」，是「不在我们这格」。", 11.5, TEAL, bold=True, first=True)
rect(s, 0.78, 5.5, 5.75, 0.95, fill=RGBColor(0xEC, 0xF4, 0xF4), line=TEAL)
tf = box(s, 1.0, 5.62, 5.3, 0.75)
para(tf, "两条都可控：版本钉死、升级顺序写进清单；", 11.5, RGBColor(0x1E, 0x4E, 0x4E), bold=True, first=True, line=1.2)
para(tf, "许可是内部自用，不碰「对外售卖」边界。", 11.5, RGBColor(0x1E, 0x4E, 0x4E), bold=True, line=1.2)

# ---------------------------------------------------------------- 13 section 4
section_slide("04", "如何应用这个数据库",
              ["四张大表转为超表，三张小表保持普通表",
               "策略先行：列存、保留、连续聚合、监控",
               "迁移一次做完，回滚只有一条退路"])

# ---------------------------------------------------------------- 14 schema
s = content_slide("先把表分好类：谁转超表，谁不动", "转换受四条硬限制约束，提前对齐就不会返工")
data = [
    ["表", "处理", "时间分区键", "说明"],
    ["power_observations", "转 hypertable", "observed_at", "主键 (farm_id, observed_at) 已含时间列"],
    ["nwp_forecasts", "转 hypertable", "valid_at", "主键 (farm_id, valid_at) 满足要求"],
    ["nwp_revisions", "转 hypertable", "valid_at", "主键含 issued_at；source 可做 chunk skipping"],
    ["predictions", "转 hypertable", "valid_at", "主键 (run_id, origin, valid_at) 含时间列"],
    ["wind_farms / model_runs / dataset_splits", "保持普通表", "—", "超表之间不支持外键，必须留一侧"],
]
table(s, data, 0.75, 1.66, 7.55, [2.35, 1.5, 1.15, 3.3], size=10.5, header_size=10.5, row_h=0.55, bold_first_col=True)
rect(s, 8.6, 1.66, 4.0, 3.2, fill=LIGHT)
tf = box(s, 8.82, 1.78, 3.6, 0.3)
para(tf, "四条硬限制", 11.5, NAVY, bold=True, first=True)
tf = box(s, 8.82, 2.12, 3.6, 1.8)
for t in ["分区列不能为空",
          "唯一索引必须包含分区列",
          "不支持把行更新到另一个 chunk",
          "超表之间不能建外键"]:
    para(tf, "· " + t, 10.5, RGBColor(0x3A, 0x42, 0x50), first=(t.startswith("分区列")), line=1.2, space_after=3)
tf = box(s, 8.82, 3.62, 3.6, 0.9)
para(tf, "我们已经满足：四张表的主键都包含时间列，外键全部指向普通表。", 10.5, TEAL, bold=True, first=True, line=1.2)
tf = box(s, 8.6, 4.95, 4.0, 0.55)
para(tf, "只按时间分区，不加场站哈希维度：10–100 个站用不着。", 10.5, TEAL, bold=True, first=True, line=1.2)
rect(s, 0.78, 5.42, 11.82, 1.2, fill=RGBColor(0x1E, 0x24, 0x2E))
tf = box(s, 1.0, 5.53, 11.4, 1.05)
para(tf, "SELECT create_hypertable('predictions', by_range('valid_at'),", 10.5, RGBColor(0xE6, 0xE9, 0xEF), first=True, line=1.15)
para(tf, "       chunk_time_interval => INTERVAL '7 days', migrate_data => true);", 10.5, RGBColor(0xE6, 0xE9, 0xEF), line=1.15)
para(tf, "ALTER TABLE nwp_revisions SET (timescaledb.compress = true);   -- 90 天后转列存", 10.5, RGBColor(0x8F, 0xD3, 0xC8), line=1.15)

# ---------------------------------------------------------------- 15 policies
s = content_slide("四套策略，把「运维靠脚本」变成「库里自管」", "策略跑在后台 worker 上，有运行历史，失败能查")
data = [
    ["策略", "怎么做", "为什么这么设"],
    ["列存压缩", "NWP / 预测 180 天，修订表 90 天", "阈值必须大于最大修订延迟，否则迟到数据要改写压缩块"],
    ["保留清理", "NWP 2 年、预测 3 年、功率 5 年", "DROP CHUNK 瞬时完成，不产生死元组"],
    ["连续聚合", "误差日报（分站×提前量）、日可用率", "看板不扫千万行原始表；迟到数据自动进失效区间重算"],
    ["行存调优", "fillfactor 90、激进 autovacuum、received_at 建 BRIN", "预测表重跑频繁；入库时间单调递增，BRIN 只要几十 KB"],
]
table(s, data, 0.75, 1.68, 8.3, [1.5, 3.1, 3.9], size=10.5, header_size=10.5, row_h=0.78, bold_first_col=True)
rect(s, 9.35, 1.68, 3.25, 3.5, fill=WHITE, line=LINE)
tf = box(s, 9.55, 1.8, 2.85, 0.3)
para(tf, "必须监控的四项", 11.5, NAVY, bold=True, first=True)
mon = [("作业失败", "任一非 Success 即告警"), ("聚合滞后", "> 2 个刷新周期"), ("chunk 数", "单表 > 500 查间隔"), ("压缩率", "< 70% 查 segmentby")]
yy = 2.18
for k, v in mon:
    tf = box(s, 9.55, yy, 2.85, 0.28)
    para(tf, k, 10.5, ORANGE, bold=True, first=True)
    tf = box(s, 9.55, yy + 0.26, 2.85, 0.28)
    para(tf, v, 10, GRAY, first=True)
    yy += 0.72
tf = box(s, 0.8, 6.25, 11.7, 0.5)
para(tf, "这些都不需要额外组件：全是扩展自带的能力，跟着 PG 一起备份、一起监控。", 12.5, GRAY, first=True)

# ---------------------------------------------------------------- 16 migration
s = content_slide("迁移一次做完，顺序错了要重来", "逻辑复制不可用（官方不推荐），所以必须接受一次短暂停机")
steps = ["冷备\npg_dump -Fc", "起新实例\nPG 18.6 + TS 2.32", "建结构\nAlembic + 扩展", "灌数据\n先普通表后大表", "校验\n行数/求和/时间范围", "切流量\n先只读再写", "观察一周\n作业/压缩率/P95"]
w = 1.62
for i, st in enumerate(steps):
    x = 0.78 + i * 1.7
    rect(s, x, 1.75, w, 0.95, fill=LIGHT if i % 2 == 0 else WHITE, line=LINE)
    tf = box(s, x + 0.08, 1.82, w - 0.16, 0.35)
    para(tf, f"{i + 1}", 13, TEAL, bold=True, first=True, align=PP_ALIGN.CENTER)
    tf = box(s, x + 0.06, 2.16, w - 0.12, 0.5)
    for j, line in enumerate(st.split("\n")):
        para(tf, line, 9, NAVY if j == 0 else GRAY, bold=(j == 0), first=(j == 0),
             align=PP_ALIGN.CENTER, line=1.0, space_after=0)
rect(s, 0.78, 3.05, 11.82, 0.55, fill=RGBColor(0xFB, 0xF3, 0xEC), line=ORANGE)
tf = box(s, 1.0, 3.16, 11.4, 0.35)
para(tf, "第 4 步是效率关键：先灌数据、再做 create_hypertable(migrate_data => true)，比「先建超表再逐行灌」快一个量级。", 11, RGBColor(0x7A, 0x4A, 0x2A), bold=True, first=True)
rect(s, 0.78, 3.85, 5.75, 1.55, fill=WHITE, line=RED)
tf = box(s, 1.0, 3.98, 5.3, 0.32)
para(tf, "回滚只有一条退路", 12.5, RED, bold=True, first=True)
tf = box(s, 1.0, 4.32, 5.3, 1.0)
para(tf, "TimescaleDB 没有官方「撤销 hypertable」路径，只能靠迁移前的 pg_dump 恢复，或切回旧实例。", 10.5, RGBColor(0x3A, 0x42, 0x50), first=True, line=1.25)
para(tf, "所以第 1 步不是可选项。", 10.5, NAVY, bold=True, line=1.25)
rect(s, 6.8, 3.85, 5.8, 1.55, fill=WHITE, line=LINE)
tf = box(s, 7.02, 3.98, 5.35, 0.32)
para(tf, "停机窗口", 12.5, NAVY, bold=True, first=True)
tf = box(s, 7.02, 4.32, 5.35, 1.0)
para(tf, "数据量小，10 分钟级；「边同步边切换」在这里行不通，因为官方不推荐用逻辑复制同步超表。", 10.5, RGBColor(0x3A, 0x42, 0x50), first=True, line=1.25)
tf = box(s, 0.8, 5.75, 11.7, 0.9)
para(tf, "迁移草案已经写好、但没有执行：docs/timescaledb-migration-draft.sql（不注册为 Alembic 版本，alembic upgrade head 不会碰它）。",
     12, GRAY, first=True, line=1.25)

# ---------------------------------------------------------------- 17 section 5
section_slide("05", "部署在什么类型的设备上",
              ["生产起步用「主库 + 流复制备库」，不是单机、也不是一上来就 K8s",
               "16 GB 内存 / 4 vCPU / NVMe SSD 足够起步，扩容看三个信号"])

# ---------------------------------------------------------------- 18 topology
s = content_slide("三种形态，按阶段选", "十站、每日一次预报的负载，单机加备份就够；备库是为了「挂了还能出预报」")
forms = [
    ("A. 单机（开发 / 预发）", ["官方生产级镜像", "数据卷持久化、端口不对外", "只做演练与验收，不承接生产"], GRAY, "不用于生产"),
    ("B. 主备 + 自动切换（推荐起步）", ["1 主 1 备流复制 + Patroni", "WAL 归档对象存储，支持 PITR", "RPO ≤ 5 分钟，RTO ≤ 1 分钟"], TEAL, "生产推荐"),
    ("C. Kubernetes（已有集群时）", ["CloudNativePG / StackGres", "备份、扩容、升级编排更复杂", "没 K8s 经验就别选这条"], BLUE, "有集群再考虑"),
]
for i, (name, items, color, tag) in enumerate(forms):
    x = 0.78 + i * 4.02
    rect(s, x, 1.72, 3.82, 2.78, fill=WHITE, line=LINE)
    rect(s, x, 1.72, 3.82, 0.06, fill=color)
    tf = box(s, x + 0.22, 1.88, 3.4, 0.6)
    para(tf, name, 13, NAVY, bold=True, first=True, line=1.1)
    tf = box(s, x + 0.22, 2.55, 3.4, 1.9)
    for j, it in enumerate(items):
        para(tf, "· " + it, 10.5, RGBColor(0x3A, 0x42, 0x50), first=(j == 0), line=1.25, space_after=4)
    rect(s, x + 0.22, 3.92, 1.45, 0.38, fill=color)
    tf = box(s, x + 0.24, 3.98, 1.4, 0.28)
    para(tf, tag, 9.5, WHITE, bold=True, first=True, align=PP_ALIGN.CENTER)
tf = box(s, 0.8, 4.95, 11.7, 1.1)
para(tf, "为什么不是一上来就上多节点 / 分布式：", 12, NAVY, bold=True, first=True)
para(tf, "官方已在 2.14 下线分布式超表；我们的写入量（每天几百到几万行）用单机主备就能覆盖，多节点只会把运维复杂度先抬上去。", 11.5, GRAY, space_before=2, line=1.25)

# ---------------------------------------------------------------- 19 sizing
s = content_slide("设备规格与扩容信号", "以 16 GB / 4 vCPU / NVMe SSD 为起点，三年数据放得下")
data = [
    ["项", "建议", "理由"],
    ["CPU / 内存", "4 vCPU / 16 GB", "列存扫描可并行；shared_buffers 4 GB"],
    ["磁盘", "NVMe SSD，200 GB 起", "三年数据 + 压缩后冗余充足；备份另存对象存储"],
    ["shared_buffers", "4 GB（25%）", "常规起点"],
    ["effective_cache_size", "12 GB（~70%）", "给规划器「OS 也有缓存」的信号"],
    ["max_worker_processes", "16 起", "后台作业要 worker"],
    ["wal_level", "replica + WAL 归档", "流复制与 PITR"],
]
table(s, data, 0.75, 1.68, 7.7, [2.0, 2.45, 3.25], size=11, header_size=11, row_h=0.58, bold_first_col=True)
rect(s, 8.75, 1.68, 3.85, 3.95, fill=LIGHT)
tf = box(s, 8.97, 1.8, 3.45, 0.3)
para(tf, "什么时候该加机器", 11.5, NAVY, bold=True, first=True)
signals = [("活跃 chunk 索引", "超过内存 25% → 调小 chunk 间隔或加内存"),
           ("查询 P95", "连续两周上升 → 先看执行计划，再考虑扩容"),
           ("磁盘使用", "> 70% → 开更早的列存/保留策略，而不是先买盘"),
           ("复制延迟", "> 30 秒 → 查网络与 WAL 生成速率")]
yy = 2.18
for k, v in signals:
    tf = box(s, 8.97, yy, 3.45, 0.28)
    para(tf, k, 10.5, TEAL, bold=True, first=True)
    tf = box(s, 8.97, yy + 0.26, 3.45, 0.42)
    para(tf, v, 9.5, GRAY, first=True, line=1.15)
    yy += 0.82
tf = box(s, 0.8, 6.02, 11.7, 0.8)
para(tf, "容量口径：100 站 15 分钟功率观测约 350 万行/年；50 站 NWP 每天 4 批约 526 万行/年。开列存压缩后，三年单机 200 GB 足够。",
     11.5, GRAY, first=True, line=1.25)

# ---------------------------------------------------------------- 20 next
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, fill=NAVY)
tf = box(s, 0.95, 0.75, 11.4, 0.7)
para(tf, "下一步：备而不发，等真实数据触发", 30, WHITE, bold=True, first=True)
tf = box(s, 0.95, 1.55, 11.4, 0.4)
para(tf, "现在不做任何操作；本机实验库与优化链保持原样", 14, RGBColor(0xC9, 0xD3, 0xDE), first=True)
rect(s, 0.95, 2.1, 11.45, 0.02, fill=RGBColor(0x35, 0x44, 0x58))
tf = box(s, 0.95, 2.28, 5.4, 0.35)
para(tf, "三条启动条件（满足任一条）", 14, TEAL, bold=True, first=True)
conds = ["真实数据接入后日均新增 > 10 万行，或任一表 > 5,000 万行",
         "需要看板类聚合查询（分站×提前量×按周误差、可用率）",
         "需要按年限自动清理历史数据"]
yy = 2.72
for i, c in enumerate(conds, 1):
    tf = box(s, 0.95, yy, 0.4, 0.35)
    para(tf, f"{i}", 13, TEAL, bold=True, first=True)
    tf = box(s, 1.42, yy, 5.0, 0.6)
    para(tf, c, 11.5, RGBColor(0xD5, 0xDD, 0xE6), first=True, line=1.2)
    yy += 0.62
tf = box(s, 6.85, 2.28, 5.5, 0.35)
para(tf, "触发后按四步走", 14, TEAL, bold=True, first=True)
plans = [("P0", "真机验证：迁移耗时、实际压缩率、5 条查询 P50/P95"),
         ("P1", "草案转 Alembic 幂等迁移"),
         ("P2", "开列存/保留策略，接两张连续聚合"),
         ("P3", "用 pg_dump 恢复一次，确认 RTO 真实")]
yy = 2.72
for k, v in plans:
    tf = box(s, 6.85, yy, 0.55, 0.3)
    para(tf, k, 11.5, ORANGE, bold=True, first=True)
    tf = box(s, 7.45, yy, 4.9, 0.55)
    para(tf, v, 11, RGBColor(0xD5, 0xDD, 0xE6), first=True, line=1.2)
    yy += 0.62
rect(s, 0.95, 5.62, 11.45, 0.9, fill=RGBColor(0x24, 0x35, 0x4C))
tf = box(s, 1.2, 5.75, 11.0, 0.7)
para(tf, "一句话总结：不是换数据库，是给 PostgreSQL 装上时序能力；代价是跟一个扩展版本、守住一条许可证边界。",
     13.5, WHITE, bold=True, first=True, line=1.25)
tf = box(s, 0.95, 6.78, 11.4, 0.4)
para(tf, "2026-11-16　·　详细依据：docs/database-selection.md", 10.5, RGBColor(0x7F, 0x8C, 0x9C), first=True)
PAGE["n"] += 1

NOTES = {
    1: "开场：这是一份能直接做决定的汇报，五章对应五个问题，结论在第 2 页。",
    2: "三个数字：主库形态、约 90% 存储收益、93.6 对 83.2。最后一行强调现在不做任何动作。",
    3: "第一章只解决一个认知问题：数据库类型不等于能不能当业务库。",
    4: "六类各有各的活。价格锚点：TimescaleDB 是 PG 扩展，不是第七类。",
    5: "四种物理布局决定擅长负载；最后一列 LSM 是 InfluxDB 这类，写快但覆盖订正弱。",
    6: "第二章：先讲需求，再讲选型，避免「因为产品有名所以选它」。",
    7: "七条需求里，只有关系型能一次满足；右侧数字说明现在很小，不是选型理由。",
    8: "这一页是全场最容易被忽略的：NWP 是观测的 3 倍行数，所以「才百万行」这个判断是错的。",
    9: "第三章：为什么选它。先给分数，再给能力，最后给代价。",
    10: "强调权重变化：不是 TimescaleDB 变强了，是口径换成了生产口径。",
    11: "四组能力对上四件事；右边压缩比是本地实测的代理测量，说清口径。",
    12: "两条代价主动讲：版本耦合、许可证。再讲四条排除理由，避免被问「为什么不用 ClickHouse」。",
    13: "第四章：怎么用。分三步：表怎么改、策略怎么配、迁移怎么做。",
    14: "四张大表转超表，三张小表不动；四条限制我们已经全部满足。",
    15: "策略是这套方案的价值所在：把脚本运维变成库内自管，并且可监控。",
    16: "迁移七步，第 4 步是效率关键；回滚只有 pg_dump 一条路，所以第 1 步不能省。",
    17: "第五章：装在哪。先说形态，再说规格。",
    18: "推荐 B：主备，不是单机也不是 K8s；备库是为了「挂了还能出预报」。",
    19: "16GB/4vCPU/NVMe 200GB 起步；扩容看三个信号，别先买盘。",
    20: "收尾：现在不做，三条启动条件触发后再走 P0–P3。最后一句是全场要记住的话。",
}
for i, sl in enumerate(prs.slides, 1):
    txt = NOTES.get(i)
    if txt and not sl.notes_slide.notes_text_frame.text.strip():
        sl.notes_slide.notes_text_frame.text = txt

out = "artifacts/deck_db_20261116/数据库选型与部署方案_2026-11-16.pptx"
prs.save(out)
print("saved", out, "slides:", len(prs.slides.__iter__.__self__._sldIdLst))
