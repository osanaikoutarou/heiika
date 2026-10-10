"""意味のプログラム化（rikai 形式）の検証と表示。

使い方:
  python rikai.py check  変換例/ai.json   形式・引用・参照を検証し、元文のどこを写し損ねたかを出す
  python rikai.py render 変換例/ai.json   人が読むためのプログラム風の表示にする
  python rikai.py html   変換例/ai.json 出力.html [見出し]   スマホでも読めるページにする
  python rikai.py blog   変換例/ai.json   見出し＝要旨、本文＝中身のブログ型（Markdown）にする
  python rikai.py init   元文.txt 出力.json [話者] [読み手]   元文をそのまま入れた JSON のひな形を作る
  python rikai.py init   元文.txt 出力.json [話者] [読み手] --light   軽モード: 文に番号を振り、見出し・段落ごとのまとまりを機械で先に作る
  python rikai.py sentences 変換例/ai.json   番号付きの文を表示する（軽モードの引用は「#12」「#12-15」「#12:冒頭の語」で書く）

軽モード（mode: light）: 引用は文番号で書ける（書き写しが減る）。まとまりは見出し・段落で機械が先に切る。
imports・states・versions・relations は任意（組み直しに使うのは、まとまりと主張だけ）。
"""

import json
import re
import sys

FORMAT = "rikai/0.1"
CLAIM_TYPES = {"断定", "推測", "伝聞", "感想", "提案", "依頼", "質問", "決定", "規範", "許可", "定義"}
RELATION_TYPES = {"理由", "対比", "例", "前提", "結果", "並列", "言い換え"}
LONG_QUOTE = 40  # 主張以外の要素の引用がこれより長いと、写し損ねを隠しやすい
ANALYSIS_TYPES = {"穴", "矛盾", "間違い", "メモ"}
CTX_KEYS = {"version", "env", "scope", "time", "viewpoint", "speaker"}
# 要旨の出どころ: 元文にある／元文の内容を縮めた／元文にない内容を含む
GIST_SOURCES = {"元文", "要約", "推論"}
NODE_FIELDS = {
    "claim": {"text", "type"},
    "seq": {"steps"},
    "cond": {"when", "then"},
    "group": {"name", "body"},
    "choice": {"options"},
    "compare": {"targets", "axes"},
    "quote_of": {"speaker", "body"},
    "unknown": {"about"},
}
CHILD_LISTS = ("steps", "then", "else", "body")
# 写し損ねの判定で無視する文字（句読点・記号・空白）
IGNORABLE = re.compile(r"[\s、。，．,.・:：「」『』（）()!?！？\-]")


def _norm(s):
    return re.sub(r"\s+", "", s)


SENT_END = "。！？"
BRACKETS = {"（": "）", "(": ")", "「": "」", "『": "』", "【": "】"}


def split_sentences(text):
    """「。！？」と改行で文を区切る（括弧の中では区切らない）。scorer.py と同じ規則。"""
    out, buf, depth = [], "", 0
    closers = set(BRACKETS.values())
    for c in text:
        if c == "\n":
            if buf.strip():
                out.append(buf.strip())
            buf, depth = "", 0
            continue
        buf += c
        if c in BRACKETS:
            depth += 1
        elif c in closers and depth > 0:
            depth -= 1
        elif c in SENT_END and depth == 0:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


REF = re.compile(r"^#(\d+)(?:-(\d+))?(?::(.*))?$")


def resolve_quote(q, doc):
    """引用を、元文の文字列の一覧に直す。文番号の引用（#12, #12-15, #12:冒頭の語）は sentences から引く。
    返り値: (文字列の一覧, エラー文)。"""
    m = REF.match(q.strip()) if isinstance(q, str) else None
    if not m:
        return [q], None
    sents = doc.get("sentences")
    if not sents:
        return [], f"文番号の引用「{q}」があるが、sentences がない（init --light で作る）"
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if not (1 <= a <= b <= len(sents)):
        return [], f"引用「{q}」の文番号が範囲外（1〜{len(sents)}）"
    if m.group(3) is not None:
        part = m.group(3).strip()
        if b != a:
            return [], f"引用「{q}」: 範囲と冒頭の語は同時に使えない"
        if not part or _norm(part) not in _norm(sents[a - 1]):
            return [], f"引用「{q}」の語が文{a}にない"
        return [part], None
    return [sents[i - 1] for i in range(a, b + 1)], None


