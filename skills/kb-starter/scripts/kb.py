#!/usr/bin/env python3
"""kb-starter：把零散资料整理成分层知识库的辅助脚本（只用 Python 标准库）。

子命令
  scan    扫描资料文件夹，按规则分类，生成方案表 plan.csv、plan.md、snippets.jsonl
  decide  把 AI 对“待AI”条目的判断（decisions.json）写回方案表
  apply   按确认后的方案表把文件复制进知识库（原件不动），更新清单、资料索引和变更日志，最后自动对账
  verify  对账：来源里每个文件都有去向、副本与原件哈希一致、原件没被改过
  index   从知识卡片头部生成知识索引（AI 每次开工先读它，相当于记忆入口）
  gaps    缺口的机械部分：未蒸馏的原文、缺口清单、被提到却没写的知识、没写来源的卡片、待测假设
  stats   增长统计：按周统计带标签的日志和新增卡片，判断知识库是在“生长”还是只在“变大”
  lint    体检的机械部分：断链、孤立笔记、未登记文件、近期改动、候选概念、整洁度、增长
  pack    生成给网页版 AI 粘贴用的上下文包

规则只处理“看文件名、扩展名和元数据就能判断”的文件；判断不了的标成“待AI”，
由调用本脚本的 AI 读 snippets.jsonl（必要时读原文首页）后写 decisions.json。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = SKILL_DIR / "templates"

# ---------------------------------------------------------------- 分层与类别

SKELETON = [
    "00_原始资料/01_论文原文",
    "00_原始资料/02_实验记录",
    "00_原始资料/03_会议记录",
    "00_原始资料/04_网页剪藏",
    "00_原始资料/05_课程笔记",
    "00_原始资料/06_个人笔记",
    "00_原始资料/09_待定",
    "01_知识提炼/概念节点",
    "01_知识提炼/文献卡片",
    "01_知识提炼/决策",
    "01_知识提炼/问题",
    "01_知识提炼/教训",
    "01_知识提炼/人物",
    "01_知识提炼/研究问题",
    "02_方法论库/SOP与方案",
    "02_方法论库/写作模板",
    "02_方法论库/AI提示词",
    "03_研究输出/论文草稿",
    "03_研究输出/汇报PPT",
    "03_研究输出/报告",
    "04_复盘日记/体检",
    "99_工作区",
]

# 类别 → 目标目录；{项目} 为空时连同前面的 / 一起去掉
CATEGORIES = {
    "论文": "00_原始资料/01_论文原文/{项目}",
    "题录": "00_原始资料/01_论文原文/{项目}",
    "源稿": "00_原始资料/01_论文原文/{项目}/源稿",
    "实验记录": "00_原始资料/02_实验记录/{项目}",
    "照片": "00_原始资料/02_实验记录/{项目}/照片",
    "会议记录": "00_原始资料/03_会议记录/{项目}",
    "网页剪藏": "00_原始资料/04_网页剪藏/{项目}",
    "课程资料": "00_原始资料/05_课程笔记/{项目}",
    "个人笔记": "00_原始资料/06_个人笔记/{项目}",
    "待定": "00_原始资料/09_待定",
    "论文草稿": "03_研究输出/论文草稿/{项目}",
    "汇报PPT": "03_研究输出/汇报PPT/{项目}",
    "报告": "03_研究输出/报告/{项目}",
    "代码": "99_工作区/代码/{项目}",
}

STATUS_COPY, STATUS_AI, STATUS_SKIP, STATUS_DONE, STATUS_REGISTER = "复制", "待AI", "跳过", "已入库", "登记"
STATUS_BLOCKED = "无法读取"  # iCloud 未下载、没有读权限、失效的链接：不入库，但必须让用户看到

# 子目录：放在目标目录下的一层，用于“历史版本”或同名文件保留来源文件夹；入库名：给无意义文件名起的新名字（只改副本）
PLAN_FIELDS = ["id", "状态", "类别", "项目", "子目录", "入库名", "目标目录", "文件名", "原路径", "依据", "置信度",
               "大小KB", "日期", "修改时间", "sha256"]
MANIFEST_FIELDS = ["sha256", "知识库路径", "原路径", "类别", "项目", "入库时间"]

# ---------------------------------------------------------------- 关键词规则（只匹配文件名、所在文件夹名和文档标题）

KW = {
    "会议记录": ["会议", "组会", "纪要", "讨论", "访谈", "录音", "转写", "例会", "谈话", "沟通", "meeting", "minutes", "transcript", "interview"],
    "课程资料": ["课程", "课件", "讲义", "作业", "习题", "考试", "期末", "期中", "复习", "lecture", "course", "homework", "syllabus", "assignment", "tutorial"],
    "实验记录": ["实验", "原始数据", "数据", "配比", "测试", "表征", "样品", "检测", "试制", "记录", "sop", "protocol", "xrd", "ftir", "nmr", "sem", "tga", "dsc", "xps", "raman", "gpc", "data", "sample", "experiment"],
    "汇报PPT": ["汇报", "答辩", "开题", "展示", "presentation", "slides", "defense", "seminar"],
    "报告": ["报告", "申请书", "基金", "项目书", "计划书", "总结", "proposal", "report", "grant"],
    "论文草稿": ["论文", "初稿", "草稿", "终稿", "修改稿", "大修", "小修", "投稿", "定稿", "毕业论文", "manuscript", "draft", "thesis", "revision", "rebuttal"],
    "网页剪藏": ["剪藏", "clip", "clipping", "wikipedia", "知乎", "公众号", "网页"],
    "截图": ["截屏", "截图", "屏幕快照", "screenshot", "screen shot", "snipaste"],
    "强产出": ["初稿", "草稿", "修改稿", "终稿", "定稿", "draft", "manuscript", "作业", "课件", "讲义"],
    # PDF 只有出现这些词才算自己的稿子；单个“论文”不够，下载的文献也常放在“论文”文件夹里
    "PDF草稿": ["初稿", "草稿", "修改稿", "终稿", "定稿", "大修", "小修", "投稿", "毕业论文", "draft", "manuscript", "revision", "rebuttal"],
    # 命中这些词的文件不自动入库，交给 AI 列出来问用户
    "隐私": ["身份证", "护照", "户口", "银行", "流水", "工资", "合同", "密码", "体检报告", "病历", "社保", "学生证",
             "passport", "password", "bank statement", "salary", "contract", "id card"],
}

# 放在这些文件夹里、又没有别的线索的 PDF，按文献处理
PAPER_DIRS = {"文献", "论文", "papers", "paper", "references", "literature", "参考文献", "引用文献", "pdf", "pdfs"}

# 文件夹名像“类别”而不像“项目”时，不拿来当项目名
GENERIC_DIRS = {
    "论文", "文献", "papers", "paper", "pdf", "pdfs", "literature", "references", "实验", "数据", "data", "课程", "课件",
    "会议", "照片", "图片", "photos", "images", "img", "pictures", "下载", "downloads", "download", "新建文件夹",
    "未命名", "misc", "其他", "杂项", "资料", "docs", "documents", "文档", "桌面", "desktop", "notes", "笔记", "备份",
    "backup", "temp", "tmp", "inbox", "收件箱", "screenshots", "截图", "录音", "audio", "video", "视频", "code", "代码",
}

EXT = {
    "pdf": {".pdf"},
    "doc": {".doc", ".docx", ".odt", ".rtf", ".pages", ".wps"},
    "ppt": {".ppt", ".pptx", ".key", ".odp", ".dps"},
    "sheet": {".xls", ".xlsx", ".csv", ".tsv", ".ods", ".et"},
    "instrument": {".spc", ".jdx", ".dx", ".xrdml", ".opj", ".opju", ".mnova", ".jcamp", ".chi", ".mpr", ".dta", ".txtx"},
    "image": {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp", ".gif"},
    "audio": {".m4a", ".mp3", ".wav", ".aac", ".amr", ".flac", ".ogg", ".opus"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm"},
    "web": {".html", ".htm", ".mhtml", ".mht", ".webarchive", ".url", ".webloc"},
    "text": {".md", ".txt", ".markdown"},
    "biblio": {".bib", ".ris", ".enw", ".nbib"},
    "tex": {".tex"},
    "texjunk": {".aux", ".log", ".bbl", ".blg", ".out", ".toc", ".fdb_latexmk", ".fls"},
    "code": {".py", ".ipynb", ".r", ".m", ".jl", ".sh", ".js", ".ts", ".cpp", ".c", ".h", ".java", ".go", ".rs", ".mat"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".tgz", ".bz2", ".xz"},
}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r"}
JUNK_EXT = {".tmp", ".part", ".crdownload", ".lnk", ".download"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "env"}

DOI_RE = re.compile(rb"10\.\d{4,9}/[-._;()/:A-Za-z0-9]{3,}")
ARXIV_RE = re.compile(r"arxiv[:\s]*\d{4}\.\d{4,5}|(^|[^\d])\d{4}\.\d{4,5}(v\d+)?\.pdf$", re.I)
EXIF_DATE_RE = re.compile(rb"(20\d{2}|19\d{2}):(\d{2}):(\d{2}) \d{2}:\d{2}:\d{2}")
CAMERA_RE = re.compile(rb"Apple|Canon|NIKON|SONY|HUAWEI|Xiaomi|OPPO|vivo|samsung|FUJIFILM|Panasonic|OLYMPUS|Google|HONOR|Leica|DJI|OnePlus|realme|Redmi")


# ---------------------------------------------------------------- 工具函数

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text_guess(path: Path, limit: int = 4000) -> str:
    raw = path.read_bytes()[: limit * 3]
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)[:limit]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")[:limit]


def strip_xml(s: str) -> str:
    s = re.sub(r"</w:p>|</a:p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&amp;", "&", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def office_info(path: Path) -> tuple[str, str]:
    """返回 (文档标题, 前几百字正文)。只适用于 OOXML（docx/pptx/xlsx）。"""
    title, body = "", ""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "docProps/core.xml" in names:
                core = z.read("docProps/core.xml").decode("utf-8", "ignore")
                m = re.search(r"<dc:title>(.*?)</dc:title>", core, re.S)
                title = strip_xml(m.group(1)) if m else ""
            if "word/document.xml" in names:
                body = strip_xml(z.read("word/document.xml").decode("utf-8", "ignore"))
            else:
                slides = sorted(
                    (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.findall(r"\d+", n)[-1]),
                )
                if slides:
                    body = "\n".join(strip_xml(z.read(n).decode("utf-8", "ignore")) for n in slides[:3])
                elif "xl/sharedStrings.xml" in names:
                    body = strip_xml(z.read("xl/sharedStrings.xml").decode("utf-8", "ignore"))
                    if "xl/workbook.xml" in names:
                        wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
                        sheets = re.findall(r'<sheet [^>]*name="([^"]+)"', wb)
                        body = "工作表：" + "、".join(sheets) + "\n" + body
    except (zipfile.BadZipFile, KeyError, OSError):
        pass
    return title, body[:800]


def pdf_info(path: Path) -> tuple[bool, str, str]:
    """返回 (是否像已发表文献, 识别到的 DOI/arXiv, 首页文字)。"""
    first_page = ""
    if shutil.which("pdftotext"):
        try:
            first_page = subprocess.run(
                ["pdftotext", "-l", "1", "-q", str(path), "-"],
                capture_output=True, timeout=20,
            ).stdout.decode("utf-8", "ignore")
        except (subprocess.TimeoutExpired, OSError):
            first_page = ""
    with path.open("rb") as f:
        raw = f.read(3 << 20)
    ident = ""
    m = DOI_RE.search(first_page.encode("utf-8", "ignore")) or DOI_RE.search(raw)
    if m:
        ident = "DOI " + m.group(0).decode("ascii", "ignore").rstrip(").;,")
    elif ARXIV_RE.search(first_page) or ARXIV_RE.search(path.name):
        ident = "arXiv"
    if not first_page:
        # 没有 pdftotext 时，从未压缩的部分捞一点可读文字给 AI 参考
        chunks = re.findall(rb"\(([^()\\]{4,200})\)", raw[:400000])
        first_page = " ".join(c.decode("latin-1", "ignore") for c in chunks[:60])
    return bool(ident), ident, first_page[:800]


def image_info(path: Path) -> tuple[str, bool]:
    """返回 (EXIF 拍摄日期 YYYY-MM-DD, 是否有相机厂商信息)。"""
    with path.open("rb") as f:
        head = f.read(512 * 1024)
    m = EXIF_DATE_RE.search(head)
    date = f"{m.group(1).decode()}-{m.group(2).decode()}-{m.group(3).decode()}" if m else ""
    return date, bool(CAMERA_RE.search(head))


KW_PATTERNS = {
    "课程资料": [r"第[一二三四五六七八九十百\d]+[讲课]", r"(?<![a-z])lec(ture)?[ _-]?\d+"],
}
COPY_MARK_RE = re.compile(r" \(\d+\)\.[^.]+$|（\d+）|副本|[ _-]copy(\.| \d)|的副本")

# 文件名末尾的版本标记。只认末尾，避免“改性木粉”里的“改”、“第3讲”里的“第3”被误当版本
SEP = r"[\s_\-.]*"
VERSION_TAIL_RES = [
    re.compile(SEP + r"(?:v|ver|version|rev)\s*(\d+)(?:\.\d+)*$", re.I),
    re.compile(SEP + r"第\s*([\d一二三四五六七八九十]+)\s*版$"),
    re.compile(SEP + r"(?:版本|版)\s*(\d+)$"),
    re.compile(SEP + r"(?:final|最终版?|终稿|定稿|修改版|修改稿|修订版|修订稿|最新版?|new)\s*\d*$", re.I),
    re.compile(SEP + r"[\(（]\s*\d+\s*[\)）]$"),
    re.compile(SEP + r"(?:副本|copy)\s*\d*$", re.I),
]
PINNED = "〔子目录已指定〕"  # AI 或用户明确指定过子目录的行，decide 重新标版本/同名时不再改动
FINAL_RE = re.compile(r"final|最终|终稿|定稿|修改|修订|最新|new", re.I)
EXT_FAMILY = {".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx", ".jpeg": ".jpg"}

# 看不出内容的文件名：相机、扫描仪、截图、“新建文档”之类
_DATE_CHARS = r"[\d\-_ .:年月日时分秒上下午()（）]"
GENERIC_NAME_RE = re.compile(
    r"(?:img|dsc|dscn|dcim|pxl|mmexport|wechatimg|微信图片|image|scan|scanned|扫描件?|截屏|截图|屏幕截图|屏幕快照|"
    r"screenshot|screen ?shot|untitled|未命名|无标题|新建[^\d]*|document|文档|file|download|下载|temp|tmp|"
    r"photo|照片|video|视频|录音|新录音|audio|record)" + _DATE_CHARS + r"*"
    r"|" + _DATE_CHARS + r"+"          # 纯数字、纯日期
    r"|[0-9a-f\-]{8,}",                # 哈希、UUID
    re.I,
)


def kw_hits(text: str, key: str) -> list[str]:
    """中文关键词按子串匹配；英文关键词要求前后不是字母，避免 sem 命中 seminar、data 命中 metadata。"""
    t = text.lower()
    hits = []
    for pat in KW_PATTERNS.get(key, []):
        m = re.search(pat, t)
        if m:
            hits.append(m.group(0))
    for k in KW[key]:
        kl = k.lower()
        if kl.isascii():
            if re.search(r"(?<![a-z])" + re.escape(kl) + r"(?![a-z])", t):
                hits.append(k)
        elif kl in t:
            hits.append(k)
    return hits


def dest_dir(category: str, project: str, sub: str = "") -> str:
    pattern = CATEGORIES.get(category, CATEGORIES["待定"])
    project = (project or "").strip().strip("/")
    d = pattern.replace("{项目}", project) if project else pattern.replace("/{项目}", "").replace("{项目}", "")
    sub = (sub or "").strip().strip("/")
    return f"{d}/{sub}" if sub else d


def version_key(name: str) -> tuple[str, bool, int]:
    """返回 (去掉版本标记后的主干, 是否带版本标记, 版本号；final 类记 999)。"""
    stem = Path(name).stem
    base, num, marked = stem, -1, False
    changed = True
    while changed:
        changed = False
        for rx in VERSION_TAIL_RES:
            m = rx.search(base)
            if m and m.start() > 0:
                tail = m.group(0)
                if m.groups() and m.group(1) and m.group(1).isdigit():
                    num = max(num, int(m.group(1)))
                elif FINAL_RE.search(tail):
                    num = max(num, 999)
                base, marked, changed = base[: m.start()], True, True
    return base.strip().lower(), marked, num


def ext_family(path: Path) -> str:
    ext = path.suffix.lower()
    return EXT_FAMILY.get(ext, ext)


def is_generic_name(name: str) -> bool:
    stem = Path(name).stem.strip()
    return bool(stem) and (bool(GENERIC_NAME_RE.fullmatch(stem)) or len(re.sub(r"[\W_]", "", stem)) <= 2)


def stat_quiet(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def guess_project(rel: Path) -> str:
    for part in rel.parts[:-1]:
        name = part.strip()
        if name.lower() in GENERIC_DIRS or re.fullmatch(r"[\d._\- ]+", name) or re.match(r"\d{2}_", name):
            continue
        return name
    return ""


# ---------------------------------------------------------------- 知识库纲要
#
# 纲要由用户定：项目、目标、核心问题，以及把资料分到项目用的「关键词」「别名」。
# 每个项目一节，标题写成「### 项目：名称」或「### 课程：名称」，字段写成顶格的「- 字段：值」。

OUTLINE = "知识库纲要.md"
OUTLINE_HEAD_RE = re.compile(r"^###\s*(项目|课程)\s*[：:]\s*(.+?)\s*$")
OUTLINE_FIELD_RE = re.compile(r"^-\s*([^：:\s][^：:]{0,11})\s*[：:]\s*(.*)$")
OUTLINE_ITEM_RE = re.compile(r"^\s+(?:\d+[.、)）]|[-*])\s*(.+)$")
TERM_SPLIT_RE = re.compile(r"[,，、;；]+")


def read_outline(path: Path | None) -> dict:
    """解析纲要；没有纲要时返回空字典。模板占位符 {{…}} 和 HTML 注释里的示例都不算。"""
    if not path or not path.exists():
        return {}
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8", errors="ignore"), flags=re.S)
    fm = read_frontmatter(text)
    projects: list[dict] = []
    cur, field, section, purpose = None, "", "", []
    for line in body_of(text).splitlines():
        if line.startswith("## "):
            section, cur = line[3:].strip(), None
            continue
        m = OUTLINE_HEAD_RE.match(line)
        if m:
            cur, field = None, ""
            if "{{" not in m.group(2):
                cur = {"kind": m.group(1), "name": m.group(2), "fields": {}, "questions": []}
                projects.append(cur)
            continue
        if line.startswith("### "):
            cur = None
            continue
        if cur is None:
            if section.startswith("这个知识库用来做什么") and line.strip() and "{{" not in line:
                purpose.append(line.strip())
            continue
        f = OUTLINE_FIELD_RE.match(line)
        if f:
            field, value = f.group(1).strip(), f.group(2).strip()
            cur["fields"][field] = "" if "{{" in value else value
            continue
        q = OUTLINE_ITEM_RE.match(line)
        if q and field == "核心问题" and "{{" not in q.group(1):
            cur["questions"].append(q.group(1).strip())
    for p in projects:
        def terms(key: str) -> list[str]:
            return [t.strip() for t in TERM_SPLIT_RE.split(p["fields"].get(key, ""))
                    if t.strip() and t.strip() not in ("待确认", "无", "跳过")]
        p["aliases"], p["keywords"] = terms("别名"), terms("关键词")
        if not p["questions"] and p["fields"].get("核心问题"):  # 写在同一行的问题
            p["questions"] = [q.strip() for q in re.split(r"[；;]", p["fields"]["核心问题"]) if q.strip()]
    return {"path": str(path), "updated": fm.get("更新", ""), "purpose": " ".join(purpose), "projects": projects}


def term_hits(text: str, terms: list[str]) -> list[str]:
    """和 kw_hits 一样：中文按子串匹配，英文要求前后不是字母数字。"""
    t = text.lower()
    hits = []
    for term in terms:
        tl = term.lower()
        if tl.isascii():
            if len(tl) >= 2 and re.search(r"(?<![a-z0-9])" + re.escape(tl) + r"(?![a-z0-9])", t):
                hits.append(term)
        elif tl in t:
            hits.append(term)
    return hits


def outline_project(rel: Path, snippet: str, projects: list[dict]) -> tuple[str, str]:
    """按纲要给文件定项目，返回 (项目, 依据)。项目为空、依据不为空表示两个项目打平，留给 AI。"""
    for folder in rel.parts[:-1]:
        for p in projects:
            if folder.strip().lower() in {p["name"].lower(), *(a.lower() for a in p["aliases"])}:
                return p["name"], f"所在文件夹「{folder}」是纲要项目"
    name_ctx = " ".join(rel.parts)
    scored = []
    for p in projects:
        terms = [p["name"], *p["aliases"], *p["keywords"]]
        in_name = term_hits(name_ctx, terms)
        in_body = [h for h in term_hits(snippet, terms) if h not in in_name]
        score = 2 * len(in_name) + len(in_body)  # 文件名里的命中更可靠
        if score:
            scored.append((score, p["name"], in_name + in_body))
    if not scored:
        return "", ""
    scored.sort(key=lambda s: -s[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return "", f"关键词同时命中「{scored[0][1]}」「{scored[1][1]}」"
    return scored[0][1], f"纲要关键词「{'、'.join(scored[0][2][:3])}」"


def locate_in_kb(rel_in_kb: Path) -> tuple[str, str]:
    """文件已在知识库里时，按它所在的目录反推 (类别, 项目)。"""
    parent = rel_in_kb.parent.as_posix()
    best = ("待定", "", -1)
    for cat, pattern in CATEGORIES.items():
        base = pattern.split("/{项目}")[0]
        if parent == base or parent.startswith(base + "/"):
            rest = parent[len(base):].strip("/").split("/")
            project = rest[0] if rest and rest[0] not in ("", "照片", "源稿") else ""
            sub = pattern.split("{项目}")[-1].strip("/")  # 照片、源稿
            score = len(base) + (10 if sub and sub in rest else 0)
            if score > best[2]:
                best = (cat, project, score)
    return best[0], best[1]


def load_ignored(kb: Path) -> dict[str, str]:
    path = kb / ".kb" / "ignored.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row["sha256"]: row["原因"] for row in csv.DictReader(f)}


def load_manifest(kb: Path) -> dict[str, dict]:
    path = kb / ".kb" / "manifest.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row["sha256"]: row for row in csv.DictReader(f)}


def read_plan(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_plan(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # 带 BOM，Excel 打开中文不乱码
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in PLAN_FIELDS})


# ---------------------------------------------------------------- 分类

def classify(path: Path, rel: Path) -> tuple[str, str, str, str, str, str]:
    """返回 (状态, 类别, 依据, 置信度, 日期, 片段)。"""
    ext = path.suffix.lower()
    name_ctx = " ".join(rel.parts)  # 文件名 + 所在文件夹名
    snippet, date = "", ""

    def hit(key: str, text: str = name_ctx) -> list[str]:
        return kw_hits(text, key)

    def first_kw(order: list[str], text: str) -> tuple[str, list[str]]:
        for key in order:
            h = hit(key, text)
            if h:
                return key, h
        return "", []

    if ext in EXT["pdf"]:
        is_paper, ident, snippet = pdf_info(path)
        strong = hit("强产出")
        if is_paper and not strong:
            return STATUS_COPY, "论文", f"PDF 含 {ident}", "高", date, snippet
        note = f"（同时含 {ident}）" if is_paper else ""
        if "开题" in name_ctx and "报告" in name_ctx:
            return STATUS_COPY, "报告", f"文件名含「开题报告」{note}", "中", date, snippet
        h = hit("PDF草稿")
        if h:
            return STATUS_COPY, "论文草稿", f"文件名含「{h[0]}」{note}", "中", date, snippet
        key, h = first_kw(["课程资料", "会议记录", "汇报PPT", "报告", "实验记录"], name_ctx)
        if key:
            return STATUS_COPY, key, f"文件名含「{h[0]}」{note}", "中", date, snippet
        if any(part.lower() in PAPER_DIRS for part in rel.parts[:-1]):
            return STATUS_COPY, "论文", "放在文献文件夹里，但没识别到 DOI", "中", date, snippet
        return STATUS_AI, "", "PDF 未识别到 DOI/arXiv，文件名也无线索", "", date, snippet

    if ext in EXT["doc"]:
        title, snippet = office_info(path)
        text = name_ctx + " " + title
        if "开题" in text and "报告" in text:
            return STATUS_COPY, "报告", "文件名含「开题报告」", "高", date, snippet
        key, h = first_kw(["会议记录", "课程资料", "实验记录", "报告", "论文草稿", "汇报PPT"], text)
        if key:
            key = "报告" if key == "汇报PPT" else key  # “中期汇报.docx”这类文字稿归报告
            return STATUS_COPY, key, f"文件名/标题含「{h[0]}」", "中", date, snippet
        return STATUS_AI, "", "文档无文件名线索，需看正文判断是自己写的还是别人给的", "", date, snippet

    if ext in EXT["ppt"]:
        title, snippet = office_info(path)
        text = name_ctx + " " + title
        key, h = first_kw(["课程资料", "汇报PPT", "会议记录"], text)
        if key == "课程资料":
            return STATUS_COPY, key, f"文件名/标题含「{h[0]}」", "中", date, snippet
        if key:  # 组会、讨论等会议词出现在幻灯片上，也是自己的汇报
            conf = "中" if h[0] in ("slides", "presentation", "seminar") else "高"  # 课件也常叫 slides
            return STATUS_COPY, "汇报PPT", f"文件名/标题含「{h[0]}」", conf, date, snippet
        return STATUS_AI, "", "幻灯片无文件名线索，需看前几页判断是课件还是自己的汇报", "", date, snippet

    if ext in EXT["sheet"] or ext in EXT["instrument"]:
        if ext in {".xlsx", ".ods"}:
            _, snippet = office_info(path)
        elif ext in {".csv", ".tsv"}:
            snippet = read_text_guess(path, 600)
        key, h = first_kw(["课程资料", "报告"], name_ctx)
        if key:
            return STATUS_COPY, key, f"文件名含「{h[0]}」", "中", date, snippet
        if ext in EXT["instrument"]:
            return STATUS_COPY, "实验记录", f"仪器数据格式 {ext}", "高", date, snippet
        h = hit("实验记录")
        return STATUS_COPY, "实验记录", ("文件名含「%s」" % h[0]) if h else "表格/数据文件默认归实验数据", "高" if h else "中", date, snippet

    if ext in EXT["image"]:
        date, camera = image_info(path)
        if hit("截图"):
            return STATUS_AI, "", "截图，内容不确定", "", date, snippet
        h = hit("实验记录")
        if h:
            return STATUS_COPY, "照片", f"文件名含「{h[0]}」", "高", date, snippet
        if date and camera:
            return STATUS_COPY, "照片", f"相机/手机拍摄（EXIF {date}），默认归实验照片", "中", date, snippet
        return STATUS_AI, "", "图片无拍摄信息也无文件名线索", "", date, snippet

    if ext in EXT["audio"]:
        key, h = first_kw(["课程资料", "实验记录"], name_ctx)
        if key:
            return STATUS_COPY, key, f"录音，文件名含「{h[0]}」", "中", date, snippet
        h = hit("会议记录")
        return STATUS_COPY, "会议记录", ("录音，文件名含「%s」" % h[0]) if h else "录音默认归会议记录", "高" if h else "中", date, snippet

    if ext in EXT["video"]:
        key, h = first_kw(["课程资料", "会议记录", "实验记录", "汇报PPT"], name_ctx)
        if key:
            return STATUS_COPY, key, f"视频，文件名含「{h[0]}」", "中", date, snippet
        return STATUS_AI, "", "视频无文件名线索", "", date, snippet

    if ext in EXT["web"]:
        if ext in {".html", ".htm"}:
            m = re.search(r"<title>(.*?)</title>", read_text_guess(path, 20000), re.S | re.I)
            snippet = m.group(1).strip() if m else ""
        return STATUS_COPY, "网页剪藏", f"网页文件 {ext}", "高", date, snippet

    if ext in EXT["biblio"]:
        return STATUS_COPY, "题录", f"文献题录 {ext}", "高", date, read_text_guess(path, 400)

    if ext in EXT["tex"]:
        return STATUS_COPY, "论文草稿", "LaTeX 源文件", "中", date, read_text_guess(path, 600)

    if path.name.endswith(".synctex.gz") or ext in {".aux", ".bbl", ".blg", ".fls", ".fdb_latexmk"} or (
        ext in EXT["texjunk"] and path.with_suffix(".tex").exists()
    ):
        return STATUS_SKIP, "", "LaTeX 编译中间文件", "高", date, ""

    if ext in EXT["code"]:
        return STATUS_COPY, "代码", f"代码文件 {ext}", "中", date, read_text_guess(path, 400)

    if ext in EXT["archive"]:
        return STATUS_COPY, "待定", "压缩包，未解压，原样存放", "低", date, ""

    if ext in EXT["text"]:
        snippet = read_text_guess(path, 800)
        fm = snippet[:600]
        if re.search(r"^(source|url|来源|原文链接)\s*:\s*https?://", fm, re.M | re.I):
            return STATUS_COPY, "网页剪藏", "笔记头部带来源链接", "高", date, snippet
        key, h = first_kw(["会议记录", "课程资料", "实验记录", "网页剪藏"], name_ctx)
        if key:
            return STATUS_COPY, key, f"文件名含「{h[0]}」", "中", date, snippet
        return STATUS_COPY, "个人笔记", "文本笔记，无其他线索", "中", date, snippet

    return STATUS_AI, "", f"未知格式 {ext or '无扩展名'}", "", date, snippet


# ---------------------------------------------------------------- scan

ICLOUD_RE = re.compile(r"^\.(.+)\.icloud$")
FOLDER_COUNT_RE = re.compile(r"含 (\d+) 个文件")


def count_files(d: Path) -> tuple[int, int]:
    n = size = 0
    for root, _, names in os.walk(d):
        for name in names:
            n += 1
            st = stat_quiet(Path(root) / name)
            size += st.st_size if st else 0
    return n, size


def walk_source(src: Path, kb: Path | None) -> tuple[list[Path], list[dict]]:
    """列出要处理的文件，以及不展开的文件夹（隐藏、依赖、符号链接）。两者合起来覆盖来源里的每一个文件。"""
    files, folders = [], []
    for root, dirs, names in os.walk(src):
        root_p = Path(root)
        if kb and root_p == kb:
            # 扫描的是知识库本身：只看 A 层和 D 层；根目录入口文件、B/C 层笔记、工作区都不是“资料”
            dirs[:] = sorted(d for d in dirs if d.startswith(("00_", "03_")))
            names = []
        keep = []
        for d in sorted(dirs):
            p = root_p / d
            if kb and p == kb and src != kb:
                continue  # 知识库本身在资料文件夹里面：不扫，否则会把副本当成新资料
            if p.is_symlink():
                folders.append({"path": p, "reason": f"符号链接文件夹，未跟随（指向 {os.path.realpath(p)}）", "size": 0})
            elif d.startswith(".") or d in SKIP_DIRS:
                n, size = count_files(p)
                if n:
                    folders.append({"path": p, "reason": f"隐藏或依赖文件夹，含 {n} 个文件，未展开；需要其中的文件请拿出来再扫", "size": size})
            else:
                keep.append(d)
        dirs[:] = keep
        # 带“(1)”“副本”的排在后面，重复时保留原名那份
        for n in sorted(names, key=lambda x: (bool(COPY_MARK_RE.search(x)), x)):
            p = root_p / n
            if kb and p == kb / "00_原始资料" / "资料索引.md":
                continue  # 自动生成的索引本身不入库
            files.append(p)
    return files, folders


def mark_versions(rows: list[dict]) -> None:
    """同一文件的多个版本（v1/v2、终稿、修改版、(1)）：最新的留在原处，其余放进 历史版本/。"""
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if (r["状态"] != STATUS_COPY or r.get("类别") in ("照片", "代码", "题录", "待定", "") or r.get("子目录")
                or PINNED in r.get("依据", "")):
            continue
        base, marked, num = version_key(r["文件名"])
        if base:
            groups[(r["类别"], r["项目"], base, ext_family(Path(r["文件名"])))].append((r, marked, num))
    for members in groups.values():
        if len(members) < 2 or not any(m for _, m, _ in members):
            continue
        members.sort(key=lambda it: (it[2], float(it[0].get("修改时间") or 0)), reverse=True)
        cur = members[0][0]
        if "现行版" not in cur["依据"]:
            cur["依据"] += f"；现行版（另有 {len(members) - 1} 个旧版本放入 历史版本/）"
        for r, _, _ in members[1:]:
            r["子目录"] = "历史版本"
            r["依据"] += f"；旧版本，现行版是 #{cur['id']} {cur['文件名']}"


def target_key(r: dict) -> str:
    return (dest_dir(r["类别"], r["项目"], r.get("子目录", "")) + "/" + (r.get("入库名") or r["文件名"])).lower()


def mark_collisions(rows: list[dict], src: Path) -> None:
    """不同文件夹里的同名文件（如两个 1.jpg）会被放进同一目录：给它们保留来源文件夹名作子目录。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["状态"] == STATUS_COPY and r.get("类别"):
            groups[target_key(r)].append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        for r in members:
            parent = Path(r["原路径"]).parent
            if parent == src or PINNED in r.get("依据", ""):
                continue
            if parent.name in (r.get("子目录") or "").split("/"):
                continue
            r["子目录"] = "/".join(x for x in (r.get("子目录", ""), parent.name) if x)
            r["依据"] += f"；与其他文件同名，保留来源文件夹「{parent.name}」"


