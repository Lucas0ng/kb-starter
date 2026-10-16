#!/usr/bin/env python3
"""端到端测试：生成测试资料 → 建库 → 重复运行 → 增量入库 → 登记 → 体检 → 粘贴包。

用法：python3 e2e.py <临时工作目录>
每一步带断言，最后打印通过/失败汇总；退出码非 0 表示有失败。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB_PY = HERE.parent / "skills" / "kb-starter" / "scripts" / "kb.py"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f"：{detail}" if detail else ""))


def kb(*args: str, ok_codes=(0,)) -> str:
    p = subprocess.run([sys.executable, str(KB_PY), *args], capture_output=True, text=True)
    if p.returncode not in ok_codes:
        print(p.stdout, p.stderr)
        raise SystemExit(f"kb.py {args[0]} 退出码 {p.returncode}")
    return p.stdout + p.stderr


def plan(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["文件名"]: r for r in csv.DictReader(f)}


def digest_tree(root: Path) -> dict[str, str]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            try:
                out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            except PermissionError:
                out[str(p.relative_to(root))] = "无读权限"
    return out


def by_path(path: Path, src: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {str(Path(r["原路径"]).relative_to(src)): r for r in csv.DictReader(f)}


def main() -> None:
    base = Path(sys.argv[1]).expanduser().resolve()
    src, kbdir = base / "src", base / "kb"
    for d in (src, kbdir, base / "w1", base / "w2", base / "w3", base / "w4", base / "new"):
        shutil.rmtree(d, ignore_errors=True)
    subprocess.run([sys.executable, str(HERE / "make_fixtures.py"), str(src)], check=True, capture_output=True)
    before = digest_tree(src)
    print(f"测试资料：{len(before)} 个文件\n")

    # ---------------------------------------------------------- 1 扫描
    print("1. 扫描与规则分类")
    out = kb("scan", str(src), "--out", str(base / "w1"))
    print("  " + out.strip().replace("\n", "\n  "))
    p = plan(base / "w1" / "plan.csv")
    expect = {
        "Li2024_electrolyte_additive.pdf": ("复制", "论文", "电解液"),
        "Li2024_electrolyte_additive (1).pdf": ("跳过", "", None),
        "s41586-023-06000.pdf": ("复制", "论文", "综述"),
        "arxiv_battery_ml.pdf": ("复制", "论文", "综述"),
        "电解质综述_中文期刊.pdf": ("复制", "论文", ""),
        "refs.bib": ("复制", "题录", "综述"),
        "论文初稿_v3.docx": ("复制", "论文草稿", "综述"),
        "thesis.tex": ("复制", "论文草稿", "综述"),
        "thesis.aux": ("跳过", "", None),
        "~$论文初稿_v3.docx": ("跳过", "", None),
        "开题报告_v2.docx": ("复制", "报告", ""),
        "组会汇报_20261010.pptx": ("复制", "汇报PPT", "电解液"),
        "2026-10-07讨论纪要.docx": ("复制", "会议记录", "电解液"),
        "录音_组会1015.m4a": ("复制", "会议记录", "电解液"),
        "实验记录_配液_20261013.docx": ("复制", "实验记录", "电解液"),
        "原始数据_循环容量.xlsx": ("复制", "实验记录", "电解液"),
        "XRD_sample1.csv": ("复制", "实验记录", "电解液"),
        "IMG_4521.jpg": ("复制", "照片", "电解液"),
        "第3讲_高分子物理.pdf": ("复制", "课程资料", "高分子物理"),
        "高分子化学_第5章课件.pptx": ("复制", "课程资料", "高分子物理"),
        "homework2.docx": ("复制", "课程资料", "高分子物理"),
        "网页剪藏_电解液综述.md": ("复制", "网页剪藏", ""),
        "How batteries work - Wikipedia.html": ("复制", "网页剪藏", ""),
        "seminar_notes.md": ("复制", "个人笔记", ""),
        "SEI机理笔记.md": ("复制", "个人笔记", ""),
        "analysis.py": ("复制", "代码", ""),
        "old_backup.zip": ("复制", "待定", ""),
        ".DS_Store": ("跳过", "", None),
        "empty.txt": ("跳过", "", None),
        "身份证扫描.jpg": ("待AI", None, None),
        "导师修改意见.docx": ("待AI", None, None),
        "scan_0012.pdf": ("待AI", None, None),
        "截屏2026-10-12 下午3.21.png": ("待AI", None, None),
        "untitled.png": ("待AI", None, None),
    }
    wrong = []
    for name, (st, cat, proj) in expect.items():
        r = p.get(name)
        if r is None or r["状态"] != st or (cat is not None and r["类别"] != cat) or (proj is not None and r["项目"] != proj):
            wrong.append(f"{name} → {r and (r['状态'], r['类别'], r['项目'])}")
    check(f"{len(expect)} 个有预期的文件分类正确", not wrong, "；".join(wrong))

    print("\n1b. 一个不漏：特殊文件都有记录")
    bp = by_path(base / "w1" / "plan.csv", src)
    git = bp.get(".git", {})
    check("隐藏文件夹记一行并写明文件数", git.get("状态") == "跳过" and "含 2 个文件" in git.get("依据", ""), git.get("依据", "缺失"))
    link = bp.get("link_to_papers", {})
    check("符号链接文件夹记一行", link.get("状态") == "跳过" and "符号链接" in link.get("依据", ""), link.get("依据", "缺失"))
    ic = bp.get("综述/.论文终稿.docx.icloud", {})
    check("iCloud 占位文件标为无法读取，显示真实文件名", ic.get("状态") == "无法读取" and ic.get("文件名") == "论文终稿.docx", str(ic.get("依据")))
    lk = bp.get("电解液/locked_data.csv", {})
    check("没有读权限的文件标为无法读取", lk.get("状态") == "无法读取", str(lk.get("依据")))
    meta = json.loads((base / "w1" / "scan_meta.json").read_text(encoding="utf-8"))
    on_disk = sum(1 for p in src.rglob("*") if p.is_file())
    check("方案覆盖磁盘上的每个文件", meta["文件数"] == on_disk, f"方案 {meta['文件数']} / 磁盘 {on_disk}")

    print("\n1c. 不乱：版本、同名、无意义文件名")
    sub = lambda k: bp[k].get("子目录", "")
    check("v1、v2 进历史版本，v3 留在原处",
          sub("综述/论文初稿_v1.docx") == sub("综述/论文初稿_v2.docx") == "历史版本" and sub("综述/论文初稿_v3.docx") == "")
    check("“修改版”被认作新版，原版进历史版本",
          sub("申请书_国自然.docx") == "历史版本" and sub("申请书_国自然_修改版.docx") == "")
    check("第3讲、第4讲不被误当成版本", sub("高分子物理/第3讲_高分子物理.pdf") == sub("高分子物理/第4讲_高分子物理.pdf") == "")
    check("两个 1.jpg 各自保留来源文件夹",
          sub("电解液/照片1013/1.jpg") == "照片1013" and sub("电解液/照片1015/1.jpg") == "照片1015")
    check("无意义文件名被标出", all("建议起入库名" in bp[k]["依据"] for k in ("电解液/IMG_4521.jpg", "电解液/照片1013/1.jpg"))
          and "建议起入库名" not in bp["电解液/原始数据_循环容量.xlsx"]["依据"])
    rule_done = sum(1 for r in p.values() if r["状态"] != "待AI")
    check("规则直接处理的比例", rule_done / len(p) >= 0.8, f"{rule_done}/{len(p)}")

    # ---------------------------------------------------------- 2 AI 判断
    print("\n2. AI 判断待定条目并归组项目")
    decisions_by_name = {
        "untitled.png": {"状态": "跳过", "依据": "纯黑空白图，无内容"},
        "截屏2026-10-12 下午3.21.png": {"类别": "待定", "依据": "空白截图，看不出用途"},
        "身份证扫描.jpg": {"状态": "跳过", "依据": "隐私文件，用户确认不入库"},
        "导师修改意见.docx": {"类别": "会议记录", "项目": "电解液", "依据": "刘老师的修改意见"},
        "scan_0012.pdf": {"类别": "实验记录", "项目": "电解液", "依据": "手写循环条件扫描页"},
        "开题报告_v2.docx": {"项目": "电解液", "依据": "开题题目属于电解液课题"},
        "申请书_国自然.docx": {"项目": "电解液"},
        "申请书_国自然_修改版.docx": {"项目": "电解液"},
        "论文初稿_v2.docx": {"子目录": "", "依据": "v2 是投给另一期刊的分支稿，不算旧版"},
        "IMG_4521.jpg": {"入库名": "20261013_扣电组装"},
        "meeting_transcript.txt": {"项目": "电解液"},
        "随手想法.md": {"项目": "电解液"},
        "网页剪藏_电解液综述.md": {"项目": "电解液"},
        "analysis.py": {"项目": "电解液"},
        "SEI机理笔记.md": {"项目": "电解液"},
        "电解质综述_中文期刊.pdf": {"项目": "综述"},
    }
    decisions = {"rename_projects": {"综述": "固态电池综述"},
                 "items": {p[n]["id"]: v for n, v in decisions_by_name.items()}}
    (base / "w1" / "decisions.json").write_text(json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8")
    kb("decide", str(base / "w1" / "plan.csv"), str(base / "w1" / "decisions.json"))
    p = plan(base / "w1" / "plan.csv")
    check("待AI 清零", not any(r["状态"] == "待AI" for r in p.values()))
    check("项目改名生效", p["refs.bib"]["目标目录"] == "00_原始资料/01_论文原文/固态电池综述", p["refs.bib"]["目标目录"])
    check("AI 指定的子目录在 decide 重标后不被改回", p["论文初稿_v2.docx"]["子目录"] == "" and p["论文初稿_v1.docx"]["子目录"] == "历史版本",
          f"v2「{p['论文初稿_v2.docx']['子目录']}」v1「{p['论文初稿_v1.docx']['子目录']}」")
    check("AI 起的入库名自动补上扩展名", p["IMG_4521.jpg"]["入库名"] == "20261013_扣电组装.jpg", p["IMG_4521.jpg"]["入库名"])
    bad = {"rename_projects": {}, "items": {p["refs.bib"]["id"]: {"类别": "乱写的类别"},
                                            p["analysis.py"]["id"]: {"入库名": "分析.txt"}}}
    (base / "w1" / "bad.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    msg = kb("decide", str(base / "w1" / "plan.csv"), str(base / "w1" / "bad.json"), ok_codes=(1,))
    check("非法类别被拒绝", "不在可选类别里" in msg)
    check("入库名改扩展名被拒绝", "改了扩展名" in msg)

    # ---------------------------------------------------------- 3 执行
    print("\n3. 预演与执行")
    kb("apply", str(base / "w1" / "plan.csv"), "--kb", str(kbdir), "--name", "测试知识库", "--dry-run")
    check("预演不写文件", not kbdir.exists())
    out = kb("apply", str(base / "w1" / "plan.csv"), "--kb", str(kbdir), "--name", "测试知识库")
    print("  " + out.strip().replace("\n", "\n  "))
    with (kbdir / ".kb" / "manifest.csv").open(encoding="utf-8-sig") as f:
        man = list(csv.DictReader(f))
    mismatch = [r["知识库路径"] for r in man if hashlib.sha256((kbdir / r["知识库路径"]).read_bytes()).hexdigest() != r["sha256"]]
    check("复制件哈希与清单一致", not mismatch, f"{len(man)} 个文件")
    check("原件一个字节没变", digest_tree(src) == before)
    for f in ("AGENTS.md", "CLAUDE.md", "知识库导航.md", "知识库说明.md", "知识库变更日志.md", "INBOX.md",
              "00_原始资料/资料索引.md", "01_知识提炼/研究问题/假设台账.md", "02_方法论库/AI提示词/知识库体检提示词.md",
              "02_方法论库/写作模板/模板_概念节点.md"):
        if not (kbdir / f).exists():
            check(f"入口文件 {f}", False)
            break
    else:
        check("入口文件、索引、模板齐全", True)
    log = (kbdir / "知识库变更日志.md").read_text(encoding="utf-8")
    check("变更日志自动记录入库", "入库 " in log and "{{" not in log)
    check("身份证未入库", not list(kbdir.rglob("身份证*")))
    check("apply 自动对账并通过", "✅ 原件完好、一个不漏、副本一致" in out and list((kbdir / ".kb" / "plans").glob("*-对账.md")))
    check("旧版本放进历史版本", (kbdir / "03_研究输出/论文草稿/固态电池综述/历史版本/论文初稿_v1.docx").exists()
          and (kbdir / "03_研究输出/论文草稿/固态电池综述/论文初稿_v3.docx").exists()
          and (kbdir / "03_研究输出/论文草稿/固态电池综述/论文初稿_v2.docx").exists())
    check("同名照片分开存放", (kbdir / "00_原始资料/02_实验记录/电解液/照片/照片1013/1.jpg").exists()
          and (kbdir / "00_原始资料/02_实验记录/电解液/照片/照片1015/1.jpg").exists())
    index = (kbdir / "00_原始资料/资料索引.md").read_text(encoding="utf-8")
    check("改名只改副本，索引里保留原名", (kbdir / "00_原始资料/02_实验记录/电解液/照片/20261013_扣电组装.jpg").exists()
          and "原名 IMG_4521.jpg" in index and (src / "电解液/IMG_4521.jpg").exists())

    print("\n3b. 对账能抓到问题")
    out = kb("verify", str(base / "w1" / "plan.csv"), "--kb", str(kbdir), "--full")
    check("verify --full 通过", "✅" in out)
    victim = kbdir / "00_原始资料/01_论文原文/电解液/Li2024_electrolyte_additive.pdf"
    good = victim.read_bytes()
    victim.write_bytes(good + b"tampered")
    out = kb("verify", str(base / "w1" / "plan.csv"), "--kb", str(kbdir), ok_codes=(3,))
    check("副本被改动时报错", "副本和原件内容不一致" in out)
    victim.write_bytes(good)
    extra = src / "电解液" / "扫描后新增.csv"
    extra.write_text("a,b\n1,2\n", encoding="utf-8")
    out = kb("verify", str(base / "w1" / "plan.csv"), "--kb", str(kbdir), ok_codes=(3,))
    check("扫描后新增的文件被发现", "多了 1 个文件" in out and "扫描后新增.csv" in out)
    extra.unlink()

    # ---------------------------------------------------------- 4 重复运行
    print("\n4. 对同一文件夹重复运行")
    kb("scan", str(src), "--out", str(base / "w2"), "--kb", str(kbdir))
    p2 = plan(base / "w2" / "plan.csv")
    leftovers = [n for n, r in p2.items() if r["状态"] in ("复制", "待AI")]
    check("没有任何文件需要再处理（含上次决定不入库的）", not leftovers, "、".join(leftovers))

    # ---------------------------------------------------------- 5 增量入库
    print("\n5. 增量入库")
    new = base / "new" / "电解液实验"
    new.mkdir(parents=True)
    shutil.copy2(src / "电解液" / "Li2024_electrolyte_additive.pdf", new / "Li2024 副本.pdf")
    (new / "cycle_B50.csv").write_text("sample,capacity\nB50,152\n", encoding="utf-8")
    kb("scan", str(base / "new"), "--out", str(base / "w3"), "--kb", str(kbdir))
    p3 = plan(base / "w3" / "plan.csv")
    check("已有文献识别为已入库", p3["Li2024 副本.pdf"]["状态"] == "已入库")
    check("新数据待复制", p3["cycle_B50.csv"]["状态"] == "复制" and p3["cycle_B50.csv"]["类别"] == "实验记录")
    plan_md = (base / "w3" / "plan.md").read_text(encoding="utf-8")
    check("新出现的近似项目名被提醒", "可能就是「电解液」" in plan_md)

    # ---------------------------------------------------------- 6 登记手动放入的文件
    print("\n6. 登记用户手动放进知识库的文件")
    manual = kbdir / "00_原始资料/02_实验记录/电解液/cycle_B51_手动放入.csv"
    manual.write_text("sample,capacity\nB51,155\n", encoding="utf-8")
    ppt = kbdir / "03_研究输出/汇报PPT/电解液/组会_1017.pptx"
    ppt.write_bytes((src / "电解液" / "slides_final.pptx").read_bytes() + b"x")
    kb("scan", str(kbdir), "--out", str(base / "w4"), "--kb", str(kbdir))
    p4 = plan(base / "w4" / "plan.csv")
    reg = {n: r for n, r in p4.items() if r["状态"] != "已入库"}
    check("只出现两个新文件", set(reg) == {"cycle_B51_手动放入.csv", "组会_1017.pptx"}, "、".join(reg))
    check("按位置反推类别和项目", reg.get("cycle_B51_手动放入.csv", {}).get("类别") == "实验记录"
          and reg.get("cycle_B51_手动放入.csv", {}).get("项目") == "电解液"
          and reg.get("组会_1017.pptx", {}).get("类别") == "汇报PPT")
    kb("apply", str(base / "w4" / "plan.csv"), "--kb", str(kbdir))
    with (kbdir / ".kb" / "manifest.csv").open(encoding="utf-8-sig") as f:
        n_after = len(list(csv.DictReader(f)))
    check("登记后清单 +2，文件没被复制", n_after == len(man) + 2 and manual.exists())

    # ---------------------------------------------------------- 7 体检与粘贴包
    print("\n7. 模拟 AI 写总览和概念节点后，体检与粘贴包")
    (kbdir / "电解液_总览.md").write_text(
        "---\ntitle: 电解液 · 总览\n---\n\n# 电解液 · 总览\n\n## 当前状态\n\n截止电压 4.4 V，见 [[截止电压]] 和 [[SEI 形成]]。\n"
        "相关文献 [[Li2024_electrolyte_additive.pdf]]。\n", encoding="utf-8")
    concept = kbdir / "01_知识提炼/概念节点/SEI 形成.md"
    concept.write_text("# SEI 形成\n\n> [!abstract] 一句话\n> 添加剂在首圈优先还原，形成稳定界面。\n\n**成膜 ≠ 消耗**。见 [[截止电压]]。\n", encoding="utf-8")
    (kbdir / "01_知识提炼/文献卡片/Li2024.md").write_text("# Li2024\n\n**成膜 ≠ 消耗**；[[不存在的页面]]\n", encoding="utf-8")
    (kbdir / "02_方法论库/SOP与方案/SOP_循环测试.md").write_text("# SOP 循环测试\n\n**成膜 ≠ 消耗**\n", encoding="utf-8")
    nav = kbdir / "知识库导航.md"
    nav.write_text(nav.read_text(encoding="utf-8").replace("<!-- 建库时由 AI", "- [[电解液_总览]] — 添加剂筛选中\n<!-- 建库时由 AI"), encoding="utf-8")
    (kbdir / "03_研究输出/报告/电解液/中期汇报_v1.docx").write_bytes(b"PK-v1")
    (kbdir / "03_研究输出/报告/电解液/中期汇报_v2.docx").write_bytes(b"PK-v2")
    (kbdir / "随手放的.md").write_text("临时\n", encoding="utf-8")
    kb("lint", str(kbdir), "--out", str(base / "lint.md"))
    lint = (base / "lint.md").read_text(encoding="utf-8")
    check("体检发现被多处引用却没建页的「截止电压」", "[[截止电压]] ← 2 篇" in lint)
    check("体检发现反复加粗的候选概念", "**成膜 ≠ 消耗** · 3 篇" in lint)
    check("体检列出孤立笔记", "SOP_循环测试.md" in lint)
    check("体检列出单处断链", "[[不存在的页面]]" in lint)
    check("模板注释里的示例链接不算断链", "[[项目名_总览]]" not in lint)
    tidy = lint.split("## 8.")[1]
    check("整洁度：发现根目录多出的文件", "随手放的.md" in tidy)
    check("整洁度：发现待定区积压", "`09_待定` 有 2 个文件" in tidy)
    check("整洁度：发现多个版本并存", "中期汇报_v1.docx、中期汇报_v2.docx" in tidy)
    check("体检不把手动放入后已登记的文件算作未登记", "cycle_B51" not in lint.split("## 7.")[1])
    out = kb("pack", str(kbdir))
    packs = list((kbdir / "99_工作区" / "粘贴包").glob("*.md"))
    text = packs[0].read_text(encoding="utf-8") if packs else ""
    check("粘贴包含导航、总览、假设台账、知识索引（过期时自动更新）",
          all(k in text for k in ("## 知识库导航", "## 电解液_总览", "## 假设台账", "## 知识索引", "[[SEI 形成]]")))

    # ---------------------------------------------------------- 8 蒸馏卡片、知识索引、缺口、增长
    print("\n8. 蒸馏卡片、知识索引、缺口自检、增长统计")
    need = ["01_知识提炼/决策", "01_知识提炼/问题", "01_知识提炼/教训", "01_知识提炼/人物", "知识索引.md",
            "01_知识提炼/研究问题/缺口清单.md", "02_方法论库/AI提示词/缺口自检提示词.md"]
    need += [f"02_方法论库/写作模板/模板_{n}.md" for n in ("文献卡片", "决策卡", "问题卡", "SOP", "人物卡", "教训卡", "概念节点")]
    missing = [n for n in need if not (kbdir / n).exists()]
    check("建库时生成卡片目录、七种卡片模板、缺口清单和知识索引", not missing, "、".join(missing))

    def card(rel: str, fm: dict, body: str) -> None:
        head = "\n".join(f"{k}: {v}" for k, v in fm.items())
        (kbdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (kbdir / rel).write_text(f"---\n{head}\n---\n\n{body}\n", encoding="utf-8")

    card("01_知识提炼/文献卡片/Li2024.md",
         {"类型": "文献", "一句话": "2 wt% 氟代添加剂即可稳定 4.5 V 循环", "用于": "设计添加剂浓度对照时",
          "来源": '"[[Li2024_electrolyte_additive.pdf]]"', "项目": "电解液", "创建": dt.date.today().isoformat()},
         "# Li2024\n\n**成膜 ≠ 消耗**")
    card("01_知识提炼/决策/决策 · 截止电压改为 4.4 V.md",
         {"类型": "决策", "一句话": "截止电压定为 4.4 V", "用于": "安排下一轮循环测试、有人再提 4.5 V 时",
          "来源": '"[[2026-10-07讨论纪要.docx]]"', "项目": "电解液"}, "# 决策\n")
    card("01_知识提炼/问题/问题 · 容量跳水.md",
         {"类型": "问题", "一句话": "容量跳水与截止电压无关", "用于": "再看到容量跳水时", "项目": "电解液"}, "# 问题\n")
    card("01_知识提炼/教训/教训 · 一轮只改一个变量.md",
         {"类型": "教训", "一句话": "一轮只改一个变量", "用于": "设计新一轮实验之前",
          "来源": '"[[电解液_进展时间线]]"'}, "# 教训\n")
    card("01_知识提炼/人物/刘老师.md",
         {"类型": "人物", "一句话": "导师，决定实验路线", "用于": "需要拍板实验路线时", "来源": '"[[导师修改意见.docx]]"'},
         "# 刘老师\n")
    gap_file = kbdir / "01_知识提炼/研究问题/缺口清单.md"
    gap_text = gap_file.read_text(encoding="utf-8")
    gap_text = gap_text.replace("## 已补\n", "- [ ] 2026-10-10 · 添加剂分解产物 · 解释容量衰减 · 文献检索 · 能判断要不要降低截止电压\n\n## 已补\n"
                                "- [x] 2026-10-01 · 添加剂浓度范围 → 已补：[[Li2024]]（2026-10-12）\n")
    gap_file.write_text(gap_text, encoding="utf-8")
    log_file = kbdir / "知识库变更日志.md"
    marker = "<!-- 新记录写在这一行下面 -->"
    today_head = f"## {dt.date.today().isoformat()}"
    log_text = log_file.read_text(encoding="utf-8").replace(
        marker + "\n\n" + today_head,
        marker + "\n\n" + today_head + "\n- [蒸馏] [[Li2024_electrolyte_additive.pdf]] → [[Li2024]]\n- [回流] 更新 [[SEI 形成]]", 1)
    log_file.write_text(log_text, encoding="utf-8")

    kb("lint", str(kbdir), "--out", str(base / "lint2.md"))
    check("卡片比索引新时，体检提示索引过期", "知识索引已过期" in (base / "lint2.md").read_text(encoding="utf-8"))

    out = kb("index", str(kbdir))
    idx = (kbdir / "知识索引.md").read_text(encoding="utf-8")
    check("索引按类型分组并显示一句话和用于",
          "## 决策（1）" in idx and "截止电压定为 4.4 V · 用于：安排下一轮循环测试" in idx
          and "## 人物（1）" in idx and "## 教训（1）" in idx and "## 文献（1）" in idx)
    check("索引列出缺字段的卡片", "## 待补索引信息" in idx and "SEI 形成.md" in idx.split("## 待补索引信息")[1])
    check("索引列出项目总览", "[[电解液_总览]]" in idx)

    kb("gaps", str(kbdir), "--out", str(base / "gaps.md"))
    gaps = (base / "gaps.md").read_text(encoding="utf-8")
    sec1 = gaps.split("## 1.")[1].split("## 2.")[0]
    check("缺口：被卡片引用过的原文不算未蒸馏", "Li2024_electrolyte_additive.pdf" not in sec1 and "讨论纪要.docx" not in sec1)
    check("缺口：没有卡片引用的原文列为未蒸馏", "第3讲_高分子物理.pdf" in sec1 and "录音_组会1015.m4a" in sec1)
    check("缺口：照片和表格数据不算待蒸馏", ".jpg" not in sec1 and ".csv" not in sec1 and ".xlsx" not in sec1)
    check("缺口：自动生成的资料索引不算“被笔记链接”", "篇笔记链接" not in sec1)
    check("缺口：读出缺口清单待补和已补", "添加剂分解产物" in gaps and "已补 1" in gaps and "已挂" in gaps)
    check("缺口：没写来源的卡片被列出", "问题 · 容量跳水.md" in gaps.split("## 4.")[1])
    check("缺口：被提到却没写的知识", "[[截止电压]]" in gaps.split("## 3.")[1])

    out = kb("stats", str(kbdir))
    y, w, _ = dt.date.today().isocalendar()
    row = next((ln for ln in out.splitlines() if ln.startswith(f"| {y}-W{w:02d}")), "")
    cells = [c.strip() for c in row.strip("|").split("|")]
    ok = len(cells) == 9 and int(cells[1]) >= 1 and int(cells[2]) >= 1 and int(cells[3]) >= 1 and int(cells[8]) >= 5
    check("增长统计：本周入库、蒸馏、回流和新卡片都计入", ok, row)
    check("增长统计给出判断", "**判断**：最近两周新增" in out)
    check("体检包含增长一节", "## 9. 增长" in (base / "lint2.md").read_text(encoding="utf-8"))

    kb("pack", str(kbdir))
    text = sorted((kbdir / "99_工作区" / "粘贴包").glob("*.md"))[-1].read_text(encoding="utf-8")
    check("粘贴包带上新卡片", "截止电压定为 4.4 V" in text and "[[刘老师]]" in text)

    # ---------------------------------------------------------- 9 知识库建在资料文件夹里面
    print("\n9. 打开一个资料文件夹就开始：知识库建在它里面的「知识库/」")
    inbox = base / "资料"
    shutil.rmtree(inbox, ignore_errors=True)
    inbox.mkdir()
    (inbox / "组会纪要_1020.txt").write_text("刘老师：先做对照。\n", encoding="utf-8")
    (inbox / "cycle_B60.csv").write_text("sample,capacity\nB60,150\n", encoding="utf-8")
    (inbox / "想法.md").write_text("- 试试新添加剂\n", encoding="utf-8")
    inner = inbox / "知识库"
    before_inbox = digest_tree(inbox)
    kb("scan", str(inbox), "--out", str(base / "w9"), "--kb", str(inner))
    out = kb("apply", str(base / "w9" / "plan.csv"), "--kb", str(inner), "--name", "测试")
    check("在资料文件夹里建库并对账通过", "✅ 原件完好、一个不漏、副本一致" in out)
    check("资料文件夹里的原件没变", {k: v for k, v in digest_tree(inbox).items() if not k.startswith("知识库")} == before_inbox)
    (inbox / "cycle_B61.csv").write_text("sample,capacity\nB61,151\n", encoding="utf-8")
    kb("scan", str(inbox), "--out", str(base / "w9b"), "--kb", str(inner))
    p9 = plan(base / "w9b" / "plan.csv")
    check("以后新资料继续丢进同一个文件夹：只有新文件待入库，知识库自己不会被当成资料",
          {n for n, r in p9.items() if r["状态"] == "复制"} == {"cycle_B61.csv"}
          and not any("知识库" in r["原路径"].split("资料", 1)[1] for r in p9.values()), "、".join(p9))
    out = kb("verify", str(base / "w9" / "plan.csv"), "--kb", str(inner), ok_codes=(0, 3))
    check("新增文件在 verify 里被如实报告", "多了 1 个文件" in out and "cycle_B61.csv" in out)

    # ---------------------------------------------------------- 汇总
    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项检查，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