def walk(nodes, path="body"):
    for i, n in enumerate(nodes or []):
        p = f"{path}[{i}]"
        yield n, p
        for key in CHILD_LISTS:
            if key in n:
                yield from walk(n[key], f"{p}.{key}")


def all_elements(doc):
    for key in ("imports", "states", "versions"):
        for i, e in enumerate(doc.get(key, [])):
            yield e, f"{key}[{i}]", key
    for n, p in walk(doc.get("body", [])):
        yield n, p, "body"
    for i, r in enumerate(doc.get("relations", [])):
        yield r, f"relations[{i}]", "relations"


def check(doc):
    errors, warns = [], []
    src = doc.get("source", "")
    if doc.get("mode") == "light":
        # 軽モードでは見出し行はまとまりの名前になるので、写す対象から外す
        src = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    nsrc = _norm(src)
    if doc.get("format") != FORMAT:
        errors.append(f"format は {FORMAT} にしてください")
    if not src:
        errors.append("source（元文）がありません")

    ids = {}
    for e, p, _ in all_elements(doc):
        if "id" in e:
            if e["id"] in ids:
                errors.append(f"{p}: id {e['id']} が重複（{ids[e['id']]} と）")
            ids[e["id"]] = p
    for i, a in enumerate(doc.get("analysis", [])):
        if "id" in a:
            ids.setdefault(a["id"], f"analysis[{i}]")

    def check_quotes(e, p, required=True):
        qs = e.get("quotes", [])
        if required and not qs:
            errors.append(f"{p}: 引用(quotes)がない。元文に根拠がないなら analysis に移す")
        for q in qs:
            strs, err = resolve_quote(q, doc)
            if err:
                errors.append(f"{p}: {err}")
                continue
            for s in strs:
                if not _norm(s) or _norm(s) not in nsrc:
                    errors.append(f"{p}: 引用が元文に見つからない「{q}」")

    def check_gist(g, p):
        if not g.get("text"):
            errors.append(f"{p}: text がない")
        if g.get("source") not in GIST_SOURCES:
            errors.append(f"{p}: source は {sorted(GIST_SOURCES)} のどれか")
        if g.get("source") == "元文":
            check_quotes(g, p)

    def ref_ok(r, p, field):
        if isinstance(r, list):
            if len(r) < 2:
                errors.append(f"{p}.{field}: 候補の配列は2つ以上の id を入れる")
            for x in r:
                ref_ok(x, p, field)
            return
        if r not in ids:
            errors.append(f"{p}.{field}: 参照先 {r} がない")

    def subtree_ids(node):
        out = set()
        for n, _ in walk([node]):
            if "id" in n:
                out.add(n["id"])
        return out

    def check_gist_about(g, p, allowed):
        if "about" not in g:
            warns.append(f"{p}: about（要約した要素の id）がない。要旨が元の要素に忠実かを機械で確かめられない")
            return
        for x in g["about"]:
            if x not in ids:
                errors.append(f"{p}.about: 参照先 {x} がない")
            elif allowed is not None and x not in allowed:
                errors.append(f"{p}.about: {x} はこのまとまりの中にない")

    if "gist" in doc:
        check_gist(doc["gist"], "gist")
        if "about" in doc["gist"]:
            check_gist_about(doc["gist"], "gist", None)
    for i, t in enumerate(doc.get("senses", [])):
        p = f"senses[{i}]"
        if not t.get("term"):
            errors.append(f"{p}: term がない")
        for j, sense in enumerate(t.get("senses", [])):
            if not sense.get("meaning"):
                errors.append(f"{p}.senses[{j}]: meaning がない")
            for x in sense.get("ids", []):
                ref_ok(x, f"{p}.senses[{j}]", "ids")

    for e, p, section in all_elements(doc):
        if "id" not in e:
            errors.append(f"{p}: id がない")
        check_quotes(e, p)
        if section in ("imports", "states", "versions") and "name" not in e:
            errors.append(f"{p}: name がない")
        if section == "relations":
            if e.get("type") not in RELATION_TYPES:
                errors.append(f"{p}: type は {sorted(RELATION_TYPES)} のどれか")
            for f in ("from", "to"):
                for r in e.get(f, []):
                    ref_ok(r, p, f)
            continue
        if section != "body":
            continue
        kind = e.get("kind")
        if kind not in NODE_FIELDS:
            errors.append(f"{p}: kind「{kind}」は使えない（{sorted(NODE_FIELDS)}）")
            continue
        for f in NODE_FIELDS[kind]:
            if f not in e:
                errors.append(f"{p}: {kind} には {f} が必要")
        if kind == "claim" and e.get("type") not in CLAIM_TYPES:
            errors.append(f"{p}: claim.type は {sorted(CLAIM_TYPES)} のどれか")
        for f in ("options", "targets", "about", "overrides"):
            if isinstance(e.get(f), list):
                for r in e[f]:
                    ref_ok(r, p, f)
        ctx = e.get("ctx", {})
        for k, v in ctx.items():
            if k not in CTX_KEYS:
                errors.append(f"{p}.ctx: {k} は使えない（{sorted(CTX_KEYS)}）")
            if k == "version" and v not in ids:
                errors.append(f"{p}.ctx.version: 参照先 {v} がない")
        if "gist" in e:
            check_gist(e["gist"], f"{p}.gist")
            check_gist_about(e["gist"], f"{p}.gist", subtree_ids(e))
        if kind == "claim" and "mention" in e and not isinstance(e["mention"], bool):
            errors.append(f"{p}: mention は true/false")

    for i, a in enumerate(doc.get("analysis", [])):
        p = f"analysis[{i}]"
        if a.get("type") not in ANALYSIS_TYPES:
            errors.append(f"{p}: type は {sorted(ANALYSIS_TYPES)} のどれか")
        for r in a.get("about", []):
            ref_ok(r, p, "about")
        check_quotes(a, p, required=False)

    # 主張以外の要素の長い引用は、主張の写し損ねを隠す（検証が気づけなくなる）
    quotes_by_elem = [(e.get("id", p), e.get("kind"), [_norm(q) for q in e.get("quotes", []) if not REF.match(q)]) for e, p, sec in all_elements(doc) if sec == "body"]
    for eid, kind, qs in quotes_by_elem:
        if kind == "claim":
            continue
        for q in qs:
            if len(q) > LONG_QUOTE:
                warns.append(f"{eid}: 引用が{LONG_QUOTE}字を超えている。まとまり・比較・選択肢の引用は目印の語だけにする")
            elif any(oq and oq in q and oid != eid for oid, _, oqs in quotes_by_elem for oq in oqs):
                warns.append(f"{eid}: 引用が他の要素の引用を丸ごと含んでいる。短くする")

    # 変換部分（analysis 以外）がどれだけ元文を写したか
    conv_covered = [False] * len(nsrc)
    light = doc.get("mode") == "light"
    for e, _, _ in all_elements(doc):
        if light and e.get("kind") == "group":
            continue  # 軽モードのまとまりは範囲の引用なので、主張の写し損ねを隠さないよう数えない
        for q in e.get("quotes", []):
            strs, _err = resolve_quote(q, doc)
            for s in strs:
                _mark(nsrc, _norm(s), conv_covered)
    gaps, cur = [], ""
    meaningful = 0
    hit = 0
    for ch, c in zip(nsrc, conv_covered):
        if IGNORABLE.match(ch):
            if cur:
                cur += ch
            continue
        meaningful += 1
        if c:
            hit += 1
            if cur.strip():
                gaps.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        gaps.append(cur)
    gaps = [g.strip("、。 ") for g in gaps if len(IGNORABLE.sub("", g)) >= 2]
    coverage = hit / meaningful * 100 if meaningful else 0.0
    return errors, warns, coverage, gaps