def cmd_scan(args: argparse.Namespace) -> None:
    src = Path(args.src).expanduser().resolve()
    kb = Path(args.kb).expanduser().resolve() if args.kb else None
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(kb) if kb else {}
    ignored = load_ignored(kb) if kb else {}
    outline_path = Path(args.outline).expanduser().resolve() if args.outline else (kb / OUTLINE if kb else None)
    outline = read_outline(outline_path)
    oprojects = outline.get("projects", [])
    onames = {p["name"] for p in oprojects}

    files, folders = walk_source(src, kb)
    rows, snippets, seen = [], [], {}

    def blocked(row: dict, reason: str) -> None:
        row.update({"状态": STATUS_BLOCKED, "依据": reason, "置信度": "高"})
        rows.append(row)

    for path in files:
        rel = path.relative_to(src)
        row = {"id": str(len(rows) + 1), "文件名": path.name, "原路径": str(path), "项目": guess_project(rel)}
        m = ICLOUD_RE.match(path.name)
        if m:
            row["文件名"] = m.group(1)
            blocked(row, "iCloud 里的文件还没下载到本机（只有占位文件），在访达里右键「立即下载」后重新扫描")
            continue
        st = stat_quiet(path)
        if st is None:
            blocked(row, "读不到文件信息（可能是失效的快捷方式或链接）")
            continue
        row.update({"大小KB": f"{st.st_size / 1024:.0f}", "日期": dt.date.fromtimestamp(st.st_mtime).isoformat(),
                    "修改时间": f"{st.st_mtime:.3f}"})
        low = path.name.lower()
        if low in JUNK_NAMES or low.startswith(("~$", "._", ".~")) or path.suffix.lower() in JUNK_EXT or path.name.startswith("."):
            row.update({"状态": STATUS_SKIP, "依据": "系统或临时文件", "置信度": "高"})
            rows.append(row)
            continue
        if st.st_size == 0:
            row.update({"状态": STATUS_SKIP, "依据": "空文件", "置信度": "高"})
            rows.append(row)
            continue
        try:
            digest = sha256(path)
        except OSError as e:
            blocked(row, f"没有读取权限或读取失败（{e.strerror or e}）")
            continue
        row["sha256"] = digest
        if digest in manifest:
            row.update({"状态": STATUS_DONE, "依据": f"已在知识库：{manifest[digest]['知识库路径']}", "置信度": "高"})
            rows.append(row)
            continue
        if digest in ignored:
            row.update({"状态": STATUS_SKIP, "依据": f"之前已决定不入库：{ignored[digest]}", "置信度": "高"})
            rows.append(row)
            continue
        if digest in seen:
            row.update({"状态": STATUS_SKIP, "依据": f"与 #{seen[digest]} 内容完全相同", "置信度": "高"})
            rows.append(row)
            continue
        seen[digest] = row["id"]

        try:
            status, cat, reason, conf, date, snippet = classify(path, rel)
        except OSError as e:
            blocked(row, f"读取内容失败（{e.strerror or e}）")
            continue
        if date:
            row["日期"] = date
        privacy = kw_hits(" ".join(rel.parts), "隐私")
        if privacy and status != STATUS_SKIP:
            status, conf = STATUS_AI, ""
            reason = f"疑似隐私文件（文件名含「{privacy[0]}」），先问用户是否入库；规则原判断：{cat or '无'}"
        # 有纲要时，项目以纲要为准：文件夹名是纲要项目 > 纲要关键词 > 按文件夹名猜的
        if oprojects and status in (STATUS_COPY, STATUS_AI):
            proj, why = outline_project(rel, snippet, oprojects)
            if proj or why:
                row["项目"] = proj
                reason += f"；{why}"
            elif row["项目"] and row["项目"] not in onames:
                reason += f"；文件夹名「{row['项目']}」不在纲要里"
        # 文件已经在知识库的 A/D 层里（用户手动放进去的）：只登记，不复制
        if kb and status != STATUS_SKIP:
            try:
                in_kb = path.relative_to(kb)
            except ValueError:
                in_kb = None
            if in_kb is not None and in_kb.parts[0][:3] in {"00_", "03_", "99_"}:
                cat, row["项目"] = locate_in_kb(in_kb)
                status, conf, reason = STATUS_REGISTER, "高", "已在知识库目录内，按所在位置登记，不复制"
                row["目标目录"] = path.parent.relative_to(kb).as_posix()
        row.update({"状态": status, "类别": cat, "依据": reason, "置信度": conf})
        rows.append(row)
        if status == STATUS_AI or snippet:
            snippets.append({"id": row["id"], "文件名": path.name, "原路径": str(path), "状态": status, "片段": snippet[:600]})

    for fd in folders:
        rows.append({"id": str(len(rows) + 1), "状态": STATUS_SKIP, "文件名": fd["path"].name + "/", "原路径": str(fd["path"]),
                     "依据": fd["reason"], "置信度": "高", "大小KB": f"{fd['size'] / 1024:.0f}"})

    mark_versions(rows)
    mark_collisions(rows, src)
    for r in rows:
        if r["状态"] in (STATUS_COPY, STATUS_AI) and is_generic_name(r["文件名"]):
            r["依据"] += "；文件名看不出内容，建议起入库名"
        if r["状态"] == STATUS_COPY and r.get("类别"):
            r["目标目录"] = dest_dir(r["类别"], r["项目"], r.get("子目录", ""))

    meta = {"src": str(src), "kb": str(kb) if kb else "", "扫描时间": dt.datetime.now().isoformat(timespec="seconds"),
            "纲要": outline.get("path", ""), "纲要项目": [p["name"] for p in oprojects],
            "文件数": len(files) + sum(int(FOLDER_COUNT_RE.search(f["reason"]).group(1)) for f in folders
                                     if FOLDER_COUNT_RE.search(f["reason"]))}
    (out / "scan_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    plan_csv = out / "plan.csv"
    write_plan(plan_csv, rows)
    with (out / "snippets.jsonl").open("w", encoding="utf-8") as f:
        for s in snippets:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    write_plan_md(out / "plan.md", rows, meta)
    summarize(rows, plan_csv)


def load_meta(plan_csv: Path) -> dict:
    p = plan_csv.with_name("scan_meta.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_plan_md(path: Path, rows: list[dict], meta: dict | None = None) -> None:
    meta = meta or {}
    files = [r for r in rows if not r["文件名"].endswith("/")]
    by_status = Counter(r["状态"] for r in files)
    folder_rows = [r for r in rows if r["文件名"].endswith("/")]
    lines = ["# 建库方案（待确认）", ""]
    if meta.get("src"):
        lines += [f"资料来源：`{meta['src']}`（扫描于 {meta.get('扫描时间', '')}）", ""]
    lines += [
        f"共 {len(files)} 个文件：" + "，".join(f"{k} {v}" for k, v in by_status.most_common()) + "。"
        + (f"另有 {len(folder_rows)} 个文件夹未展开（见“不处理”）。" if folder_rows else ""),
        "原件只复制不移动；确认前不会写入知识库。修改方案可直接编辑 plan.csv 的「类别」「项目」「子目录」「入库名」「状态」。",
    ]
    blocked = [r for r in rows if r["状态"] == STATUS_BLOCKED]
    if blocked:
        lines += ["", "## ⚠ 读不了的文件（不会入库，需要你处理）", "", "| # | 文件 | 原因 |", "|---|---|---|"]
        lines += [f"| {r['id']} | {r['文件名']} | {r['依据']} |" for r in blocked]
    lines += ["", "## 将放到哪里", "", "| 目标目录 | 文件数 | 其中低/中置信度 |", "|---|---|---|"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["状态"] in (STATUS_COPY, STATUS_REGISTER):
            groups[r.get("目标目录") or dest_dir(r["类别"], r["项目"], r.get("子目录", ""))].append(r)
    for d in sorted(groups):
        unsure = sum(1 for r in groups[d] if r["置信度"] in ("中", "低"))
        lines.append(f"| `{d}` | {len(groups[d])} | {unsure} |")

    def table(title: str, sel: list[dict], cols: str, fmt) -> None:
        if sel:
            lines.extend(["", f"## {title}", "", cols, "|" + "---|" * (cols.count("|") - 1)])
            lines.extend(fmt(r) for r in sel)

    where = lambda r: f"{r['类别']}{'/' + r['项目'] if r['项目'] else ''}{'/' + r['子目录'] if r.get('子目录') else ''}"
    table("请扫一眼：规则判断把握一般的",
          [r for r in rows if r["状态"] == STATUS_COPY and r["置信度"] in ("中", "低")],
          "| # | 文件 | 归到 | 依据 |", lambda r: f"| {r['id']} | {r['文件名']} | {where(r)} | {r['依据']} |")
    table("旧版本（放进 历史版本/，不删除）", [r for r in rows if r.get("子目录", "").startswith("历史版本")],
          "| # | 文件 | 归到 | 说明 |", lambda r: f"| {r['id']} | {r['文件名']} | {where(r)} | {r['依据'].split('；')[-1]} |")
    table("文件名看不出内容的（AI 可建议入库名，原件不改名）",
          [r for r in rows if "建议起入库名" in r["依据"] and not r.get("入库名")],
          "| # | 文件 | 归到 |", lambda r: f"| {r['id']} | {r['文件名']} | {where(r) if r['类别'] else '待判断'} |")
    table("将改名入库的（只改副本）", [r for r in rows if r.get("入库名")],
          "| # | 原名 | 入库名 |", lambda r: f"| {r['id']} | {r['文件名']} | {r['入库名']} |")
    table("待 AI 判断", [r for r in rows if r["状态"] == STATUS_AI],
          "| # | 文件 | 原因 |", lambda r: f"| {r['id']} | {r['文件名']} | {r['依据']} |")
    table("不处理", [r for r in rows if r["状态"] in (STATUS_SKIP, STATUS_DONE)],
          "| # | 文件 | 原因 |", lambda r: f"| {r['id']} | {r['文件名']} | {r['依据']} |")

    projects = Counter(r["项目"] for r in rows if r["状态"] in (STATUS_COPY, STATUS_AI, STATUS_REGISTER))
    oproj = meta.get("纲要项目") or []
    if oproj:
        lines += ["", "## 识别到的项目（以纲要为准）", ""]
        for name in oproj:
            n = projects.get(name, 0)
            lines.append(f"- {name}：{n} 个文件" + ("" if n else "（⚠ 没分到任何文件：关键词可能不对，或还没有这个项目的资料）"))
        others = [(p, n) for p, n in projects.most_common() if p and p not in oproj]
        if others:
            lines += ["", "不在纲要里的项目名（多半是文件夹名；并入纲要里的项目，或问用户要不要加进纲要）：", ""]
            lines += [f"- {p}：{n} 个文件" for p, n in others]
        if projects.get(""):
            lines.append(f"\n未分项目：{projects['']} 个文件。很多的话，先补纲要关键词再扫一次。")
        by_kw = sum(1 for r in rows if "纲要关键词" in r.get("依据", ""))
        lines.append(f"\n其中 {by_kw} 个文件靠纲要关键词分到项目。纲要：`{meta.get('纲要', '')}`")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    known: set[str] = set()
    if meta.get("kb"):
        known = {m["项目"] for m in load_manifest(Path(meta["kb"])).values() if m.get("项目")}
    lines += ["", "## 识别到的项目", "", "> 没有知识库纲要，项目只按文件夹名猜。建议先做纲要访谈。", ""]
    for p, n in projects.most_common():
        note = ""
        if known and p and p not in known:
            close = difflib.get_close_matches(p, sorted(known), n=1, cutoff=0.4)
            note = f" ⚠ 知识库里还没有这个项目" + (f"，可能就是「{close[0]}」" if close else "")
        lines.append(f"- {p or '（未分项目）'}：{n} 个文件{note}")
    if known:
        lines.append(f"\n知识库现有项目：{'、'.join(sorted(known))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize(rows: list[dict], plan_csv: Path) -> None:
    files = [r for r in rows if not r["文件名"].endswith("/")]
    c = Counter(r["状态"] for r in files)
    cats = Counter(r["类别"] for r in rows if r["状态"] == STATUS_COPY)
    print(f"方案表：{plan_csv}")
    print("状态：" + "，".join(f"{k} {v}" for k, v in c.most_common()))
    print("类别：" + "，".join(f"{k} {v}" for k, v in cats.most_common()))
    folders = len(rows) - len(files)
    if folders:
        print(f"未展开的文件夹：{folders} 个（隐藏、依赖或符号链接，见 plan.md“不处理”）")
    if c.get(STATUS_BLOCKED):
        print(f"⚠ 读不了：{c[STATUS_BLOCKED]} 个，必须告诉用户（见 plan.md 顶部）")
    pending = [r["id"] for r in rows if r["状态"] == STATUS_AI]
    if pending:
        print(f"待AI：{len(pending)} 个（id: {', '.join(pending)}），片段见 snippets.jsonl")


# ---------------------------------------------------------------- decide

def cmd_decide(args: argparse.Namespace) -> None:
    plan_csv = Path(args.plan).expanduser().resolve()
    rows = read_plan(plan_csv)
    data = json.loads(Path(args.decisions).expanduser().read_text(encoding="utf-8"))
    renames = data.get("rename_projects", {})
    items = data.get("items", {})
    by_id = {r["id"]: r for r in rows}
    errors = []
    for rid, d in items.items():
        r = by_id.get(str(rid))
        if r is None:
            errors.append(f"没有 id {rid}")
            continue
        cat = d.get("类别", r.get("类别", ""))
        if cat and cat not in CATEGORIES:
            errors.append(f"id {rid} 的类别「{cat}」不在可选类别里：{'、'.join(CATEGORIES)}")
            continue
        if "入库名" in d:
            new = str(d["入库名"]).strip()
            ext = Path(r["文件名"]).suffix
            if not new or "/" in new or "\\" in new:
                errors.append(f"id {rid} 的入库名「{new}」不能为空或带路径")
                continue
            if not Path(new).suffix:
                new += ext
            elif Path(new).suffix.lower() != ext.lower():
                errors.append(f"id {rid} 的入库名「{new}」改了扩展名（原来是 {ext}）")
                continue
            r["入库名"] = new
        r["类别"] = cat
        for field in ("项目", "子目录"):
            if field in d:
                r[field] = d[field]
        r["状态"] = d.get("状态", STATUS_COPY if r["状态"] == STATUS_AI else r["状态"])
        if "依据" in d:
            r["依据"] = "AI：" + d["依据"]
        if "子目录" in d and PINNED not in r["依据"]:
            r["依据"] += PINNED
        r["置信度"] = d.get("置信度", "AI")
    for r in rows:
        if r.get("项目") in renames:
            r["项目"] = renames[r["项目"]]
    # 归组、改名之后可能出现新的版本组和同名冲突，再标一次
    meta = load_meta(plan_csv)
    mark_versions(rows)
    if meta.get("src"):
        mark_collisions(rows, Path(meta["src"]))
    for r in rows:
        if r["状态"] == STATUS_COPY and r.get("类别"):
            r["目标目录"] = dest_dir(r["类别"], r["项目"], r.get("子目录", ""))
        elif r["状态"] not in (STATUS_COPY, STATUS_REGISTER):
            r["目标目录"] = ""
        # 登记的文件不移动，目标目录保持它现在的位置
    write_plan(plan_csv, rows)
    write_plan_md(plan_csv.with_name("plan.md"), rows, meta)
    for e in errors:
        print("错误：" + e, file=sys.stderr)
    summarize(rows, plan_csv)
    if errors:
        sys.exit(1)


# ---------------------------------------------------------------- 初始化骨架

def fill(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


GAP_LIST = "01_知识提炼/研究问题/缺口清单.md"
LEDGER = "01_知识提炼/研究问题/假设台账.md"
# templates/root 里的文件 → 在知识库中的位置
PLACED_FILES = {
    "知识库纲要.md": OUTLINE,  # 建库时 AI 通常已按访谈写好，这里只在缺失时放空模板
    "AGENTS.md": "AGENTS.md",
    "CLAUDE.md": "CLAUDE.md",
    "知识库导航.md": "知识库导航.md",
    "知识库说明.md": "知识库说明.md",
    "知识库变更日志.md": "知识库变更日志.md",
    "INBOX.md": "INBOX.md",
    "假设台账.md": LEDGER,
    "缺口清单.md": GAP_LIST,
    "知识库体检提示词.md": "02_方法论库/AI提示词/知识库体检提示词.md",
    "缺口自检提示词.md": "02_方法论库/AI提示词/缺口自检提示词.md",
}


def ensure_skeleton(kb: Path, name: str) -> list[str]:
    created = []
    for d in SKELETON:
        p = kb / d
        if not p.exists():
            p.mkdir(parents=True)
            created.append(d + "/")
    values = {"知识库名": name, "日期": dt.date.today().isoformat()}
    # 入口文件、台账、提示词：已有就不覆盖
    for tpl, dest in PLACED_FILES.items():
        target = kb / dest
        if not target.exists() and (TEMPLATE_DIR / "root" / tpl).exists():
            target.write_text(fill((TEMPLATE_DIR / "root" / tpl).read_text(encoding="utf-8"), values), encoding="utf-8")
            created.append(dest)
    # 笔记模板放进库里，换任何 AI 都能找到
    for tpl in sorted((TEMPLATE_DIR / "notes").glob("*.md")):
        target = kb / "02_方法论库/写作模板" / tpl.name
        if not target.exists():
            shutil.copy2(tpl, target)
            created.append(str(target.relative_to(kb)))
    (kb / ".kb").mkdir(exist_ok=True)
    if not (kb / "知识索引.md").exists():
        write_index(kb)
        created.append("知识索引.md")
    return created


LOG_TAGS = ["建库", "入库", "蒸馏", "回流", "缺口", "体检", "决策", "整理"]


def append_changelog(kb: Path, line: str, tag: str = "入库") -> None:
    """在标记行下、今天的日期标题下追加一行，行首带类型标签，stats 按标签统计。"""
    line = f"[{tag}] {line}"
    log = kb / "知识库变更日志.md"
    today = dt.date.today().isoformat()
    marker = "<!-- 新记录写在这一行下面 -->"
    text = log.read_text(encoding="utf-8") if log.exists() else f"# 知识库变更日志\n\n{marker}\n"
    if marker not in text:
        text = text.rstrip() + f"\n\n{marker}\n"
    head, tail = text.split(marker, 1)
    heading = f"## {today}"
    rest = tail.lstrip("\n")
    if rest.startswith(heading):
        tail = f"\n\n{heading}\n- {line}" + rest[len(heading):]
    else:
        tail = f"\n\n{heading}\n- {line}\n\n" + rest
    log.write_text(head + marker + tail, encoding="utf-8")


def write_source_index(kb: Path) -> None:
    manifest = load_manifest(kb)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in manifest.values():
        groups[str(Path(row["知识库路径"]).parent)].append(row)
    lines = [
        "---", "tags:", "  - 类型/索引", f"更新: {dt.date.today().isoformat()}", "---", "",
        "# 资料索引", "",
        "> [!info] 由 `kb.py apply` 自动生成，请勿手改。",
        "> 列出所有经过入库流程的原始资料与产出文件。AI 需要读原文时，先在这里定位，再打开具体文件。", "",
    ]
    for d in sorted(groups):
        lines += [f"## {d}", ""]
        for row in sorted(groups[d], key=lambda r: r["知识库路径"]):
            p = row["知识库路径"]
            orig = Path(row["原路径"]).name
            renamed = f" · 原名 {orig}" if orig != Path(p).name else ""
            lines.append(f"- [[{p}|{Path(p).name}]]  · {row['类别']} · 入库 {row['入库时间'][:10]}{renamed}")
        lines.append("")
    (kb / "00_原始资料" / "资料索引.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- 知识卡片与知识索引
#
# 知识卡片 = 01_知识提炼/ 和 02_方法论库/（模板除外）里的笔记。卡片头部的
#   类型 / 一句话 / 用于 / 来源 / 项目
# 决定它在知识索引里怎么显示。“用于”写“什么时候该想起它”，AI 靠它把当前任务和过去的知识对上。

CARD_DIRS = ("01_知识提炼", "02_方法论库")
TYPE_ORDER = ["概念", "决策", "问题", "教训", "SOP", "人物", "文献", "研究问题", "提示词", "其他"]
FM_LINE_RE = re.compile(r"^([^\s:#\-][^:]*?):\s*(.*)$")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def read_frontmatter(text: str) -> dict[str, str]:
    """只解析顶层的 `键: 值`；列表项（aliases 等）跳过，模板占位符 {{…}} 当作空值。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = FM_LINE_RE.match(line)
        if m:
            value = m.group(2).strip().strip("'\"").strip()
            out[m.group(1).strip()] = "" if "{{" in value else value
    return out


def infer_type(rel: Path, fm: dict) -> str:
    """优先用卡片头部的「类型」；没写时按所在文件夹和文件名推断，兼容不是 kb-starter 建的库。"""
    t = fm.get("类型", "")
    if t:
        return t if t in TYPE_ORDER else "其他"
    parts = rel.parts[:-1]
    joined = "/".join(parts)
    if "研究问题" in joined:
        return "研究问题"
    # 只认“概念节点”“概念”“领域地图”这样的文件夹；“文献概念梳理”这种混合名不算
    if any(p.endswith("概念节点") or p == "概念" or "领域地图" in p for p in parts):
        return "概念"
    if rel.stem.startswith("Paper_") or "文献" in joined:
        return "文献"
    if "决策" in joined or "指示" in rel.stem:
        return "决策"
    if "教训" in joined or "复盘" in joined:
        return "教训"
    if "问题" in joined:
        return "问题"
    if "人物" in joined:
        return "人物"
    if "SOP" in rel.stem or "SOP" in joined or "方案" in joined:
        return "SOP"
    if "提示词" in joined:
        return "提示词"
    return "其他"


def project_of(fm: dict) -> str:
    v = fm.get("项目") or fm.get("关联项目") or ""
    v = re.sub(r"[\[\]\"']", "", v)
    return re.sub(r"(项目)?_总览$", "", v).strip()


def knowledge_cards(kb: Path) -> list[dict]:
    cards = []
    for top in CARD_DIRS:
        base = kb / top
        if not base.exists():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not is_archive_dir(d) and d != "写作模板")
            for fn in sorted(files):
                if not fn.endswith(".md") or fn.startswith("."):
                    continue
                p = Path(root) / fn
                rel = p.relative_to(kb)
                text = p.read_text(encoding="utf-8", errors="ignore")
                fm = read_frontmatter(text)
                st = p.stat()
                created = fm.get("创建", "")[:10]
                if not DATE_RE.fullmatch(created):
                    created = dt.date.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime)).isoformat()
                cards.append({
                    "path": rel.as_posix(), "name": p.stem, "type": infer_type(rel, fm),
                    "one": fm.get("一句话", ""), "use": fm.get("用于", ""), "project": project_of(fm),
                    "source": fm.get("来源", "") or fm.get("原文", ""), "created": created,
                    "mtime": st.st_mtime, "text": text,
                })
    return cards


def fallback_summary(text: str, limit: int = 60) -> str:
    s = first_paragraph(text, limit)
    return re.sub(r"^(一句话|核心概念|一句话结论|用途|这是什么)\s*[:：]?\s*", "", s)


def build_index(kb: Path, cards: list[dict], compact: bool) -> str:
    names = Counter(c["name"] for c in cards)
    lines = [
        "---", "tags:", "  - 类型/索引", f"更新: {dt.date.today().isoformat()}", "---", "",
        "# 知识索引", "",
        "> [!info] 由 `kb.py index` 自动生成，请勿手改。",
        "> **AI 每次开工先读这里**：它列出这个知识库已经“知道”什么。看到和当前任务相关的条目，再打开对应笔记；"
        "回答里用到了哪条，写出来。",
        "> 每行显示卡片头部的「一句话」和「用于」（什么时候该想起它）。要改显示内容，改卡片本身，然后重新生成。", "",
    ]
    overviews = sorted(kb.glob("*_总览.md"))
    if overviews:
        lines += ["## 项目", ""]
        for ov in overviews:
            fm = read_frontmatter(ov.read_text(encoding="utf-8", errors="ignore"))
            state = fm.get("状态", "")
            lines.append(f"- [[{ov.stem}]]" + (f" — {state}" if state else ""))
        lines.append("")
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        groups[c["type"]].append(c)
    for t in TYPE_ORDER:
        if not groups.get(t):
            continue
        lines += [f"## {t}（{len(groups[t])}）", ""]
        for c in sorted(groups[t], key=lambda c: (c["project"], c["name"])):
            link = c["name"] if names[c["name"]] == 1 else c["path"].removesuffix(".md")
            one = c["one"] or fallback_summary(c["text"])
            use = c["use"]
            if compact:
                one = one[:40] + ("…" if len(one) > 40 else "")
                use = use[:30] + ("…" if len(use) > 30 else "")
            line = f"- [[{link}]]" + (f" — {one}" if one else "")
            if use:
                line += f" · 用于：{use}"
            if c["project"]:
                line += f" · {c['project']}"
            lines.append(line)
        lines.append("")
    missing = [c for c in cards if c["type"] not in ("研究问题", "提示词") and (not c["one"] or not c["use"])]
    if missing:
        lines += [f"## 待补索引信息（{len(missing)}）", "",
                  "> 这些卡片头部缺「一句话」或「用于」，AI 很难在需要时想起它们。下次打开时顺手补上。", ""]
        lines += [f"- `{c['path']}`" for c in missing[:40]]
        if len(missing) > 40:
            lines.append(f"- ……另有 {len(missing) - 40} 张")
        lines.append("")
    return "\n".join(lines)


def write_index(kb: Path, out: Path | None = None, max_chars: int = 12000) -> dict:
    cards = knowledge_cards(kb)
    text = build_index(kb, cards, compact=False)
    compact = len(text) > max_chars
    if compact:
        text = build_index(kb, cards, compact=True)
    target = out or kb / "知识索引.md"
    target.write_text(text, encoding="utf-8")
    return {"path": target, "cards": cards, "chars": len(text), "compact": compact,
            "missing": sum(1 for c in cards if c["type"] not in ("研究问题", "提示词") and (not c["one"] or not c["use"]))}


def cmd_index(args: argparse.Namespace) -> None:
    kb = Path(args.kb).expanduser().resolve()
    res = write_index(kb, Path(args.out).expanduser() if args.out else None, args.max_chars)
    types = Counter(c["type"] for c in res["cards"])
    print(f"已生成 {res['path']}（{res['chars']} 字）")
    print(f"知识卡片 {len(res['cards'])} 张：" + "，".join(f"{t} {types[t]}" for t in TYPE_ORDER if types[t]))
    if res["missing"]:
        print(f"{res['missing']} 张卡片缺「一句话」或「用于」，列在索引末尾")
    if res["compact"]:
        print(f"索引超过 {args.max_chars} 字，已改用精简格式；仍嫌长时可按项目拆分知识库或提高 --max-chars")


# ---------------------------------------------------------------- 对账

def reconcile(rows: list[dict], kb: Path, meta: dict, full: bool = False) -> tuple[list[str], list[str]]:
    """返回 (问题清单, 对账单正文)。问题清单为空 = 原件完好、一个不漏、副本一致。"""
    problems: list[str] = []
    manifest = load_manifest(kb)
    file_rows = [r for r in rows if not r["文件名"].endswith("/")]
    folder_rows = [r for r in rows if r["文件名"].endswith("/")]
    in_folders = sum(int(m.group(1)) for r in folder_rows if (m := FOLDER_COUNT_RE.search(r["依据"])))

    # 1. 一个不漏：重新遍历来源，每个文件都要在方案里，或在“未展开的文件夹”里
    unplanned: list[str] = []
    vanished: list[str] = []
    src = Path(meta["src"]) if meta.get("src") else None
    if src and src.exists():
        files_now, _ = walk_source(src, Path(meta["kb"]) if meta.get("kb") else None)
        now = {str(p) for p in files_now}
        planned = {r["原路径"] for r in file_rows}
        prefixes = tuple(r["原路径"] + os.sep for r in folder_rows)
        unplanned = sorted(p for p in now if p not in planned and not p.startswith(prefixes))
        vanished = sorted(r["原路径"] for r in file_rows if r["原路径"] not in now)
    if unplanned:
        problems.append(f"扫描之后来源里多了 {len(unplanned)} 个文件，不在方案里（重新扫描即可）：" + "、".join(Path(p).name for p in unplanned[:10]))
    if vanished:
        problems.append(f"方案里有 {len(vanished)} 个文件已不在原位置（不是本工具移动的）：" + "、".join(Path(p).name for p in vanished[:10]))

    # 2. 每个要入库的文件都在清单里、副本存在、内容一致
    copied_ok = 0
    for r in file_rows:
        if r["状态"] not in (STATUS_COPY, STATUS_REGISTER):
            if not r.get("依据"):
                problems.append(f"#{r['id']} {r['文件名']} 状态是「{r['状态']}」但没有写理由")
            continue
        m = manifest.get(r.get("sha256", ""))
        if not m:
            problems.append(f"#{r['id']} {r['文件名']} 应入库但不在清单里")
            continue
        target = kb / m["知识库路径"]
        if not target.exists():
            problems.append(f"#{r['id']} {r['文件名']} 的副本不见了：{m['知识库路径']}")
        elif sha256(target) != r["sha256"]:
            problems.append(f"#{r['id']} {r['文件名']} 的副本和原件内容不一致：{m['知识库路径']}")
        else:
            copied_ok += 1

    # 3. 原件没被改：默认比修改时间，--full 时重新算哈希
    changed = []
    for r in file_rows:
        p = Path(r["原路径"])
        if not r.get("sha256") or not p.exists():
            continue
        if full:
            try:
                if sha256(p) != r["sha256"]:
                    changed.append(r["文件名"])
            except OSError:
                pass
        elif r.get("修改时间"):
            st = stat_quiet(p)
            if st and abs(st.st_mtime - float(r["修改时间"])) > 0.01:
                changed.append(r["文件名"])
    if changed:
        problems.append(f"{len(changed)} 个原件在扫描之后被修改过（不是本工具改的）：" + "、".join(changed[:10]))

    c = Counter(r["状态"] for r in file_rows)
    need = c.get(STATUS_COPY, 0) + c.get(STATUS_REGISTER, 0)
    total = len(file_rows) + in_folders
    body = [
        f"- 来源共 **{total}** 个文件 = 复制 {c.get(STATUS_COPY, 0)} + 登记 {c.get(STATUS_REGISTER, 0)} + 已入库 {c.get(STATUS_DONE, 0)}"
        f" + 跳过 {c.get(STATUS_SKIP, 0)} + 无法读取 {c.get(STATUS_BLOCKED, 0)} + 未展开文件夹内 {in_folders}",
        f"- 副本校验：{copied_ok}/{need} 个与原件哈希一致",
        f"- 原件检查（{'重新计算哈希' if full else '比对修改时间'}）：{'有改动' if changed else '均未改动'}",
        f"- 扫描后新增 {len(unplanned)} 个，消失 {len(vanished)} 个",
    ]
    if c.get(STATUS_BLOCKED):
        body.append(f"- ⚠ {c[STATUS_BLOCKED]} 个文件读不了，没有入库：" + "、".join(r["文件名"] for r in file_rows if r["状态"] == STATUS_BLOCKED))
    return problems, body


def write_reconcile(kb: Path, stamp: str, problems: list[str], body: list[str]) -> Path:
    path = kb / ".kb" / "plans" / f"{stamp}-对账.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 对账单 {stamp}", "", *body, "", "## 结论", ""]
    lines += [f"- ❌ {p}" for p in problems] or ["- ✅ 原件完好、一个不漏、副本一致"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- apply

def cmd_apply(args: argparse.Namespace) -> None:
    plan_csv = Path(args.plan).expanduser().resolve()
    kb = Path(args.kb).expanduser().resolve()
    rows = read_plan(plan_csv)
    meta = load_meta(plan_csv)
    pending = [r for r in rows if r["状态"] == STATUS_AI]
    if pending and not args.pending_to_unsorted:
        print(f"还有 {len(pending)} 个「待AI」条目没有判断（id: {', '.join(r['id'] for r in pending)}）。"
              "先用 decide 写回，或加 --pending-to-unsorted 把它们放进 09_待定。", file=sys.stderr)
        sys.exit(2)
    bad = [r for r in rows if r["状态"] in (STATUS_COPY, STATUS_REGISTER) and r["类别"] not in CATEGORIES]
    if bad:
        print("这些条目的类别无效：" + "、".join(f"#{r['id']}「{r['类别']}」" for r in bad), file=sys.stderr)
        sys.exit(2)

    name = args.name or kb.name
    created = [] if args.dry_run else ensure_skeleton(kb, name)
    manifest = load_manifest(kb)
    now = dt.datetime.now().isoformat(timespec="seconds")
    copied, registered, existed, renamed = [], [], [], []
    for r in rows:
        status = r["状态"]
        if status == STATUS_AI and args.pending_to_unsorted:
            status = r["状态"] = STATUS_COPY
            r["类别"], r["子目录"] = "待定", ""
        if status not in (STATUS_COPY, STATUS_REGISTER):
            continue
        src = Path(r["原路径"])
        if not src.exists():
            print(f"找不到原文件，跳过：{src}", file=sys.stderr)
            continue
        digest = r.get("sha256") or sha256(src)
        r["sha256"] = digest
        if digest in manifest:
            existed.append(r)
            continue
        if status == STATUS_REGISTER:
            target = src
            registered.append(r)
        else:
            target_dir = kb / dest_dir(r["类别"], r["项目"], r.get("子目录", ""))
            fname = r.get("入库名") or src.name
            target = target_dir / fname
            n = 2
            while target.exists():
                if sha256(target) == digest:
                    break
                target = target_dir / f"{Path(fname).stem} ({n}){Path(fname).suffix}"
                n += 1
            if target.name != src.name:
                renamed.append(f"{src.name} → {target.relative_to(kb)}")
            if not args.dry_run and not target.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)  # 复制，保留时间戳；原件不动
            copied.append(r)
        manifest[digest] = {
            "sha256": digest, "知识库路径": str(target.relative_to(kb)), "原路径": str(src),
            "类别": r["类别"], "项目": r["项目"], "入库时间": now,
        }

    if args.dry_run:
        print(f"[预演] 将复制 {len(copied)} 个、登记 {len(registered)} 个、已存在 {len(existed)} 个。未写入任何文件。")
        for x in renamed:
            print(f"  改名入库：{x}")
        return

    with (kb / ".kb" / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for row in sorted(manifest.values(), key=lambda x: x["知识库路径"]):
            w.writerow(row)
    # AI 或用户决定不入库的文件记下来，下次扫描同一份文件时不再问
    ignored = load_ignored(kb)
    for r in rows:
        if r["状态"] == STATUS_SKIP and r.get("sha256") and r["依据"].startswith("AI："):
            ignored[r["sha256"]] = f"{r['文件名']} · {r['依据'][3:]}"
    if ignored:
        with (kb / ".kb" / "ignored.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sha256", "原因"])
            w.writerows(sorted(ignored.items()))
    plans_dir = kb / ".kb" / "plans"
    plans_dir.mkdir(exist_ok=True)
    stamp = base_stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 2
    while (plans_dir / f"{stamp}-plan.csv").exists():  # 同一秒内两次入库，存档不能互相覆盖
        stamp, n = f"{base_stamp}-{n}", n + 1
    shutil.copy2(plan_csv, plans_dir / f"{stamp}-plan.csv")
    if plan_csv.with_name("scan_meta.json").exists():
        shutil.copy2(plan_csv.with_name("scan_meta.json"), plans_dir / f"{stamp}-scan_meta.json")
    write_source_index(kb)

    problems, body = reconcile(rows, kb, meta)
    report = write_reconcile(kb, stamp, problems, body)
    verdict = "对账通过" if not problems else f"对账发现 {len(problems)} 处问题"

    cats = Counter(r["类别"] for r in copied + registered)
    detail = "、".join(f"{k} {v}" for k, v in cats.most_common())
    if copied or registered:
        append_changelog(kb, f"**入库 {len(copied) + len(registered)} 个文件**（{detail}）：原件未改动，{verdict}；"
                             f"方案表和对账单存于 `.kb/plans/{stamp}-*`，清单见 [[资料索引]]。")
    print(f"知识库：{kb}")
    if created:
        print(f"新建骨架 {len(created)} 项（目录和入口文件）")
    print(f"复制 {len(copied)} 个，登记 {len(registered)} 个，已存在跳过 {len(existed)} 个")
    if cats:
        print("类别：" + detail)
    for x in renamed:
        print(f"改名入库：{x}")
    print("已更新：.kb/manifest.csv、00_原始资料/资料索引.md、知识库变更日志.md")
    print(f"\n对账单：{report}")
    print("\n".join(body))
    for p in problems:
        print(f"❌ {p}", file=sys.stderr)
    if problems:
        sys.exit(3)
    print("✅ 原件完好、一个不漏、副本一致")


def cmd_verify(args: argparse.Namespace) -> None:
    plan_csv = Path(args.plan).expanduser().resolve()
    kb = Path(args.kb).expanduser().resolve()
    rows = read_plan(plan_csv)
    problems, body = reconcile(rows, kb, load_meta(plan_csv), full=args.full)
    print("\n".join(body))
    for p in problems:
        print(f"❌ {p}")
    if problems:
        sys.exit(3)
    print("✅ 原件完好、一个不漏、副本一致")


# ---------------------------------------------------------------- lint

WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]\|#\^]+)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")
EMBED_RE = re.compile(r"!\[\[([^\]\|#\^]+)")
BOLD_RE = re.compile(r"\*\*([^*\n]{2,16})\*\*\s*([:：]?)")
LINT_EXCLUDE = ("99_工作区", "02_方法论库/写作模板", ".kb", ".obsidian", ".trash", "projects")


def is_archive_dir(name: str) -> bool:
    """备份和历史版本不参与体检：它们本来就不该被链接，也不该算“近期改动”。"""
    low = name.lower()
    return "backup" in low or "备份" in name or name.startswith("历史") or name in {"旧版", "归档", "archive"}


def md_files(kb: Path) -> list[Path]:
    out = []
    for root, dirs, names in os.walk(kb):
        rel_root = Path(root).relative_to(kb)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS and not is_archive_dir(d)
                   and not str(rel_root / d).startswith(LINT_EXCLUDE)]
        if str(rel_root).startswith(LINT_EXCLUDE):
            continue
        out += [Path(root) / n for n in names if n.endswith(".md")]
    return out


def strip_code(text: str) -> str:
    """去掉代码块、行内代码和 HTML 注释，它们里面的 [[ ]] 不是真链接。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


ROOT_ENTRIES = {"AGENTS.md", "CLAUDE.md", "知识库导航.md", "知识库说明.md", "知识库变更日志.md", "INBOX.md", "知识索引.md"}
CROWDED = 150


def tidy_report(kb: Path) -> list[str]:
    out: list[str] = []
    # 根目录只放入口
    extra = sorted(p.name + ("/" if p.is_dir() else "") for p in kb.iterdir()
                   if not p.name.startswith(".") and p.name not in ROOT_ENTRIES and not p.name.endswith("_总览.md")
                   and not re.match(r"\d{2}_", p.name))
    out.append(f"- 根目录多出的文件/文件夹：{'、'.join(extra) if extra else '无'}" + ("（有意保留的可忽略）" if extra else ""))
    # 待定区积压
    pending = kb / "00_原始资料" / "09_待定"
    if pending.exists():
        items = [p for p in pending.rglob("*") if p.is_file() and not p.name.startswith(".")]
        if items:
            oldest = min(p.stat().st_mtime for p in items)
            days = (dt.datetime.now() - dt.datetime.fromtimestamp(oldest)).days
            out.append(f"- `09_待定` 有 {len(items)} 个文件，最早的已放了 {days} 天：" + "、".join(p.name for p in items[:8]))
        else:
            out.append("- `09_待定` 是空的")
    # INBOX 积压
    inbox = kb / "INBOX.md"
    if inbox.exists():
        body = inbox.read_text(encoding="utf-8", errors="ignore").split("---", 1)[-1]
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        entries = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith(("#", ">"))]
        out.append(f"- INBOX 有 {len(entries)} 条待归位")
    # 过满的文件夹、并存的多个版本
    crowded, versions = [], []
    for root, dirs, files in os.walk(kb):
        rel = Path(root).relative_to(kb)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS and not is_archive_dir(d)
                   and not str(rel / d).startswith(("99_工作区", "projects"))]
        files = [f for f in files if not f.startswith(".")]
        if len(files) > CROWDED:
            crowded.append(f"`{rel.as_posix()}/`（{len(files)} 个）")
        groups: dict[tuple, list[str]] = defaultdict(list)
        for f in files:
            base, marked, _ = version_key(f)
            groups[(base, ext_family(Path(f)))].append((f, marked))
        for (base, _), members in groups.items():
            if base and len(members) >= 2 and any(m for _, m in members):
                versions.append(f"`{rel.as_posix()}/`：" + "、".join(n for n, _ in sorted(members)))
    out.append(f"- 单个文件夹超过 {CROWDED} 个文件：{'、'.join(crowded) if crowded else '无'}"
               + ("（可按年份或子主题再分一层）" if crowded else ""))
    out.append(f"- 同一文件夹里多个版本并存（{len(versions)} 组，旧版建议移入 `历史版本/`）" + ("：" if versions else "：无"))
    out += [f"  - {v}" for v in versions[:20]]
    if len(versions) > 20:
        out.append(f"  - ……另有 {len(versions) - 20} 组")
    return out


def scan_links(kb: Path, notes: list[Path]) -> dict:
    """解析所有笔记里的 [[链接]]：能解析到哪个文件、谁链接了谁、哪些是断链，以及加粗词的分布。"""
    # 名字索引：Obsidian 按文件名（不含 .md）或相对路径解析链接
    all_files = []
    for root, dirs, names in os.walk(kb):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        all_files += [Path(root) / n for n in names]
    # resolve：链接里可能写的名字 → 被指向文件的相对路径
    # Obsidian 允许只写路径的后几段（如 [[图片文件夹/a.jpg]]），所以每个后缀都登记
    resolve: dict[str, str] = {}
    for f in all_files:
        rel = f.relative_to(kb).as_posix()
        parts = rel.split("/")
        for i in range(len(parts)):
            suffix = "/".join(parts[i:]).lower()
            resolve.setdefault(suffix, rel)
            if f.suffix == ".md":
                resolve.setdefault(suffix.removesuffix(".md"), rel)
    names = set(resolve)
    # frontmatter 里的 aliases 也能解析
    alias_re = re.compile(r"^aliases:\s*\n((?:\s+-\s+.*\n)+)", re.M)

    inbound: Counter = Counter()
    outlinks: dict[str, set[str]] = defaultdict(set)
    broken: dict[str, list[str]] = defaultdict(list)
    bold_in: dict[str, set[str]] = defaultdict(set)
    bold_label: Counter = Counter()
    bold_total: Counter = Counter()
    texts = {}
    for n in notes:
        t = n.read_text(encoding="utf-8", errors="ignore")
        texts[n] = t
        m = alias_re.search(t[:1500])
        if m:
            for a in re.findall(r"-\s+(.+)", m.group(1)):
                resolve.setdefault(a.strip().strip("'\"").lower(), n.relative_to(kb).as_posix())
    names = set(resolve)
    for n, t in texts.items():
        body = strip_code(t)
        rel = n.relative_to(kb).as_posix()
        for target in WIKILINK_RE.findall(body) + EMBED_RE.findall(body):
            tgt = target.strip().rstrip("\\").strip()  # 表格里写成 [[路径\|别名]]，去掉转义用的反斜杠
            if not tgt or "{{" in tgt:
                continue
            key = tgt.lower()
            hit_path = resolve.get(key) or resolve.get(key + ".md")
            if hit_path:
                if hit_path != rel:
                    inbound[hit_path] += 1
                    outlinks[rel].add(hit_path)
            else:
                broken[tgt].append(rel)
        for b, colon in BOLD_RE.findall(body):
            b = b.strip()
            if re.search(r"[\d%℃°/=:：，,。（）()]", b) or len(b) < 2:
                continue
            bold_in[b].add(rel)
            if colon:  # “**创新状态**：”这种是模板里的小标题，不是概念
                bold_label[b] += 1
            bold_total[b] += 1
    return {"names": names, "inbound": inbound, "outlinks": outlinks, "broken": broken, "texts": texts,
            "bold_in": bold_in, "bold_label": bold_label, "bold_total": bold_total}


def cmd_lint(args: argparse.Namespace) -> None:
    kb = Path(args.kb).expanduser().resolve()
    since = dt.date.fromisoformat(args.since) if args.since else dt.date.today() - dt.timedelta(days=args.days)
    notes = md_files(kb)
    links = scan_links(kb, notes)
    names, inbound, broken = links["names"], links["inbound"], links["broken"]
    bold_in, bold_label, bold_total = links["bold_in"], links["bold_label"], links["bold_total"]

    # 孤立笔记只查知识层（B/C）：产出层的中间稿天然没人链接，查了全是噪音
    layer_notes = [n for n in notes if n.relative_to(kb).parts[0][:3] in {"01_", "02_"}]
    orphans = sorted(n.relative_to(kb).as_posix() for n in layer_notes if inbound[n.relative_to(kb).as_posix()] == 0)

    manifest = load_manifest(kb)
    known_paths = {row["知识库路径"] for row in manifest.values()}
    unregistered = []
    raw_dir = kb / "00_原始资料"
    if raw_dir.exists():
        for root, dirs, files in os.walk(raw_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                p = Path(root) / fn
                rel = p.relative_to(kb).as_posix()
                if fn.startswith(".") or fn == "资料索引.md" or rel in known_paths:
                    continue
                unregistered.append(rel)

    changed = sorted(
        (n for n in notes if dt.date.fromtimestamp(n.stat().st_mtime) >= since),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )

    overviews = sorted(kb.glob("*_总览.md"))
    stale = []
    for ov in overviews:
        project = ov.stem.removesuffix("_总览")
        ov_mtime = ov.stat().st_mtime
        newer = [n for n in notes if project in n.relative_to(kb).as_posix() and n != ov and n.stat().st_mtime > ov_mtime]
        if newer:
            stale.append((ov.name, dt.date.fromtimestamp(ov_mtime).isoformat(), len(newer)))

    concept_names = {p.stem.lower() for p in (kb / "01_知识提炼").rglob("*.md")} if (kb / "01_知识提炼").exists() else set()
    missing_links = sorted(((k, v) for k, v in broken.items() if len(set(v)) >= 2), key=lambda kv: -len(set(kv[1])))
    bold_candidates = sorted(
        ((b, s) for b, s in bold_in.items()
         if len(s) >= args.min_notes and b.lower() not in concept_names and b.lower() not in names
         and bold_label[b] < 0.5 * bold_total[b]),
        key=lambda kv: -len(kv[1]),
    )[:20]

    lines = [f"# 体检事实清单（{dt.date.today().isoformat()}）", "",
             f"知识库：`{kb}`；统计笔记 {len(notes)} 篇；近期 = {since.isoformat()} 之后。", "",
             "> 本清单只列机械检查结果。矛盾、过时、假设状态需要 AI 读近期改动的笔记后判断。", ""]

    # 同一文件夹改动 ≥5 篇时合并成一行（多半是一次批量重建），其余逐篇列出
    by_dir: dict[str, list[Path]] = defaultdict(list)
    for n in changed:
        by_dir[n.parent.relative_to(kb).as_posix()].append(n)
    lines += [f"## 1. 近期改动的笔记（{len(changed)}）", ""]
    shown = 0
    for n in changed:
        d = n.parent.relative_to(kb).as_posix()
        if len(by_dir[d]) >= 5:
            if by_dir[d][0] is n:
                where = "根目录" if d == "." else f"`{d}/`"
                lines.append(f"- {dt.datetime.fromtimestamp(n.stat().st_mtime):%m-%d %H:%M} {where} 下 {len(by_dir[d])} 篇（批量改动）")
                shown += 1
            continue
        if shown < 60:
            lines.append(f"- {dt.datetime.fromtimestamp(n.stat().st_mtime):%m-%d %H:%M} `{n.relative_to(kb).as_posix()}`")
        shown += 1
    if not changed:
        lines.append("- 无")
    elif shown > 60:
        lines.append(f"- ……另有 {shown - 60} 条")

    lines += ["", f"## 2. 可能过时的项目总览（{len(stale)}）", ""]
    lines += [f"- `{n}` 最后修改 {d}，之后有 {c} 篇相关笔记被改过" for n, d, c in stale] or ["- 无"]

    lines += ["", f"## 3. 被多篇笔记引用、但还没有页面的链接（{len(missing_links)}）", "",
              "> 这类链接最可能是该建概念节点的地方。", ""]
    lines += [f"- [[{k}]] ← {len(set(v))} 篇：{'、'.join(sorted(set(v))[:4])}" for k, v in missing_links[:30]] or ["- 无"]

    lines += ["", f"## 4. 反复加粗、但没有概念节点的词（出现在 ≥{args.min_notes} 篇笔记）", "",
              "> 噪音较多，只作候选；由 AI 判断哪些值得单独成页。", ""]
    lines += [f"- **{b}** · {len(s)} 篇" for b, s in bold_candidates] or ["- 无"]

    single_broken = sorted(k for k, v in broken.items() if len(set(v)) < 2)
    lines += ["", f"## 5. 其他断链（{len(single_broken)}）", ""]
    lines += [f"- [[{k}]] ← `{broken[k][0]}`" for k in single_broken[:40]] or ["- 无"]
    if len(single_broken) > 40:
        lines.append(f"- ……另有 {len(single_broken) - 40} 条")

    lines += ["", f"## 6. 没有任何笔记链接到的 B/C 层笔记（{len(orphans)}）", ""]
    lines += [f"- `{o}`" for o in orphans[:40]] or ["- 无"]
    if len(orphans) > 40:
        lines.append(f"- ……另有 {len(orphans) - 40} 篇")

    lines += ["", f"## 7. 00_原始资料 里未经入库流程的文件（{len(unregistered)}）", "",
              "> 用户手动放进去的文件。需要时用 scan --kb 登记，并决定是否蒸馏。", ""]
    if not manifest:
        lines.append(f"- 本库没有 `.kb/manifest.csv`（不是用 kb.py 建的库），共 {len(unregistered)} 个文件未登记，略去明细。")
    else:
        lines += [f"- `{u}`" for u in unregistered[:40]] or ["- 无"]
        if len(unregistered) > 40:
            lines.append(f"- ……另有 {len(unregistered) - 40} 个")

    lines += ["", "## 8. 整洁度", "", "> 整理完会不会重新变乱，看这一节。", ""] + tidy_report(kb)

    cards = knowledge_cards(kb)
    lines += ["", "## 9. 增长（最近 4 周）", "", "> 知识库是在“生长”还是只在“变大”，看这一节。缺口明细用 `gaps` 查看。", ""]
    lines += growth_lines(kb, 4, cards, len(undistilled(kb, links)))
    idx = kb / "知识索引.md"
    newer = [c for c in cards if idx.exists() and c["mtime"] > idx.stat().st_mtime]
    if not idx.exists():
        lines.append("- 还没有知识索引：运行 `index` 生成")
    elif newer:
        lines.append(f"- 知识索引已过期：{len(newer)} 张卡片比索引新，运行 `index` 更新")

    lines += ["", "## 10. 纲要对照", "", "> 纲要由用户定。这里只列事实，改不改由用户决定，AI 在报告里提议。", ""]
    lines += outline_report(kb, notes, changed, manifest)

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)


def outline_report(kb: Path, notes: list[Path], changed: list[Path], manifest: dict) -> list[str]:
    outline = read_outline(kb / OUTLINE)
    if not outline.get("projects"):
        return ["- 还没有写好的 `知识库纲要.md`：建议按 kb-starter 的纲要访谈补一份，分项目、总览和体检都会更准"]
    out = []
    upd = outline.get("updated", "")[:10]
    if DATE_RE.fullmatch(upd):
        age = (dt.date.today() - dt.date.fromisoformat(upd)).days
        if age > 60:
            out.append(f"- 纲要 {age} 天没更新（{upd}）：项目阶段、核心问题可能已经变了")
    in_manifest = Counter(m.get("项目", "") for m in manifest.values())
    names = {p["name"] for p in outline["projects"]}
    texts = {n: n.read_text(encoding="utf-8", errors="ignore") for n in changed}
    for p in outline["projects"]:
        terms = [p["name"], *p["aliases"]]
        touched = [n for n in changed if term_hits(n.relative_to(kb).as_posix() + " " + texts[n][:3000], terms)]
        flags = []
        if not in_manifest.get(p["name"]):
            flags.append("资料里没有分到它的文件")
        if not touched:
            flags.append("近期没有相关笔记改动")
        if not (kb / f"{p['name']}_总览.md").exists():
            flags.append("还没有项目总览（运行 overview）")
        if not p["questions"]:
            flags.append("纲要里没写核心问题")
        state = "；".join(flags) if flags else "正常"
        out.append(f"- {p['kind']}「{p['name']}」：{in_manifest.get(p['name'], 0)} 个资料文件，近期相关改动 {len(touched)} 篇 · {state}")
    extra = [(k, v) for k, v in in_manifest.most_common() if k and k not in names]
    if extra:
        out.append("- 资料里有、纲要里没有的项目：" + "、".join(f"{k}（{v} 个文件）" for k, v in extra))
    return out


# ---------------------------------------------------------------- gaps：知识缺口的机械部分

# 值得蒸馏的原文类别，数字越小越优先；照片、题录、代码、待定不算
DISTILL_CATS = {"论文": 1, "会议记录": 2, "实验记录": 3, "源稿": 4, "网页剪藏": 5, "个人笔记": 6, "课程资料": 7}
CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+)$")


DISTILL_EXTS = EXT["pdf"] | EXT["doc"] | EXT["ppt"] | EXT["text"] | EXT["web"] | EXT["audio"]


def undistilled(kb: Path, links: dict) -> list[dict]:
    """00_原始资料 里没有被任何 B/C 层笔记链接、也没被它们按完整文件名提到的原文。

    只看文档、网页、文本和录音（照片、表格数据是记录的附件，不单独蒸馏）。同名副本只算一份。
    排序：类别优先级 → 被其他笔记链接的次数（常被提到却没卡片的最值得先蒸馏）→ 时间。
    """
    cited: set[str] = set()
    refs: Counter = Counter()  # 被人写的笔记链接的次数；自动生成的索引链接了所有文件，不算
    for rel, outs in links["outlinks"].items():
        if rel.startswith(("01_", "02_")):
            cited |= outs
        if rel not in ("00_原始资料/资料索引.md", "知识索引.md"):
            refs.update(outs)
    blob = "\n".join(t for n, t in links["texts"].items() if n.relative_to(kb).parts[0][:3] in {"01_", "02_"})
    manifest = {row["知识库路径"]: row for row in load_manifest(kb).values()}
    by_name: dict[str, dict] = {}
    raw = kb / "00_原始资料"
    if not raw.exists():
        return []
    for root, dirs, files in os.walk(raw):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not is_archive_dir(d))
        for fn in sorted(files):
            if fn.startswith(".") or fn == "资料索引.md" or Path(fn).suffix.lower() not in DISTILL_EXTS:
                continue
            p = Path(root) / fn
            rel = p.relative_to(kb).as_posix()
            row = manifest.get(rel)
            cat, project = (row["类别"], row["项目"]) if row else locate_in_kb(p.relative_to(kb))
            if cat not in DISTILL_CATS:
                continue
            if fn in by_name:  # 同一份文件放在几个文件夹里（例如按章节分开存放的文献）
                by_name[fn]["copies"] += 1
                by_name[fn]["cited"] |= rel in cited
                by_name[fn]["refs"] += refs[rel]
                continue
            by_name[fn] = {"path": rel, "name": fn, "cat": cat, "project": project, "mtime": p.stat().st_mtime,
                           "copies": 1, "cited": rel in cited or fn in blob, "refs": refs[rel]}
    out = [u for u in by_name.values() if not u["cited"]]
    out.sort(key=lambda x: (DISTILL_CATS[x["cat"]], -x["refs"], -x["mtime"]))
    return out


def read_gap_list(kb: Path) -> tuple[list[str], list[str]]:
    path = kb / GAP_LIST
    if not path.exists():
        return [], []
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8", errors="ignore"), flags=re.S)
    open_items, closed = [], []
    for line in text.splitlines():
        m = CHECKBOX_RE.match(line)
        if m:
            (closed if m.group(1).lower() == "x" else open_items).append(m.group(2).strip())
    return open_items, closed


def pending_hypotheses(kb: Path) -> list[str]:
    path = kb / LEDGER
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and "待测" in cells and cells[2] and not cells[0].startswith("-"):
            out.append(f"{cells[0]} {cells[2]}" + (f"（{cells[1]}）" if cells[1] else ""))
    return out


def cmd_gaps(args: argparse.Namespace) -> None:
    kb = Path(args.kb).expanduser().resolve()
    notes = md_files(kb)
    links = scan_links(kb, notes)
    und = undistilled(kb, links)
    open_items, closed = read_gap_list(kb)
    cards = knowledge_cards(kb)
    today = dt.date.today()

    lines = [f"# 缺口事实清单（{today.isoformat()}）", "",
             f"知识库：`{kb}`。", "",
             "> 只列机械检查结果。下一步按 `02_方法论库/AI提示词/缺口自检提示词.md`，结合你现在要做的事，"
             "把真正要补的写进 `" + GAP_LIST + "`。", ""]

    lines += [f"## 1. 库里有原文、但还没蒸馏的（{len(und)}）", "",
              "> 没有任何知识卡片链接到它们，也没被按完整文件名提到。只算文档、网页、文本和录音，同名副本算一份。",
              "> 排序：类别（论文 > 会议 > 实验 > …）→ 被其他笔记链接的次数 → 时间。", ""]
    if und:
        per = defaultdict(Counter)
        for u in und:
            per[u["project"] or "（未分项目）"][u["cat"]] += 1
        for proj, cnt in sorted(per.items()):
            lines.append(f"- {proj}：" + "、".join(f"{k} {v}" for k, v in sorted(cnt.items(), key=lambda kv: DISTILL_CATS[kv[0]])))
        lines += ["", "明细（前 30）：", ""]
        for u in und[:30]:
            extra = []
            if u["refs"]:
                extra.append(f"被 {u['refs']} 篇笔记链接")
            if u["copies"] > 1:
                extra.append(f"另有 {u['copies'] - 1} 份同名副本")
            lines.append(f"- {u['cat']} · {u['project'] or '未分项目'} · `{u['path']}`" + (f"（{'，'.join(extra)}）" if extra else ""))
        if len(und) > 30:
            lines.append(f"- ……另有 {len(und) - 30} 份")
    else:
        lines.append("- 无")

    lines += ["", f"## 2. 缺口清单里待补的（{len(open_items)}，已补 {len(closed)}）", ""]
    for item in open_items:
        m = DATE_RE.search(item)
        age = f"（已挂 {(today - dt.date.fromisoformat(m.group(0))).days} 天）" if m else ""
        lines.append(f"- [ ] {item}{age}")
    if not open_items:
        lines.append("- 无")

    missing_links = sorted(((k, v) for k, v in links["broken"].items() if len(set(v)) >= 2), key=lambda kv: -len(set(kv[1])))
    lines += ["", f"## 3. 被提到、但还没写的知识（{len(missing_links)}）", "",
              "> 被 2 篇以上笔记链接、却没有页面的名字。", ""]
    lines += [f"- [[{k}]] ← {len(set(v))} 篇" for k, v in missing_links[:20]] or ["- 无"]

    def has_source(c: dict) -> bool:
        outs = links["outlinks"].get(c["path"], set())
        return bool(c["source"]) or any(o.startswith("00_") or o.startswith("01_知识提炼/文献卡片") for o in outs)
    no_src = [c for c in cards if c["type"] in ("概念", "决策", "问题", "教训", "文献") and not has_source(c)]
    lines += ["", f"## 4. 没写来源的知识卡片（{len(no_src)}）", "",
              "> 头部没有「来源」，正文也没链接到原文或文献卡片。这些结论以后很难核对。", ""]
    lines += [f"- {c['type']} · `{c['path']}`" for c in no_src[:30]] or ["- 无"]

    hyps = pending_hypotheses(kb)
    lines += ["", f"## 5. 还在等证据的假设（{len(hyps)}）", ""]
    lines += [f"- {h}" for h in hyps] or ["- 无"]

    outline = read_outline(kb / OUTLINE)
    qs = [(p["name"], q) for p in outline.get("projects", []) for q in p["questions"]]
    lines += ["", f"## 6. 纲要里的核心问题（{len(qs)}）", "",
              "> 找缺口时以这些问题为准：哪个问题现在还答不上来，缺的是什么。", ""]
    lines += [f"- {name}：{q}" for name, q in qs] or ["- 纲要里还没写核心问题（或还没有纲要）"]

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)


# ---------------------------------------------------------------- stats：增长统计

LOG_TAG_RE = re.compile(r"^-\s*\**\s*\[(" + "|".join(LOG_TAGS) + r")\]")
# 没打标签的旧日志按关键词猜类型（顺序即优先级）
TAG_GUESS = [("蒸馏", r"精读|蒸馏|卡片"), ("回流", r"回流|写回|概念节点"), ("缺口", r"缺口"), ("体检", r"体检|断链"),
             ("入库", r"入库|归档|登记"), ("建库", r"建库"), ("决策", r"决定|确认|改为|定为|暂定|暂舍"),
             ("整理", r"整理|清理|移入|改名|重命名")]


def parse_changelog(kb: Path) -> list[tuple[dt.date, str, bool]]:
    """返回 (日期, 类型, 是否靠关键词推断)。"""
    log = kb / "知识库变更日志.md"
    if not log.exists():
        return []
    text = re.sub(r"<!--.*?-->", "", log.read_text(encoding="utf-8", errors="ignore"), flags=re.S)
    entries, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^##\s*(\d{4}-\d{2}-\d{2})", line)
        if m:
            cur = dt.date.fromisoformat(m.group(1))
            continue
        if cur is None or not line.startswith("- "):
            continue
        t = LOG_TAG_RE.match(line)
        if t:
            entries.append((cur, t.group(1), False))
            continue
        for tag, pat in TAG_GUESS:
            if re.search(pat, line):
                entries.append((cur, tag, True))
                break
        else:
            entries.append((cur, "其他", True))
    return entries


def growth_lines(kb: Path, weeks: int, cards: list[dict], n_undistilled: int) -> list[str]:
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    starts = [this_monday - dt.timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    entries = parse_changelog(kb)
    cols = ["入库", "蒸馏", "回流", "缺口", "体检", "决策"]
    lines = ["| 周 | " + " | ".join(cols) + " | 其他 | 新卡片 |", "|---|" + "---|" * (len(cols) + 2)]
    recent_cards = recent_flow = recent_in = 0
    for i, s in enumerate(starts):
        e = s + dt.timedelta(days=6)
        c = Counter(tag for d, tag, _ in entries if s <= d <= e)
        other = sum(v for k, v in c.items() if k not in cols)
        # 台账、清单、提示词是工具文件，不算“长出来的知识”
        new_cards = sum(1 for k in cards if k["type"] not in ("研究问题", "提示词")
                        and s.isoformat() <= k["created"] <= e.isoformat())
        y, w, _ = s.isocalendar()
        lines.append(f"| {y}-W{w:02d}（{s:%m-%d}–{e:%m-%d}） | " + " | ".join(str(c[k]) for k in cols)
                     + f" | {other} | {new_cards} |")
        if i >= len(starts) - 2:
            recent_cards += new_cards
            recent_flow += c["蒸馏"] + c["回流"]
            recent_in += c["入库"]
    guessed = sum(1 for _, _, g in entries if g)
    types = Counter(k["type"] for k in cards)
    open_items, closed = read_gap_list(kb)
    lines += ["",
              "- 现有知识卡片：" + ("，".join(f"{t} {types[t]}" for t in TYPE_ORDER if types[t]) or "0"),
              f"- 还没蒸馏的原文：{n_undistilled} 份；缺口清单：待补 {len(open_items)}，已补 {len(closed)}"]
    if guessed:
        lines.append(f"- 日志里有 {guessed} 条没打类型标签，按关键词推断了类型（不一定准）")
    if recent_cards == 0 and recent_flow == 0:
        verdict = "最近两周没有新增知识卡片，也没有蒸馏或回流记录：知识库只在变大，没有在生长。"
    elif recent_cards == 0:
        verdict = f"最近两周有 {recent_flow} 次蒸馏/回流，但没有新卡片：在更新旧知识，没有长出新知识。"
    else:
        verdict = f"最近两周新增 {recent_cards} 张卡片、蒸馏/回流 {recent_flow} 次、入库 {recent_in} 次。"
    lines.append(f"- **判断**：{verdict}")
    return lines


def cmd_stats(args: argparse.Namespace) -> None:
    kb = Path(args.kb).expanduser().resolve()
    links = scan_links(kb, md_files(kb))
    lines = [f"# 增长统计（最近 {args.weeks} 周，{dt.date.today().isoformat()}）", ""]
    lines += growth_lines(kb, args.weeks, knowledge_cards(kb), len(undistilled(kb, links)))
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)


# ---------------------------------------------------------------- overview：按纲要生成项目总览草稿

# 这些类别里最新的一份，作为“现行文件候选”
CURRENT_CATS = ["实验记录", "会议记录", "论文草稿", "报告", "汇报PPT", "源稿"]
# “最近的记录”只看能反映进展的类别；照片、下载的文献、剪藏、代码看不出做到了哪一步
PROGRESS_CATS = set(CURRENT_CATS) | {"个人笔记"}


def render_overview(p: dict, files: list[dict], today: dt.date) -> str:
    f = p["fields"]
    tbd = "待确认（纲要里还没写）"
    link = lambda m: f"[[{m['知识库路径']}|{Path(m['知识库路径']).name}]]"
    cell = lambda m: link(m).replace("|", "\\|")  # 表格里的竖线要转义，否则会把表格撑坏
    day = lambda m: dt.date.fromtimestamp(m["_mtime"]).isoformat() if m["_mtime"] else "日期不明"
    current = [m for m in files if "/历史版本" not in m["知识库路径"]]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for m in sorted(current, key=lambda m: -m["_mtime"]):
        by_cat[m["类别"]].append(m)

    aliases = "".join(f"\n  - {a}" for a in [p["name"], *p["aliases"]])
    lines = [
        "---", f"title: {p['name']} · 总览", f"aliases:{aliases}", "tags:", f"  - 项目/{p['name']}", "  - 类型/项目总览",
        f"状态: {f.get('现在在哪一步') or '待确认'}", f"创建: {today}", f"更新: {today}", "---", "",
        f"# {p['name']} · 总览", "",
        "> [!info] 这份总览谁来写",
        "> 标【你定】的段落来自 [[知识库纲要]]，只有你能改；要改就先改纲要，再同步到这里。",
        "> 标【AI 整理】的段落由 AI 从资料里整理，你确认后去掉“待确认”。",
        f"> 总览只写“现在”。历次进展写到 [[{p['name']}_进展时间线]]。", "",
        "## 一句话目标【你定】", "", f.get("一句话目标") or tbd, "",
        "## 核心问题【你定】", "",
    ]
    lines += [f"{i}. {q}" for i, q in enumerate(p["questions"], 1)] or [f"- {tbd}"]
    lines += ["", "每个核心问题在 [[假设台账]] 里有对应条目。", "",
              f"## 当前状态（{today}）", "",
              f"**你说的**【你定】：现在 {f.get('现在在哪一步') or '待确认'}；下一步 {f.get('下一步') or '待确认'}。", "",
              "**资料显示的进展**【AI 整理】：待 AI 读下面的最近记录后补写。", "",
              "最近的记录（实验、会议、汇报、报告、草稿和笔记，按文件时间倒序）：", ""]
    recent = sorted((m for m in current if m["类别"] in PROGRESS_CATS), key=lambda m: -m["_mtime"])[:5]
    lines += [f"- {day(m)} · {m['类别']} · {link(m)}" for m in recent] or ["- 资料里还没有这类记录"]
    lines += ["", "## 现行执行文件【AI 整理 · 待你确认】", "",
              "> 这张表就是“现在该看的全部文件”。下面是每类最新的一份，AI 读过后改成真正的步骤和口径；被替代的文件移到 `历史版本/`。", "",
              "| 步骤/用途 | 现行文件 | 当前口径 |", "|---|---|---|"]
    cands = [(c, by_cat[c][0]) for c in CURRENT_CATS if by_cat.get(c)]
    lines += [f"| {c}（候选） | {cell(m)} | 待 AI 补写 |" for c, m in cands] or ["| | | |"]
    lines += ["", "## 待确认", "", "- 现行执行文件：上表是按时间挑的候选，需要你确认。"]
    if not files:
        lines.append("- 纲要里有这个项目，但资料里还没有分到它的文件。")
    for key in ("一句话目标", "现在在哪一步", "下一步"):
        if not f.get(key):
            lines.append(f"- 纲要里还没写「{key}」。")
    if not p["questions"]:
        lines.append("- 纲要里还没写「核心问题」。")
    lines += ["", "## 资料概况【AI 整理】", "", "| 类别 | 文件数 | 最近一份 |", "|---|---|---|"]
    lines += [f"| {c} | {len(ms)} | {cell(ms[0])}（{day(ms[0])}） |"
              for c, ms in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))] or ["| | 0 | |"]
    old = len(files) - len(current)
    if old:
        lines.append(f"\n另有 {old} 份旧版本在 `历史版本/` 里。")
    lines += ["", "## 背景与依据", "", "| 类别 | 笔记 |", "|---|---|",
              "| 核心概念 | |", "| 文献 | |", "| 决策 | |", "| 原始记录 | [[资料索引]] |", "| 成品 | |", ""]
    return "\n".join(lines)


