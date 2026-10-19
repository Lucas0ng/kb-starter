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
import os
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


def run(*args: str) -> subprocess.CompletedProcess:
    """跑 kb.py，不管退出码，留给断言判断。"""
    return subprocess.run([sys.executable, str(KB_PY), *args], capture_output=True, text=True)


def safety_regressions(base: Path) -> None:
    """外部评审（2026-10）指出的 5 个问题，每个一组回归测试。"""
    sys.path.insert(0, str(HERE))
    from make_fixtures import pdf

    root = base / "safety"
    if root.exists():
        for p in root.rglob("*"):
            if p.is_dir():
                p.chmod(0o755)
        shutil.rmtree(root)
    root.mkdir(parents=True)

    # ---------------------------------------------------------- 隐私
    print("\n安全回归 1. 隐私文件不读内容，片段只给待AI")
    src = root / "privacy"
    src.mkdir()
    id_no, card_no = "110101199001011234", "6222020200112233445"
    (src / "身份证_扫描件.txt").write_text(f"姓名 张三\n身份证号 {id_no}\n", encoding="utf-8")
    pdf(src / "银行流水_2026.pdf", ["Account statement", f"Card {card_no}"])
    (src / "体检报告_2026.txt").write_text("肝功能异常，建议复查\n", encoding="utf-8")
    (src / "note.txt").write_text("普通笔记，规则能直接分类\n", encoding="utf-8")
    pdf(src / "scan_0099.pdf", ["handwritten page"])  # 普通的待AI文件：片段照常给
    kb("scan", str(src), "--out", str(root / "w_priv"))
    snip = (root / "w_priv" / "snippets.jsonl").read_text(encoding="utf-8")
    pp = plan(root / "w_priv" / "plan.csv")
    check("身份证号、银行卡号、体检内容都没有落进片段文件",
          id_no not in snip and card_no not in snip and "肝功能" not in snip)
    check("隐私文件标成待AI，并写明没有读取内容",
          all(pp[n]["状态"] == "待AI" and "没有读取内容" in pp[n]["依据"]
              for n in ("身份证_扫描件.txt", "银行流水_2026.pdf", "体检报告_2026.txt")))
    entries = [json.loads(line) for line in snip.splitlines()]
    check("片段文件只有待AI条目，隐私条目只留标记",
          all(e["状态"] == "待AI" for e in entries) and "note.txt" not in {e["文件名"] for e in entries}
          and all(e.get("隐私") and not e["片段"] for e in entries if "疑似隐私" in pp[e["文件名"]]["依据"]),
          f"{len(entries)} 条")
    check("普通待AI文件的片段照常给", any(e["文件名"] == "scan_0099.pdf" and "handwritten" in e["片段"] for e in entries))

    # ---------------------------------------------------------- 无权限文件夹
    print("\n安全回归 2. 读不了的文件夹不能报“一个不漏”")
    src = root / "locked_src"
    (src / "锁住的").mkdir(parents=True)
    (src / "a_note.txt").write_text("能读的\n", encoding="utf-8")
    (src / "锁住的" / "b.txt").write_text("读不到的\n", encoding="utf-8")
    (src / "锁住的").chmod(0)
    try:
        if os.access(src / "锁住的", os.R_OK):  # root 或 Windows 上锁不住，跳过
            check("（跳过）当前环境锁不住文件夹", True)
        else:
            kb("scan", str(src), "--out", str(root / "w_lock"))
            pl = plan(root / "w_lock" / "plan.csv")
            check("读不了的文件夹在方案表里有一行，状态是无法读取",
                  pl.get("锁住的/", {}).get("状态") == "无法读取", str(pl.get("锁住的/")))
            p = run("apply", str(root / "w_lock" / "plan.csv"), "--kb", str(root / "kb_lock"))
            out = p.stdout + p.stderr
            check("对账退出码 3，指出文件夹没法清点，不打勾",
                  p.returncode == 3 and "没法清点" in out and "✅ 原件完好" not in out, out.strip()[-200:])
    finally:
        (src / "锁住的").chmod(0o755)

    # ---------------------------------------------------------- 路径穿越
    print("\n安全回归 3. 项目名、子目录、入库名不能把文件写出知识库")
    src = root / "trav_src"
    src.mkdir()
    (src / "data_r1.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    kb("scan", str(src), "--out", str(root / "w_trav"))
    rid = plan(root / "w_trav" / "plan.csv")["data_r1.csv"]["id"]
    for label, item, extra in (("项目 ../", {"项目": "../../../kb-escape"}, {}),
                               ("子目录 a/../..", {"子目录": "a/../../b"}, {}),
                               ("入库名 ..", {"入库名": ".."}, {}),
                               ("改名到 ../", {}, {"rename_projects": {"": "../x"}})):
        dec = root / "w_trav" / "bad.json"
        dec.write_text(json.dumps({"items": {rid: item} if item else {}, **extra}, ensure_ascii=False), encoding="utf-8")
        p = run("decide", str(root / "w_trav" / "plan.csv"), str(dec))
        check(f"decide 拒绝{label}", p.returncode == 1 and "路径" in p.stderr, p.stderr.strip()[-120:])
    # 绕过 decide：直接手改 plan.csv
    rows = list(csv.DictReader((root / "w_trav" / "plan.csv").open(encoding="utf-8-sig", newline="")))
    for r in rows:
        if r["文件名"] == "data_r1.csv":
            r["项目"] = "../../kb-escape"
    with (root / "w_trav" / "plan.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    kbt = root / "deep" / "a" / "kb"
    p = run("apply", str(root / "w_trav" / "plan.csv"), "--kb", str(kbt))
    check("手改 plan.csv 的穿越项目名：apply 拒绝，一个文件都不写",
          p.returncode == 2 and not kbt.exists() and not list(root.rglob("kb-escape")), p.stderr.strip()[-150:])
    kbr = root / "kb_rule"
    kbr.mkdir()
    (kbr / "整理规则.md").write_text("## 你的规则\n\n| 匹配 | 看哪里 | 归为 | 项目 | 为什么 |\n|---|---|---|---|---|\n"
                                    "| data | 文件名 | 实验记录 | ../../evil | 测试 |\n", encoding="utf-8")
    p = run("scan", str(src), "--out", str(root / "w_rule"), "--kb", str(kbr))
    check("整理规则里的穿越项目名被忽略并提示",
          "这一行先不用" in p.stderr and plan(root / "w_rule" / "plan.csv")["data_r1.csv"]["项目"] != "../../evil")

    # ---------------------------------------------------------- 编码
    print("\n安全回归 4. Excel 另存的方案表")
    src = root / "enc_src"
    src.mkdir()
    (src / "实验记录_r2.csv").write_text("x\n1\n", encoding="utf-8")
    kb("scan", str(src), "--out", str(root / "w_gbk"))
    pc = root / "w_gbk" / "plan.csv"
    rows = list(csv.DictReader(pc.open(encoding="utf-8-sig", newline="")))
    for r in rows:  # Excel 常规格式会把修改时间的小数位丢掉
        r["修改时间"] = r["修改时间"].split(".")[0] if r.get("修改时间") else ""
    with pc.open("w", encoding="gb18030", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    p = run("apply", str(pc), "--kb", str(root / "kb_gbk"))
    check("GBK 编码、丢了小数位的方案表能正常入库并通过对账",
          p.returncode == 0 and "✅" in p.stdout and "Traceback" not in p.stderr, (p.stdout + p.stderr).strip()[-200:])
    pc.write_text("id;状态;原路径\n1;复制;x\n", encoding="utf-8")
    p = run("apply", str(pc), "--kb", str(root / "kb_gbk2"))
    check("表头坏了：给出“另存为 CSV UTF-8”的提示，不是报错堆栈",
          p.returncode == 2 and "CSV UTF-8" in p.stderr and "Traceback" not in p.stderr, p.stderr.strip()[-150:])

    # ---------------------------------------------------------- 非空文件夹
    print("\n安全回归 5. 已有内容的文件夹不能悄悄塞骨架")
    vault = root / "vault"
    vault.mkdir()
    (vault / "我的笔记.md").write_text("# 旧笔记\n", encoding="utf-8")
    kb("scan", str(src), "--out", str(root / "w_merge"))
    p = run("apply", str(root / "w_merge" / "plan.csv"), "--kb", str(vault))
    check("没有 --merge：退出码 2，提示先问用户，没有写入骨架",
          p.returncode == 2 and "--merge" in p.stderr and not (vault / "AGENTS.md").exists(), p.stderr.strip()[-150:])
    p = run("apply", str(root / "w_merge" / "plan.csv"), "--kb", str(vault), "--merge")
    check("用户同意后 --merge 并入，旧笔记原样保留",
          p.returncode == 0 and (vault / "AGENTS.md").exists() and (vault / "我的笔记.md").read_text(encoding="utf-8") == "# 旧笔记\n")
    fresh = root / "fresh"
    fresh.mkdir()
    (fresh / "知识库纲要.md").write_text("# 纲要\n", encoding="utf-8")
    p = run("apply", str(root / "w_merge" / "plan.csv"), "--kb", str(fresh))
    check("只有纲要的文件夹算空文件夹，不需要 --merge", p.returncode == 0, p.stderr.strip()[-150:])


def adopt_tests(base: Path) -> None:
    """接入已有知识库：一个 PARA 风格的 Obsidian 库，只读盘点 → 确认映射 → 原地索引。"""
    sys.path.insert(0, str(HERE))
    from make_fixtures import pdf

    print("\n接入已有知识库（adopt）")
    root = base / "adopt"
    shutil.rmtree(root, ignore_errors=True)
    v = root / "vault"

    def note(rel: str, body: str, tags: tuple[str, ...] = ()) -> None:
        p = v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = "---\ntags:\n" + "".join(f"  - {t}\n" for t in tags) + "---\n\n" if tags else ""
        p.write_text(fm + body, encoding="utf-8")

    (v / ".obsidian").mkdir(parents=True)
    (v / ".obsidian" / "app.json").write_text(json.dumps({"userIgnoreFilters": ["Scratch/"]}), encoding="utf-8")
    note("电解液_总览.md", "# 电解液 · 总览\n\n当前在做添加剂筛选。\n")
    note("README.md", "# 我的库\n\n随便写写。\n")
    note("1 Projects/电解液课题/添加剂筛选思路.md", "# 添加剂筛选思路\n\n先看 LiPF6 体系的稳定性。\n",
         ("项目/电解液", "类型/概念"))
    note("1 Projects/电解液课题/组会要点.md", "# 组会要点\n\n周三组会定了三组对照。\n", ("项目/电解液",))
    pdf(v / "1 Projects/电解液课题/方案原稿.pdf", ["draft plan"])
    note("2 Areas/健康/睡眠.md", "# 睡眠\n\n每天 7 小时。\n")
    note("2 Areas/健康/体检报告2025.md", "# 体检\n\n肝功能异常，建议复查。\n")
    (v / "3 Resources/Papers").mkdir(parents=True)
    for i in range(3):
        pdf(v / f"3 Resources/Papers/paper{i}.pdf", [f"Paper {i}", f"doi:10.1000/xyz{i}"])
    note("3 Resources/读书笔记/思考快与慢.md",
         "# 思考快与慢\n\n> Source: book\n\n## 一句话总结\n\n人有快慢两套思考系统，快系统省力但容易出错。\n\n## 细节\n\n…\n")
    note("4 Archive/旧课题/旧笔记.md", "# 旧笔记\n\n不再用了。\n")
    for d in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
        note(f"Daily/{d}.md", f"# {d}\n\n今天做了实验。\n")
    note("Templates/模板_日记.md", "# {{date}}\n")
    (v / "attachments").mkdir()
    for i in range(3):
        (v / "attachments" / f"img{i}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 50)
    note("Scratch/草稿.md", "# 临时\n")
    note("杂物/一个笔记.md", "# 杂\n")
    pdf(v / "杂物/一个文件.pdf", ["misc"])
    (v / "杂物/一张图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    before = digest_tree(v)

    p = run("adopt", str(v), "--out", str(v / "_out"))
    check("--out 放在知识库里面会被拒绝（盘点必须只读）", p.returncode == 2 and not (v / "_out").exists())
    out = root / "out"
    kb("adopt", str(v), "--out", str(out))
    check("只读盘点：库里一个字节没变，没有生成 .kb/", digest_tree(v) == before and not (v / ".kb").exists())
    rows = {r["目录"]: r for r in csv.DictReader((out / "adopt_map.csv").open(encoding="utf-8-sig", newline=""))}
    expect = {
        "1 Projects": "知识", "1 Projects/电解液课题": "知识", "2 Areas": "知识", "3 Resources": "知识",
        "3 Resources/Papers": "原始资料", "4 Archive": "归档", "Daily": "复盘", "Templates": "模板",
        "attachments": "附件", "Scratch": "忽略", "杂物": "待定",
    }
    wrong = [f"{d}→{rows.get(d, {}).get('角色')}" for d, role in expect.items() if rows.get(d, {}).get("角色") != role]
    check(f"{len(expect)} 个目录的角色建议正确", not wrong, "；".join(wrong))
    check("项目文件夹归到已识别的项目，主题文件夹各当一个项目",
          rows["1 Projects/电解液课题"]["项目"] == "电解液" and rows["2 Areas/健康"]["项目"] == "健康",
          f"{rows['1 Projects/电解液课题']['项目']} / {rows['2 Areas/健康']['项目']}")
    report = (out / "adopt.md").read_text(encoding="utf-8")
    check("从总览和标签识别出项目「电解液」", "| 电解液 | 电解液_总览.md | 2 |" in report)
    draft = (out / "知识索引-草稿.md").read_text(encoding="utf-8")
    check("索引草稿收进知识目录的笔记，不收模板、归档、日记",
          all(n in draft for n in ("[[添加剂筛选思路]]", "[[睡眠]]", "[[思考快与慢]]"))
          and not any(n in draft for n in ("模板_日记", "旧笔记", "2026-10-01")))
    check("隐私笔记不读、不进草稿，报告里列出", "体检报告2025" not in draft and "体检报告2025" in report)
    check("没写「一句话」时用“一句话总结”小节做摘要", "人有快慢两套思考系统" in draft)
    check("标签里的类型和项目被认出来", "## 概念" in draft and "[[添加剂筛选思路]] — 先看 LiPF6 体系的稳定性。 · 电解液" in draft)

    p = run("adopt", str(v), "--out", str(out), "--write")
    check("--write 没有 --map：拒绝", p.returncode == 2 and not (v / ".kb").exists())
    bad = out / "bad_map.csv"
    bad.write_text("目录,角色,项目\n2 Areas,乱写,\n", encoding="utf-8-sig")
    p = run("adopt", str(v), "--out", str(out), "--map", str(bad), "--write")
    check("映射表里的角色不合法：拒绝，不写入", p.returncode == 2 and "不在可选角色里" in p.stderr and not (v / ".kb").exists())
    (v / "知识索引.md").write_text("# 我自己写的索引\n", encoding="utf-8")
    p = run("adopt", str(v), "--out", str(out), "--map", str(out / "adopt_map.csv"), "--write")
    check("已有手写的知识索引：不覆盖", p.returncode == 2 and (v / "知识索引.md").read_text(encoding="utf-8") == "# 我自己写的索引\n")
    (v / "知识索引.md").unlink()
    kb("adopt", str(v), "--out", str(out), "--map", str(out / "adopt_map.csv"), "--write")
    after = digest_tree(v)
    added = sorted(set(after) - set(before))
    check("确认后写入：只多了 .kb/adopt.csv 和 知识索引.md，原有文件一个没动",
          added == [".kb/adopt.csv", "知识索引.md"] and all(after[k] == before[k] for k in before), "、".join(added))
    index = (v / "知识索引.md").read_text(encoding="utf-8")
    check("写入的索引和草稿收的笔记一样", index.count("\n- [[") == draft.count("\n- [["))
    out_idx = kb("index", str(v))
    check("以后 KB index 按映射找卡片，隐私笔记仍不进索引",
          "知识卡片 4 张" in out_idx and "体检报告2025" not in (v / "知识索引.md").read_text(encoding="utf-8"), out_idx.strip().splitlines()[1] if out_idx.strip() else "")
    kb("scan", str(v / "3 Resources"), "--out", str(root / "w_ad"))
    p = run("apply", str(root / "w_ad" / "plan.csv"), "--kb", str(v))
    check("接入过的库仍需 --merge 才能往里复制资料", p.returncode == 2 and "--merge" in p.stderr)


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

    # ---------------------------------------------------------- 10 纲要：先访谈，再按纲要分项目、写总览
    print("\n10. 纲要：按用户定的主线分项目、生成总览草稿、体检对照")
    kb10 = base / "kb10"
    shutil.rmtree(kb10, ignore_errors=True)
    kb10.mkdir()
    (kb10 / "知识库纲要.md").write_text("""---
title: 知识库纲要
更新: 2026-10-16
---

# 测试 · 纲要

## 这个知识库用来做什么

读研期间的课题资料、综述和课程笔记。

## 项目

<!-- 注释里的示例不算：### 项目：示例项目 -->

### 项目：电解液

- 一句话目标：筛选能让锂电池在 4.4 V 下稳定循环的电解液添加剂
- 现在在哪一步：第 1 轮添加剂筛选做完
- 下一步：做 4 组浓度对照
- 核心问题：
  1. 添加剂浓度多少时循环最稳？
  2. 高电压下容量衰减来自哪里？
- 关键词：电解液、添加剂、截止电压、FEC、LiPF6、electrolyte、additive
- 别名：电解液实验、battery
- 大纲来源：开题报告_v2.docx

### 项目：综述

- 一句话目标：写一篇固态电解质综述
- 现在在哪一步：初稿 v3
- 核心问题：固态电解质界面问题有哪些解决思路？
- 关键词：固态电解质、solid-state、综述、review

### 课程：高分子物理

- 一句话目标：期末考试
- 关键词：高分子物理、polymer physics

### 项目：{{项目名}}
""", encoding="utf-8")
    (src / "张老师给的").mkdir(exist_ok=True)  # 不是通用名、也不在纲要里的文件夹
    (src / "张老师给的" / "参考资料.txt").write_text("一些背景材料\n", encoding="utf-8")
    kb("scan", str(src), "--out", str(base / "w10"), "--kb", str(kb10))
    p10 = plan(base / "w10" / "plan.csv")
    want = {
        "开题报告_v2.docx": "电解液",          # 根目录散落文件，首页标题命中关键词
        "申请书_国自然.docx": "电解液",
        "meeting_transcript.txt": "电解液",    # 正文提到“截止电压”
        "随手想法.md": "电解液",               # 正文提到 FEC
        "电解质综述_中文期刊.pdf": "综述",      # 文件名里的“综述”比正文里的 electrolyte 分高
        "第3讲_高分子物理.pdf": "高分子物理",    # 文件夹名就是纲要里的课程
        "组会汇报_20261010.pptx": "电解液",     # 文件夹名就是纲要项目
        "seminar_notes.md": "",               # 什么都没命中
    }
    wrong = [f"{n} → {p10[n]['项目']!r}（{p10[n]['依据']}）" for n, proj in want.items() if p10[n]["项目"] != proj]
    check("按纲要分项目：文件夹名、文件名、首页关键词", not wrong, "；".join(wrong))
    check("依据里写明靠哪个关键词分的", "纲要关键词" in p10["开题报告_v2.docx"]["依据"])
    md10 = (base / "w10" / "plan.md").read_text(encoding="utf-8")
    check("plan.md 按纲要列项目，模板占位和注释里的示例不算项目",
          "识别到的项目（以纲要为准）" in md10 and "- 电解液：" in md10 and "示例项目" not in md10 and "{{" not in md10)
    check("纲要外的文件夹名被单独列出，通用文件夹名不算", "不在纲要里的项目名" in md10
          and "- 张老师给的：1 个文件" in md10 and "papers" not in md10.split("不在纲要里的项目名")[-1])
    check("两个项目打平时不硬分，留给 AI", p10["网页剪藏_电解液综述.md"]["项目"] == ""
          and "同时命中" in p10["网页剪藏_电解液综述.md"]["依据"])
    kb("apply", str(base / "w10" / "plan.csv"), "--kb", str(kb10), "--name", "测试", "--pending-to-unsorted", ok_codes=(0, 3))
    check("已有纲要不会被空模板覆盖", "### 项目：电解液" in (kb10 / "知识库纲要.md").read_text(encoding="utf-8"))
    out = kb("overview", str(kb10))
    ov = (kb10 / "电解液_总览.md").read_text(encoding="utf-8") if (kb10 / "电解液_总览.md").exists() else ""
    check("overview 为纲要里每个项目生成总览", all((kb10 / f"{n}_总览.md").exists() for n in ("电解液", "综述", "高分子物理")), out.strip())
    check("【你定】段落照抄纲要", "## 一句话目标【你定】" in ov and "4.4 V 下稳定循环" in ov and "1. 添加剂浓度多少时循环最稳？" in ov)
    check("【AI 整理】段落列出最近记录和现行文件候选", "最近的记录" in ov and "| 实验记录（候选） | [[" in ov and "| 会议记录（候选） | [[" in ov)
    rows_ov = [ln for ln in ov.splitlines() if ln.startswith("| ") and "[[0" in ln]  # 带路径和别名的链接
    check("表格里的链接竖线已转义，Obsidian 里表格不会被撑坏", rows_ov and all("\\|" in ln and ln.count(" | ") == 2 for ln in rows_ov))
    recent_ov = ov.split("最近的记录")[1].split("##")[0]
    check("最近的记录只列能反映进展的文件（不列照片、剪藏）", "· 照片 ·" not in recent_ov and "· 网页剪藏 ·" not in recent_ov)
    check("同一行写的核心问题也能读出来", "1. 固态电解质界面问题有哪些解决思路？" in (kb10 / "综述_总览.md").read_text(encoding="utf-8"))
    check("纲要没写的字段进待确认", "纲要里还没写「下一步」" in (kb10 / "高分子物理_总览.md").read_text(encoding="utf-8"))
    out = kb("overview", str(kb10), "--project", "电解液")
    check("总览已存在时不覆盖，草稿另存", "总览草稿" in out and (kb10 / "电解液_总览.md").read_text(encoding="utf-8") == ov)
    lint10 = base / "lint10.md"
    kb("lint", str(kb10), "--out", str(lint10))
    lt = lint10.read_text(encoding="utf-8")
    check("体检有纲要对照一节", "## 10. 纲要对照" in lt and "项目「电解液」" in lt and "课程「高分子物理」" in lt)
    gp = kb("gaps", str(kb10))
    check("缺口清单列出纲要核心问题", "## 6. 纲要里的核心问题（3）" in gp and "电解液：添加剂浓度多少时循环最稳？" in gp)
    kb("pack", str(kb10))
    pk = next((kb10 / "99_工作区" / "粘贴包").glob("*.md")).read_text(encoding="utf-8")
    check("粘贴包把纲要放在最前面", pk.find("## 知识库纲要") != -1 and pk.find("## 知识库纲要") < pk.find("## 知识库导航"))
    lint_old = base / "lint_old.md"
    kb("lint", str(kbdir), "--out", str(lint_old))
    check("没有纲要的库，体检提示补纲要", "还没有写好的 `知识库纲要.md`" in lint_old.read_text(encoding="utf-8"))

    # ---------------------------------------------------------- 11 先整理、后审查：整理规则 + reorg
    print("\n11. 先整理后审查：自动生成整理规则，用户改规则后 reorg 挪副本")
    rules_md = kb10 / "整理规则.md"
    rt = rules_md.read_text(encoding="utf-8") if rules_md.exists() else ""
    check("apply 自动生成整理规则报告", all(s in rt for s in ("## 你的规则（优先）", "### A. 先看这几条", "### B. 分类规则",
                                                    "### C. 项目规则", "### D. 不入库", "| B1 |")))
    check("隐私文件没进待定、没入库，也没记成“已决定不入库”",
          not list(kb10.rglob("身份证*")) and "隐私待定" in rt
          and "身份证" not in (kb10 / ".kb" / "ignored.csv").read_text(encoding="utf-8-sig") if (kb10 / ".kb" / "ignored.csv").exists() else True)
    a_part = rt.split("### A.")[1].split("### B.")[0]
    b_part = rt.split("### B.")[1].split("### C.")[0]
    check("A 里列过的文件不在 B 里重复", "untitled.png" in a_part and "untitled.png" not in b_part)
    check("按文件夹猜的纲要外项目在 C 里标出来", "（纲要里没有这个项目）" in rt)

    rt = rt.replace("| 匹配 | 看哪里 | 归为 | 项目 | 为什么 |\n|---|---|---|---|---|\n",
                    "| 匹配 | 看哪里 | 归为 | 项目 | 为什么 |\n|---|---|---|---|---|\n"
                    "| slides | 文件名 | 课程资料 | 高分子物理 | slides 都是课件 |\n"
                    "| 张老师给的 | 文件夹 | | 电解液 | 张老师给的是电解液参考资料 |\n"
                    "| metadata_update | 文件名 | 不入库 | | 没用的导出文件 |\n"
                    "| 乱写 | 文件名 | 不存在的类别 | | 这一行应被忽略 |\n", 1)
    rules_md.write_text(rt, encoding="utf-8")
    before_src = digest_tree(src)
    man_before = (kb10 / ".kb" / "manifest.csv").read_text(encoding="utf-8-sig")
    out = kb("reorg", str(kb10), "--dry-run")
    check("reorg 预演列出 3 个文件、不改动，并提示无效规则",
          out.count("[预演]") == 4 and "不存在的类别" in out
          and (kb10 / ".kb" / "manifest.csv").read_text(encoding="utf-8-sig") == man_before, out.strip())
    ov_before = (kb10 / "电解液_总览.md").read_text(encoding="utf-8")
    out = kb("reorg", str(kb10))
    print("  " + out.strip().replace("\n", "\n  "))
    new_slides = kb10 / "00_原始资料/05_课程笔记/高分子物理/slides_final.pptx"
    check("规则改类别：slides 挪到课程资料", new_slides.exists() and not (kb10 / "03_研究输出/汇报PPT/电解液/slides_final.pptx").exists())
    check("规则只改项目：张老师给的 → 电解液", (kb10 / "00_原始资料/06_个人笔记/电解液/参考资料.txt").exists())
    check("规则不入库：副本移到 .kb/回收（可恢复），清单里删掉",
          not list((kb10 / "00_原始资料").rglob("metadata_update.csv")) and list((kb10 / ".kb" / "回收").rglob("metadata_update.csv"))
          and "metadata_update" not in (kb10 / ".kb" / "manifest.csv").read_text(encoding="utf-8-sig"))
    ov_after = (kb10 / "电解液_总览.md").read_text(encoding="utf-8")
    check("总览里指向挪走文件的链接跟着更新", "汇报PPT/电解液/slides_final.pptx" in ov_before
          and "汇报PPT/电解液/slides_final.pptx" not in ov_after and "05_课程笔记/高分子物理/slides_final.pptx" in ov_after)
    check("reorg 不碰原件", digest_tree(src) == before_src)
    archived = sorted((kb10 / ".kb" / "plans").glob("*-plan.csv"))[-1]
    out = kb("verify", str(archived), "--kb", str(kb10), ok_codes=(0, 3))
    check("reorg 之后对账仍通过（移出的算“按规则移出”）", "✅" in out and "事后按整理规则移出" in out, out.strip()[-300:])
    check("变更日志记了 [整理]", "[整理] **按整理规则重新整理**" in (kb10 / "知识库变更日志.md").read_text(encoding="utf-8"))

    pptx_src = src / "电解液" / "slides_final.pptx"
    (src / "电解液" / "slides_week2.pptx").write_bytes(pptx_src.read_bytes() + b"week2")
    kb("scan", str(src), "--out", str(base / "w11"), "--kb", str(kb10))
    p11 = plan(base / "w11" / "plan.csv")
    check("以后入库先套用你的规则", p11["slides_week2.pptx"]["类别"] == "课程资料" and p11["slides_week2.pptx"]["项目"] == "高分子物理"
          and p11["slides_week2.pptx"]["依据"].startswith("你的规则 1"), p11["slides_week2.pptx"]["依据"])
    check("移出的文件以后不再入库", p11["metadata_update.csv"]["状态"] == "跳过", p11["metadata_update.csv"]["依据"])
    check("隐私文件下次扫描仍会提醒", p11["身份证扫描.jpg"]["状态"] == "待AI")
    kb("apply", str(base / "w11" / "plan.csv"), "--kb", str(kb10), "--pending-to-unsorted", ok_codes=(0, 3))
    rt2 = rules_md.read_text(encoding="utf-8")
    check("再次 apply：你的规则原样保留，报告写明规则用了几次",
          "| slides | 文件名 | 课程资料 | 高分子物理 |" in rt2 and "规则 1 用了 1 次" in rt2 and rt2.count("## 最近一次整理用到的规则") == 1)

    safety_regressions(base)
    adopt_tests(base)

    # ---------------------------------------------------------- 汇总
    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项检查，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