def _mark(nsrc, nq, covered):
    pos = nsrc.find(nq) if nq else -1
    while pos >= 0:
        for k in range(pos, pos + len(nq)):
            covered[k] = True
        pos = nsrc.find(nq, pos + 1)


def _q(e):
    return "「" + " / ".join(e.get("quotes", [])) + "」" if e.get("quotes") else ""


def render(doc):
    out = []
    meta = doc.get("meta", {})
    if meta:
        out.append("// " + "  ".join(f"{k}: {v}" for k, v in meta.items()))
        out.append("")
    by_domain = {}
    for imp in doc.get("imports", []):
        by_domain.setdefault(imp.get("domain", "一般"), []).append(imp["name"])
    for d, names in by_domain.items():
        out.append(f"import {{ {', '.join(names)} }} from {d}")
    if by_domain:
        out.append("")
    for st in doc.get("states", []):
        out.append(f"state {st['name']}" + (f"   // {st['desc']}" if st.get("desc") else ""))
    for v in doc.get("versions", []):
        out.append(f"version {v['name']}")
    if doc.get("states") or doc.get("versions"):
        out.append("")
    names = {}
    for e, _, _ in all_elements(doc):
        if "id" in e:
            if e.get("kind") == "cond":
                names[e["id"]] = f"if {e['when']}"
            elif e.get("kind") == "quote_of":
                names[e["id"]] = f"{e['speaker']} の言うこと"
            elif e.get("kind") == "unknown":
                names[e["id"]] = f"??? {e['about']}"
            elif e.get("kind") == "seq":
                names[e["id"]] = "順番に（" + " → ".join(st.get("text", st["id"]) for st in e.get("steps", [])) + "）"
            else:
                names[e["id"]] = e.get("name") or e.get("text") or e["id"]

    for e, _, _ in all_elements(doc):
        if e.get("kind") == "compare":
            names[e["id"]] = "比較（" + ", ".join(names.get(t, t) for t in e.get("targets", [])) + "）"

    def rname(x):
        return "（" + "／".join(names.get(y, y) for y in x) + " のどれか）" if isinstance(x, list) else names.get(x, x)

    def ctx_str(n):
        ctx = n.get("ctx", {})
        if not ctx:
            return ""
        parts = []
        for k, v in ctx.items():
            parts.append(f"{k}={names.get(v, v) if k == 'version' else v}")
        return "  [" + ", ".join(parts) + "]"

    def node(n, ind, num=None):
        pad = "  " * ind
        lead = f"{num}. " if num is not None else ""
        k = n["kind"]
        if k == "claim":
            who = f"(話者={n['speaker']})" if n.get("speaker") else ""
            men = "〈例として〉" if n.get("mention") else ""
            out.append(f"{pad}{lead}@{n['type']}{who} {men}{n['text']}{ctx_str(n)}")
        elif k == "seq":
            out.append(f"{pad}{lead}順番に{ctx_str(n)}:")
            for i, s in enumerate(n["steps"], 1):
                node(s, ind + 1, i)
        elif k == "cond":
            label = "もし（仮定）" if n.get("hypothetical") else "if"
            ov = f"   // ただし書き: {', '.join(names.get(r, r) for r in n['overrides'])} より優先" if n.get("overrides") else ""
            out.append(f"{pad}{lead}{label} {n['when']}{ctx_str(n)}:{ov}")
            for s in n["then"]:
                node(s, ind + 1)
            if n.get("else"):
                out.append(f"{pad}else:")
                for s in n["else"]:
                    node(s, ind + 1)
        elif k == "group":
            out.append(f"{pad}{lead}fn {n['name']}{ctx_str(n)} {{")
            for s in n["body"]:
                node(s, ind + 1)
            out.append(f"{pad}}}")
        elif k == "choice":
            out.append(f"{pad}{lead}選択肢 {n.get('name', '')}: " + " / ".join(names.get(r, r) for r in n["options"]))
        elif k == "compare":
            out.append(f"{pad}{lead}compare " + ", ".join(names.get(r, r) for r in n["targets"]) + " by [" + ", ".join(n["axes"]) + "]")
        elif k == "quote_of":
            out.append(f"{pad}{lead}{n['speaker']} が言うには{ctx_str(n)}:")
            for s in n["body"]:
                node(s, ind + 1)
        elif k == "unknown":
            out.append(f"{pad}{lead}??? {n['about']}   // 元文の時点で未確定・曖昧 {_q(n)}")

    for n in doc.get("body", []):
        node(n, 0)
    rels = doc.get("relations", [])
    if rels:
        out.append("")
        out.append("// ===== 関係 =====")
        for r in rels:
            f = ", ".join(rname(x) for x in r["from"])
            t = ", ".join(rname(x) for x in r["to"])
            out.append(f"{r['type']}: {f} → {t}")
    if doc.get("senses"):
        out.append("")
        out.append("// ===== 同じ語の別の意味 =====")
        for t in doc["senses"]:
            out.append(f"// {t['term']}: " + " ／ ".join(f"({i}) {x['meaning']}" for i, x in enumerate(t["senses"], 1)))
    an = doc.get("analysis", [])
    if an:
        out.append("")
        out.append("// ===== 分析（元文に根拠のない判断。変換とは別） =====")
        for a in an:
            about = f"（{', '.join(names.get(x, x) for x in a.get('about', []))}）" if a.get("about") else ""
            out.append(f"// [{a['type']}]{about} {a['text']}")
    return "\n".join(out)


