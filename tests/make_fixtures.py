#!/usr/bin/env python3
"""生成一个约 40 个文件的“零散资料”测试文件夹，用来跑通 kb.py 的建库流程。

用法：python3 make_fixtures.py <输出目录>
需要 Pillow 生成带 EXIF 的照片；macOS 上会顺带用 sips 生成一张 HEIC。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def pdf(path: Path, lines: list[str]) -> None:
    """手写一个最小 PDF，正文不压缩（只放 ASCII）。"""
    text_ops = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
    for ln in lines:
        safe = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        text_ops.append(f"({safe}) Tj T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def core_xml(title: str, creator: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator></cp:coreProperties>"
    )


def docx(path: Path, paragraphs: list[str], title: str = "", creator: str = "") -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
        z.writestr("docProps/core.xml", core_xml(title, creator))


def pptx(path: Path, slides: list[str], title: str = "") -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for i, s in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:a="a" xmlns:p="p"><a:p><a:r><a:t>{s}</a:t></a:r></a:p></p:sld>')
        z.writestr("docProps/core.xml", core_xml(title, ""))


def xlsx(path: Path, sheets: list[str], strings: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", "<workbook><sheets>" + "".join(f'<sheet name="{s}" sheetId="{i}"/>' for i, s in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/sharedStrings.xml", "<sst>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")


def photo(path: Path, make: str | None, when: str | None, color=(180, 140, 90)) -> None:
    from PIL import Image

    img = Image.new("RGB", (64, 48), color)
    exif = Image.Exif()
    if make:
        exif[0x010F] = make
    if when:
        exif[0x0132] = when
    img.save(path, exif=exif.tobytes() if (make or when) else b"")


def main() -> None:
    root = Path(sys.argv[1]).expanduser()
    if root.exists():
        shutil.rmtree(root)
    a, r, p, c, n = (root / "电解液", root / "综述", root / "papers", root / "高分子物理", root / "notes")
    for d in (a, r, p, c, n):
        d.mkdir(parents=True)

    # ---- 电解液项目
    pdf(a / "Li2024_electrolyte_additive.pdf", ["Fluorinated additive for high-voltage electrolytes",
                                          "Nature Energy 2024", "https://doi.org/10.1038/s41893-024-01234-5"])
    shutil.copy2(a / "Li2024_electrolyte_additive.pdf", a / "Li2024_electrolyte_additive (1).pdf")
    pdf(a / "scan_0012.pdf", ["Scanned page", "handwritten notes about cycling at 0.5 C"])
    docx(a / "实验记录_配液_20261013.docx", ["1 M LiPF6 溶于 EC/DMC，25 ℃ 搅拌", "加入 2 wt% 添加剂后澄清"], "配液记录", "张同学")
    xlsx(a / "原始数据_循环容量.xlsx", ["B45", "B46", "B48"], ["样品", "容量/mAh", "148", "152"])
    (a / "XRD_sample1.csv").write_text("2theta,intensity\n10,120\n20,560\n", encoding="utf-8")
    (a / "metadata_update.csv").write_text("key,value\nbatch,7\n", encoding="utf-8")
    photo(a / "IMG_4521.jpg", "Apple", "2026:10:13 14:20:00")
    pptx(a / "组会汇报_20261010.pptx", ["添加剂筛选 第 1 轮结果", "张同学 2026-10-10"])
    pptx(a / "slides_final.pptx", ["电解液课题进展", "张同学 · 刘老师课题组 · 2026-10"])
    docx(a / "2026-10-07讨论纪要.docx", ["刘老师：截止电压改为 4.4 V", "下周先做四组对照"], "讨论纪要", "张同学")
    (a / "录音_组会1015.m4a").write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00" + b"\x00" * 2048)

    # ---- 综述项目
    pdf(r / "s41586-023-06000.pdf", ["Solid-state electrolytes", "Nature 2023", "doi:10.1038/s41586-023-06000-1"])
    pdf(r / "arxiv_battery_ml.pdf", ["Machine learning for battery materials", "arXiv:2403.12345v2 [cs.LG]"])
    docx(r / "论文初稿_v3.docx", ["Solid-state electrolytes for lithium metal batteries: a review", "1. Introduction"], "综述初稿", "张同学")
    (r / "~$论文初稿_v3.docx").write_bytes(b"temp-lock-file")
    (r / "thesis.tex").write_text("\\documentclass{article}\n\\begin{document}\nSolid-state electrolyte review\n\\end{document}\n", encoding="utf-8")
    (r / "thesis.aux").write_text("\\relax\n", encoding="utf-8")
    (r / "refs.bib").write_text("@article{li2024,\n  title={Fluorinated additive},\n  doi={10.1038/s41893-024-01234-5}\n}\n", encoding="utf-8")

    # ---- 通用文件夹名：不应被当成项目
    pdf(p / "电解质综述_中文期刊.pdf", ["Electrolyte review", "Energy Storage Science and Technology 2021, 10(3): 1-12"])
    (n / "SEI机理笔记.md").write_text("# SEI 机理\n\n添加剂优先还原 → 形成富 LiF 界面。\n", encoding="utf-8")

    # ---- 课程
    pdf(c / "第3讲_高分子物理.pdf", ["Lecture 3: Chain conformation", "Polymer Physics, Fall 2026"])
    pptx(c / "高分子化学_第5章课件.pptx", ["第五章 自由基聚合", "主讲：李老师"])
    docx(c / "homework2.docx", ["Problem 1: compute the radius of gyration"], "Homework 2", "张同学")

    # ---- 根目录散落文件
    docx(root / "开题报告_v2.docx", ["高电压电解液添加剂的筛选及其界面机制研究", "一、选题依据"], "开题报告", "张同学")
    docx(root / "申请书_国自然.docx", ["项目名称：高电压电解液添加剂", "立项依据"], "申请书", "张同学")
    docx(root / "导师修改意见.docx", ["张同学：", "第二节需要补充添加剂浓度对照，第四节图 3 重画。", "——刘老师"], "", "刘老师")
    (root / "meeting_transcript.txt").write_text("刘老师：这周先把截止电压定下来。\n张：好的。\n", encoding="utf-8")
    (root / "seminar_notes.md").write_text("# 讲座随记\n\n关于绿色化学的几点想法。\n", encoding="utf-8")
    (root / "随手想法.md").write_text("- 试试 FEC 添加剂？\n- 问一下 XPS 预约\n", encoding="utf-8")
    (root / "网页剪藏_电解液综述.md").write_text("---\nsource: https://example.org/electrolyte-review\n---\n# 电解液综述摘录\n", encoding="utf-8")
    (root / "How batteries work - Wikipedia.html").write_text("<html><head><title>Battery - Wikipedia</title></head><body>Battery</body></html>", encoding="utf-8")
    (root / "analysis.py").write_text("import csv\nprint('capacity mean')\n", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (root / "empty.txt").write_bytes(b"")
    photo(root / "截屏2026-10-12 下午3.21.png", None, None, (240, 240, 240))
    photo(root / "身份证扫描.jpg", "HUAWEI", "2025:03:01 09:00:00", (200, 200, 220))
    photo(root / "untitled.png", None, None, (10, 10, 10))
    with zipfile.ZipFile(root / "old_backup.zip", "w") as z:
        z.writestr("readme.txt", "old stuff")

    if shutil.which("sips"):
        subprocess.run(["sips", "-s", "format", "heic", str(a / "IMG_4521.jpg"), "--out", str(a / "lab_photo.heic")],
                       capture_output=True)

    # ---- “不漏”：隐藏文件夹、iCloud 占位、没有读权限、符号链接文件夹
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (r / ".论文终稿.docx.icloud").write_bytes(b"bplist00\x00placeholder")
    locked = a / "locked_data.csv"
    locked.write_text("sample,capacity\nB49,150\n", encoding="utf-8")
    locked.chmod(0)
    (root / "link_to_papers").symlink_to(p, target_is_directory=True)

    # ---- “不乱”：多个版本、同名文件、看不出内容的文件名
    docx(r / "论文初稿_v1.docx", ["Solid-state electrolytes: first draft"], "综述初稿", "张同学")
    docx(r / "论文初稿_v2.docx", ["Solid-state electrolytes: second draft"], "综述初稿", "张同学")
    docx(root / "申请书_国自然_修改版.docx", ["项目名称：高电压电解液添加剂（修改后）", "立项依据"], "申请书", "张同学")
    pdf(c / "第4讲_高分子物理.pdf", ["Lecture 4: Rubber elasticity", "Polymer Physics, Fall 2026"])
    for folder, color in (("照片1013", (90, 60, 30)), ("照片1015", (30, 60, 90))):
        (a / folder).mkdir()
        photo(a / folder / "1.jpg", "Apple", "2026:10:13 15:00:00" if folder.endswith("13") else "2026:10:15 15:00:00", color)

    files = [f for f in root.rglob("*") if f.is_file()]
    print(f"生成 {len(files)} 个文件于 {root}")


if __name__ == "__main__":
    main()