def cmd_overview(args: argparse.Namespace) -> None:
    kb = Path(args.kb).expanduser().resolve()
    outline = read_outline(kb / OUTLINE)
    projects = outline.get("projects", [])
    if args.project:
        projects = [p for p in projects if p["name"] == args.project]
    if not projects:
        print(f"纲要里没有{'项目「' + args.project + '」' if args.project else '任何项目'}：先按纲要访谈写好 {OUTLINE}。",
              file=sys.stderr)
        sys.exit(2)
    manifest = load_manifest(kb)
    today = dt.date.today()
    for p in projects:
        files = [dict(m) for m in manifest.values() if m.get("项目") == p["name"]]
        for m in files:
            st = stat_quiet(kb / m["知识库路径"])
            m["_mtime"] = st.st_mtime if st else 0
        target = kb / f"{p['name']}_总览.md"
        if target.exists() and not args.force:
            target = kb / "99_工作区/总览草稿" / f"{p['name']}_总览-草稿-{today:%Y%m%d}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_overview(p, files, today), encoding="utf-8")
        where = "（总览已存在，草稿另存，请合并）" if target.parent.name == "总览草稿" else ""
        print(f"{p['name']}：{len(files)} 个文件 → {target.relative_to(kb)}{where}")


# ---------------------------------------------------------------- pack