def to_html(doc, heading):
    from pathlib import Path
    _, _, coverage, _ = check(doc)
    nodes = [n for n, _ in walk(doc.get("body", []))]
    stats = {
        "heading": heading,
        "chars": len(_norm(doc.get("source", ""))),
        "claims": sum(1 for n in nodes if n.get("kind") == "claim"),
        "groups": sum(1 for n in nodes if n.get("kind") == "group"),
        "unknowns": sum(1 for n in nodes if n.get("kind") == "unknown"),
        "coverage": round(coverage),
    }
    safe = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    tpl = (Path(__file__).parent / "viewer_template.html").read_text("utf-8")
    return tpl.replace("__TITLE__", heading).replace("__DATA__", safe(doc)).replace("__STATS__", safe(stats))


REL_PREFIX = {"理由": "だから、", "結果": "つまり、", "対比": "一方、", "前提": "前提として、", "例": "例えば、"}


def blog(doc, max_level=None):
    """JSON をブログ型のセクション列にする。見出しは要旨、本文はまとまりの直下の中身。
    max_level より深いまとまりは、セクションにせず本文の中に小見出し付きの箇条書きとして入れる。"""
    vname = {v["id"]: v["name"] for v in doc.get("versions", [])}
    incoming = {}
    for r in doc.get("relations", []):
        if any(isinstance(x, list) for x in r["from"] + r["to"]):
            continue  # 指す先が曖昧な関係は、接続語を付けない
        for t in r["to"]:
            incoming.setdefault(t, []).append(r)
    rendered = set()
    sections = []

    def heading(n):
        g = n.get("gist")
        if not g:
            return n.get("name", n.get("id", ""))
        return g["text"] + (" 〔推論〕" if g.get("source") == "推論" else "")

    def ctx_suffix(n):
        c = n.get("ctx", {})
        parts = [vname.get(c["version"], c["version"])] if "version" in c else []
        if "time" in c:
            parts.append(c["time"])
        return f"（{'・'.join(parts)}）" if parts else ""

    def lines(n, ind=0):
        pad = "  " * ind
        k = n["kind"]
        if k == "claim":
            pre = ""
            for r in incoming.get(n["id"], []):
                if any(f in rendered for f in r["from"]):
                    pre = REL_PREFIX.get(r["type"], "")
                    break
            who = f"{n['speaker']}: " if n.get("speaker") and n["speaker"] != "書き手" else ""
            rendered.add(n["id"])
            return [f"{pad}・{pre}{who}{n['text']}{ctx_suffix(n)}"]
        rendered.add(n.get("id"))
        if k == "quote_of":
            out = [f"{pad}・{n['speaker']}{ctx_suffix(n)}:"]
            for c in n["body"]:
                out += lines(c, ind + 1)
            return out
        if k == "unknown":
            return [f"{pad}・{n['about']} → 元文には書かれていない"]
        if k == "seq":
            out = []
            for i, st in enumerate(n["steps"], 1):
                out += [l.replace("・", f"{i}. ", 1) if j == 0 else l for j, l in enumerate(lines(st, ind))]
            return out
        if k == "cond":
            out = [f"{pad}・もし{n['when']}:"]
            for c in n["then"]:
                out += lines(c, ind + 1)
            if n.get("else"):
                out.append(f"{pad}・そうでなければ:")
                for c in n["else"]:
                    out += lines(c, ind + 1)
            return out
        if k == "group":
            out = [f"{pad}・{heading(n)}:"]
            for c in n["body"]:
                out += lines(c, ind + 1)
            return out
        return [f"{pad}・{n.get('name') or n.get('about') or k}"]

    def section(n, level):
        body, subs = [], []
        for c in n["body"]:
            as_section = c["kind"] == "group" and (max_level is None or level + 1 <= max_level)
            (subs if as_section else body).append(c)
        out = []
        for c in body:
            out += lines(c)
        h = heading(n)
        out = [l for l in out if l.strip().lstrip("・") != h]  # 見出しと同じ文は本文に重ねない
        sections.append({"level": level, "id": n["id"], "heading": h, "lines": out})
        for c in subs:
            section(c, level + 1)

    lead = []
    top = doc.get("gist", {}).get("text", "")
    first = len(sections)
    sections.append({"level": 1, "id": "_lead", "heading": top, "lines": lead})
    for n in doc.get("body", []):
        if n["kind"] == "group":
            section(n, 2)
        elif n["kind"] == "quote_of" and any(c["kind"] == "group" for c in n["body"]):
            lead.append(f"・以下は、{n['speaker']}{ctx_suffix(n)}の内容")
            rendered.add(n["id"])
            for c in n["body"]:
                if c["kind"] == "group":
                    section(c, 2)
                else:
                    sections[first]["lines"] += lines(c)
        else:
            lead += lines(n)
    return sections


