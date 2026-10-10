"""Markdown の見出しごとに文の難しさを採点し、必要ならブログ型の HTML にする。

使い方:
  python md_page.py 入力.md                          見出しごとの点数を表示する
  python md_page.py 入力.md 出力.html "ページ名" ["上の小見出し"]   HTML も作る

見出しは「#」がタイトル、「##」「###」「####」がセクション。
「> 」で始まる行（解説など）や「- 」の箇条書きは、そのまま本文として扱う。
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import scorer  # noqa: E402


def parse(md):
    sections = []
    cur = {"level": 1, "heading": "", "paras": []}
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            if cur["heading"] or cur["paras"]:
                sections.append(cur)
            cur = {"level": min(level, 4), "heading": s.lstrip("#").strip(), "paras": []}
        elif s:
            if s.startswith("> "):
                s = s[2:].strip()
            elif s.startswith("- ") or s.startswith("* "):
                s = "・" + s[2:].strip()
            cur["paras"].append(s)
    if cur["heading"] or cur["paras"]:
        sections.append(cur)
    return sections


def main():
    src = sys.argv[1]
    sections = parse(Path(src).read_text("utf-8"))
    limit = scorer.SECTION_LIMIT
    total = 0.0
    for i, s in enumerate(sections):
        # 「解説」の印は採点の対象にしない（本文の一部としては数える）
        text = "\n".join([s["heading"]] + [p.replace("解説（推論）:", "").replace("解説:", "") for p in s["paras"]])
        s["score"] = scorer.analyze(text)["total"] if text.strip() else 0.0
        s["lines"] = []
        s["id"] = f"s{i}"
        total += s["score"]
        mark = f"  ← {limit}点超" if s["score"] > limit else ""
        print(f"{s['score']:6.1f}  {'  ' * (s['level'] - 1)}{s['heading'] or '（見出しなし）'}{mark}")
    over = sum(1 for s in sections if s["score"] > limit)
    print(f"合計 {total:.1f}点（高いほど難しい）／{len(sections)}セクション／上限 {limit}点を超えたセクション {over}")
    if len(sys.argv) > 2:
        out, title = sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "読みやすく組み直した文章"
        eyebrow = sys.argv[4] if len(sys.argv) > 4 else ""
        if sections and sections[0]["level"] != 1:
            sections.insert(0, {"level": 1, "id": "top", "heading": title, "paras": [], "lines": [], "score": 0.0})
        data = json.dumps({"eyebrow": eyebrow, "limit": limit, "sections": sections}, ensure_ascii=False).replace("</", "<\\/")
        tpl = (HERE / "blog_template.html").read_text("utf-8")
        Path(out).write_text(tpl.replace("__TITLE__", title).replace("__DATA__", data), "utf-8")
        print(out)


if __name__ == "__main__":
    main()