def body_of(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def first_paragraph(text: str, limit: int = 160) -> str:
    for block in re.split(r"\n\s*\n", body_of(text)):
        b = block.strip()
        if not b or b.startswith("#"):
            continue
        b = re.sub(r"^>\s*(\[!.*?\]\s*)?", "", b, flags=re.M)
        b = re.sub(r"\s+", " ", b).strip()
        if b:
            return b[:limit] + ("…" if len(b) > limit else "")
    return ""


def cmd_pack(args: argparse.Namespace) -> None:
    kb = Path(args.kb).expanduser().resolve()
    parts = [
        "# 我的知识库上下文包",
        "",
        "> 下面是我个人知识库的导航、项目现状和概念索引，由本地 AI 自动生成。"
        "请把它当作回答的背景：优先依据这里的信息；这里没有的，请直接说明需要我提供哪份原文，不要自行补全数据或结论。",
        "",
    ]
    outline = kb / OUTLINE
    if read_outline(outline).get("projects"):  # 主线放最前面，网页版 AI 先知道我在做什么
        parts += ["---", "## 知识库纲要", "", body_of(re.sub(r"<!--.*?-->", "", outline.read_text(encoding="utf-8"), flags=re.S))]
    nav = kb / "知识库导航.md"
    if nav.exists():
        parts += ["---", "## 知识库导航", "", body_of(nav.read_text(encoding="utf-8"))]
    overviews = [kb / f"{args.project}_总览.md"] if args.project else sorted(kb.glob("*_总览.md"))
    for ov in overviews:
        if ov.exists():
            parts += ["---", f"## {ov.stem}", "", body_of(ov.read_text(encoding="utf-8"))]
        else:
            print(f"找不到 {ov.name}", file=sys.stderr)
    ledger = kb / LEDGER
    if ledger.exists():
        parts += ["---", "## 假设台账", "", body_of(ledger.read_text(encoding="utf-8"))]
    index = kb / "知识索引.md"
    cards = knowledge_cards(kb)
    if index.exists() and any(c["mtime"] > index.stat().st_mtime for c in cards):
        write_index(kb)  # 索引比卡片旧时先更新，网页版 AI 拿到的才是最新的
        print("知识索引已过期，已重新生成")
    concepts = sorted((kb / "01_知识提炼").rglob("*.md")) if (kb / "01_知识提炼").exists() else []
    concepts = [c for c in concepts if "概念" in c.parent.name or "领域地图" in c.parent.name]
    if index.exists():
        # 知识索引已经是“知道什么”的清单；指定项目时只留标题和属于该项目的条目
        body = body_of(index.read_text(encoding="utf-8"))
        if args.project:
            body = "\n".join(ln for ln in body.splitlines() if not ln.startswith("- ") or args.project in ln)
        parts += ["---", "## 知识索引", "", body]
    elif concepts:
        parts += ["---", "## 概念节点索引", ""]
        for c in concepts:
            text = c.read_text(encoding="utf-8", errors="ignore")
            if args.project and args.project not in text:
                continue
            parts.append(f"- **{c.stem}**：{first_paragraph(text)}")
    out = Path(args.out).expanduser() if args.out else kb / "99_工作区/粘贴包" / (
        f"{dt.date.today():%Y%m%d}-粘贴包{'-' + args.project if args.project else ''}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(parts) + "\n"
    out.write_text(content, encoding="utf-8")
    print(f"已生成 {out}（{len(content)} 字）")
    if len(content) > 60000:
        print("提示：内容偏长，网页版 AI 可能截断；可加 --project 只打包一个项目。")


# ---------------------------------------------------------------- 入口

def main() -> None:
    p = argparse.ArgumentParser(description="kb-starter：零散资料 → 分层知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描资料并生成分类方案表")
    s.add_argument("src", help="零散资料所在文件夹")
    s.add_argument("--out", required=True, help="方案表输出目录（建议放在知识库外的临时目录）")
    s.add_argument("--kb", help="已有知识库路径；给出时会跳过已入库的文件，并读取其中的知识库纲要")
    s.add_argument("--outline", help="知识库纲要路径（默认 <知识库>/知识库纲要.md）；按其中的项目和关键词分项目")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("decide", help="把 AI 的判断写回方案表")
    d.add_argument("plan", help="plan.csv 路径")
    d.add_argument("decisions", help="decisions.json 路径")
    d.set_defaults(func=cmd_decide)

    a = sub.add_parser("apply", help="按方案表复制文件进知识库")
    a.add_argument("plan", help="plan.csv 路径")
    a.add_argument("--kb", required=True, help="知识库路径（不存在会新建）")
    a.add_argument("--name", help="知识库名称，用于入口文件标题")
    a.add_argument("--dry-run", action="store_true", help="只预演，不写文件")
    a.add_argument("--pending-to-unsorted", action="store_true", help="把仍是待AI的条目放进 09_待定")
    a.set_defaults(func=cmd_apply)

    v = sub.add_parser("verify", help="对账：原件完好、一个不漏、副本一致")
    v.add_argument("plan", help="plan.csv 路径（或 .kb/plans/ 里存档的方案表）")
    v.add_argument("--kb", required=True, help="知识库路径")
    v.add_argument("--full", action="store_true", help="重新计算原件哈希（慢，但最彻底）")
    v.set_defaults(func=cmd_verify)

    o = sub.add_parser("overview", help="按纲要生成项目总览草稿（【你定】照抄纲要，【AI 整理】从资料清单填）")
    o.add_argument("kb", help="知识库路径")
    o.add_argument("--project", help="只生成某个项目")
    o.add_argument("--force", action="store_true", help="覆盖已有总览（默认已有时草稿另存到 99_工作区/总览草稿/）")
    o.set_defaults(func=cmd_overview)

    x = sub.add_parser("index", help="生成知识索引（AI 开工先读的记忆入口）")
    x.add_argument("kb", help="知识库路径")
    x.add_argument("--out", help="写到别处而不是知识库根目录的 知识索引.md")
    x.add_argument("--max-chars", type=int, default=12000, help="超过这个字数改用精简格式（默认 12000）")
    x.set_defaults(func=cmd_index)

    g = sub.add_parser("gaps", help="缺口的机械检查：未蒸馏原文、待补缺口、没写来源的卡片、待测假设")
    g.add_argument("kb", help="知识库路径")
    g.add_argument("--out", help="写入文件而不是打印")
    g.set_defaults(func=cmd_gaps)

    t = sub.add_parser("stats", help="增长统计：按周统计日志标签和新增卡片")
    t.add_argument("kb", help="知识库路径")
    t.add_argument("--weeks", type=int, default=4, help="统计最近几周（默认 4）")
    t.add_argument("--out", help="写入文件而不是打印")
    t.set_defaults(func=cmd_stats)

    l = sub.add_parser("lint", help="体检的机械检查")
    l.add_argument("kb", help="知识库路径")
    l.add_argument("--days", type=int, default=7, help="近期 = 最近几天（默认 7）")
    l.add_argument("--since", help="近期起点 YYYY-MM-DD，优先于 --days")
    l.add_argument("--min-notes", type=int, default=3, help="候选概念至少出现在几篇笔记（默认 3）")
    l.add_argument("--out", help="写入文件而不是打印")
    l.set_defaults(func=cmd_lint)

    k = sub.add_parser("pack", help="生成给网页版 AI 的粘贴包")
    k.add_argument("kb", help="知识库路径")
    k.add_argument("--project", help="只打包某个项目")
    k.add_argument("--out", help="输出文件路径")
    k.set_defaults(func=cmd_pack)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