def section_text(sec):
    return "\n".join([sec["heading"]] + [l.strip() for l in sec["lines"]])


def blog_markdown(sections):
    out = []
    for sec in sections:
        out += ["#" * sec["level"] + " " + sec["heading"], ""]
        for l in sec["lines"]:
            ind = (len(l) - len(l.lstrip(" "))) // 2
            out.append("  " * ind + "- " + l.strip().lstrip("・"))
        out.append("")
    return "\n".join(out)


def light_skeleton(src):
    """軽モードのひな形: 文に番号を振り、見出し・段落ごとのまとまりを先に作る。主張は空のまま LLM が埋める。"""
    sentences = []
    groups = []
    cur = None
    under_heading = False  # 見出しの下にいる間は、段落が変わっても同じまとまり
    for raw in src.split("\n"):
        line = raw.strip()
        if not line:
            if not under_heading:
                cur = None
            continue
        if line.startswith("#"):
            name = line.lstrip("#").strip()
            cur = {"id": f"g{len(groups) + 1}", "kind": "group", "name": name, "quotes": [], "body": [], "_first": None}
            groups.append(cur)
            under_heading = True
            continue
        if cur is None:
            cur = {"id": f"g{len(groups) + 1}", "kind": "group", "name": f"段落{len(groups) + 1}", "quotes": [], "body": [], "_first": None}
            groups.append(cur)
        for s in split_sentences(line):
            sentences.append(s)
            if cur["_first"] is None:
                cur["_first"] = len(sentences)
            cur["_last"] = len(sentences)
    body = []
    for g in groups:
        if g["_first"] is None:
            continue
        a, b = g["_first"], g["_last"]
        g["quotes"] = [f"#{a}" if a == b else f"#{a}-{b}"]
        del g["_first"], g["_last"]
        body.append(g)
    return sentences, body


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("check", "render", "html", "blog", "init", "sentences"):
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "init":
        args = [a for a in sys.argv[2:] if a != "--light"]
        light = "--light" in sys.argv
        src = open(args[0], encoding="utf-8").read().strip()
        out = args[1] if len(args) > 1 else args[0].rsplit(".", 1)[0] + ".rikai.json"
        meta = {"speaker": args[2] if len(args) > 2 else "", "listener": args[3] if len(args) > 3 else "", "notes": ""}
        skeleton = {"format": FORMAT, "source": src, "meta": meta, "imports": [], "states": [], "versions": [],
                    "body": [], "relations": [], "analysis": []}
        if light:
            sentences, body = light_skeleton(src)
            skeleton["mode"] = "light"
            skeleton["sentences"] = sentences
            skeleton["body"] = body
        json.dump(skeleton, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(out)
        if light:
            print(f"文 {len(sentences)}、まとまり {len(body)}（見出し・段落で機械が切った）")
        return
    doc = json.load(open(sys.argv[2], encoding="utf-8"))
    if sys.argv[1] == "sentences":
        for i, s in enumerate(doc.get("sentences") or split_sentences(doc.get("source", "")), 1):
            print(f"#{i} {s}")
        return
    if sys.argv[1] == "render":
        print(render(doc))
        return
    if sys.argv[1] == "blog":
        print(blog_markdown(blog(doc)))
        return
    if sys.argv[1] == "html":
        out = sys.argv[3] if len(sys.argv) > 3 else sys.argv[2].replace(".json", ".html")
        heading = sys.argv[4] if len(sys.argv) > 4 else doc.get("meta", {}).get("title", "意味の構造")
        open(out, "w", encoding="utf-8").write(to_html(doc, heading))
        print(out)
        return
    errors, warns, coverage, gaps = check(doc)
    for e in errors:
        print("エラー:", e)
    for w in warns:
        print("注意:", w)
    print(f"写した割合: {coverage:.0f}%（元文のうち、変換部分の引用でカバーされた文字の割合）")
    if gaps:
        print("写していない箇所:")
        for g in gaps:
            print("  -", g)
    print("OK" if not errors else f"エラー {len(errors)} 件")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
