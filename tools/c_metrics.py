"""C指標の試作（LLMなし）。「構造を文章にしたときの読みにくさ」のうち2つを機械で数え、仮の重みで1つの点数にする。

1. 文をまたいで前を指す言葉（この・その・これ・そういう・そのため など）の数と、指す先までの距離（文字数。何文前かも出す）
2. まとまり（見出しで区切られたセクション。なければ段落）ごとに、新しく出てくる「読み手の知らない名前・名詞句」の数

点数 = 指示語の負荷（1語ごとに 基本点 + 距離の超過分 × 係数）+ 新出の負荷（まとまりごとに、無料枠を超えた新出の数 × 係数）。高いほど読みにくい。
重みは WEIGHTS（手で決めた仮の値で、根拠はない）。道具は scorer.py と同じ GiNZA と wordfreq。指す先の決め方は仮の規則（RULES）。

使い方: python c_metrics.py 入力.md [--head-sections N] [--head-paragraphs N] [--one-section] [--drop-meta] [--detail] [--json]
  --head-sections N   ## 見出しのまとまりの最初の N 個だけ使う（# の題名は最初のまとまりに含める）
  --head-paragraphs N 見出しがない文章で、最初の N 段落だけ使う
  --one-section       全体を1つのまとまりとして扱う（元の小説のように改行がない文章の代わり）
  --drop-meta         最初の ## 見出し（または --- の行）より前にある箇条書き・表・--- を落とす（出典などのメタ情報）
"""

import json
import os
import re
import sys

import ginza

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scorer  # noqa: E402

# どの数字も仮の値で、根拠はない。変数として扱う。
WEIGHTS = {
    "anaphora_base": 1.0,        # 文をまたぐ指示語1つあたりの基本点
    "anaphora_free_chars": 30,   # 指す先までの文字数がこれを超えた分 × 係数
    "anaphora_per_char": 0.1,
    "anaphora_cap_chars": 200,   # 距離はここまでで打ち止め（指す先の誤認で遠くに飛んだ1語が全体を左右しないように）
    "new_free_items": 3,         # 1まとまりで新しく出る「知らない名前・名詞句」の無料枠
    "new_per_item": 3.0,         # 無料枠を超えた1つあたり
    "unknown_zipf": 4.0,         # 出現頻度(Zipf値)がこれ未満なら「読み手の知らない語」。scorer.WEIGHTS["word_zipf_ok"] と同じ
}
# C の総量がこれ以上なら、書き換えのときに C の直し（指示語を名詞に戻す・新出の多いまとまりを分ける）を組み込む。根拠はない仮の値
C_TRIGGER = 20.0

RULES = """指す先の決め方（仮の規則）:
- 数える語: 連体詞 この/その/こんな/そんな（＋名詞）、代名詞 これ/それ/これら/それら/ここ/そこ、
  副詞 そう/こう（そういう・こういう・そうして・そう噂された を含む）、接続 そのため/このため。
  数えない: あ系（あの・あれ。読み手と共有済みの知識を指す）、固定表現（その後・そのまま・その他 など）、そして。
- 連体詞＋名詞: 係り先の名詞が同じ文の前方にあれば「文内」（またがないので数えない）。
  なければ前の文を1文ずつさかのぼり、その名詞を含む最初の文の、その名詞の位置までを距離（文字数）にする。
  見つからなければ「推定」で、前の文の真ん中までを距離にする。
  係り先が形式名詞（こと・もの など）や名詞以外なら、前の文の内容を指すとみなし、前の文の真ん中までを距離にする。
- 代名詞: 文頭の文節にあれば前の文（真ん中まで）。文の途中なら「文内」。
- 副詞 そう/こう・接続 そのため: 前の文（真ん中まで）。
- 見出しは文に数えない（距離にも入れない）。ふりがな「（ひらがな）」は除いてから解析する。
新出の数え方:
- 名詞の連なり（名詞・固有名詞・数詞・「・」）ごとに、文章全体で初めて出たまとまりで数える。
- 固有名詞（GiNZA の PROPN・固有名詞タグ・名前らしい固有表現）は、その部分（「降矢木家」→「降矢木」）を鍵にし、
  丸ごと頻度辞書にあれば既知（日本・アジア）、なければ知らない名前とする。
- それ以外の名詞句は、scorer.word_zipf（複合語は部品に分けて最も難しい部品で代表）が unknown_zipf 未満なら知らない語とする。
- 数詞を含む連なり、形式名詞・接辞だけの連なりは数えない。"""

DET_WORDS = {"この", "その", "こんな", "そんな"}
PRON_WORDS = {"これ", "それ", "これら", "それら", "ここ", "そこ"}
ADV_WORDS = {"そう", "こう"}
CONJ_HEADS = {"ため"}
FIXED_HEADS = {"後", "まま", "他", "ほか", "うえ", "上", "もの", "際", "辺", "都度", "分", "度", "日々"}
FORMAL_NOUNS = {"こと", "もの", "よう", "ため", "とも", "ほど", "まま", "後", "中", "うち", "わけ", "はず",
                "つもり", "の", "際", "ところ", "方", "つ", "たち", "点", "一"}
RUN_POS = {"NOUN", "PROPN", "NUM"}
# GiNZA の固有表現ラベルのうち、名前として扱うもの（数・時・職名などは外す）
NAME_LABEL_RE = re.compile(
    r"Person|Organization|Company|Political|Facility|Location|Region|Province|City|Country|GPE|Museum|Theater|"
    r"Park|School|Station|Worship|Movie|Book|Magazine|Game|Music|Show|Religion|Ethnic|Nationality|Event|Era|"
    r"Dynasty|God|Character|Castle|Spa|Line|Road|Bridge|River|Mountain|Sea|Bay|Lake|Island|Astral|Star|Planet|Continental")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FURIGANA_RE = re.compile(r"（[ぁ-ゖー]+）")
META_LINE_RE = re.compile(r"^\s*(?:[-*+]\s|\||---\s*$)")


def drop_meta(text):
    """最初の ## 見出し（または --- の行）より前にある箇条書き・表・--- を落とす。"""
    out, before = [], True
    for l in text.splitlines():
        m = HEADING_RE.match(l)
        if before and ((m and len(m.group(1)) >= 2) or l.strip() == "---"):
            before = False
            if l.strip() == "---":
                continue
        if before and META_LINE_RE.match(l):
            continue
        out.append(l)
    return "\n".join(out)


def parse_sections(text, head_sections=None, head_paragraphs=None, one_section=False, meta=False):
    """Markdown をまとまりに分ける。## で区切り、# の題名は最初のまとまりの見出しに含める。最初の ## より前の本文は「導入」のまとまりにする。
    ## がなければ空行で区切った段落ごと（空行がなければ改行ごと）。one_section なら全体を1つにする。"""
    if meta:
        text = drop_meta(text)
    text = FURIGANA_RE.sub("", text)
    lines = text.splitlines()
    has_h2 = any((m := HEADING_RE.match(l)) and len(m.group(1)) >= 2 for l in lines)
    sections = []
    if has_h2:
        title_lines, cur = [], None
        for l in lines:
            m = HEADING_RE.match(l)
            if m and len(m.group(1)) == 1:
                title_lines.append(m.group(2).strip())
                continue
            if m:
                cur = {"title": m.group(2).strip(), "headings": [m.group(2).strip()], "lines": []}
                sections.append(cur)
                continue
            if cur is None:
                # 最初の ## より前にある本文は、題名ではなく「導入」のまとまりとして数える
                if l.strip():
                    cur = {"title": "導入", "headings": [], "lines": []}
                    sections.append(cur)
                    cur["lines"].append(l)
                continue
            cur["lines"].append(l)
        if head_sections:
            sections = sections[:head_sections]
        if sections and title_lines:
            sections[0]["headings"] = title_lines + sections[0]["headings"]
    else:
        body = text.strip()
        paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
        if len(paras) == 1 and "\n" in body:
            paras = [p for p in body.splitlines() if p.strip()]
        if head_paragraphs:
            paras = paras[:head_paragraphs]
        sections = [{"title": f"段落{i + 1}", "headings": [], "lines": p.splitlines()} for i, p in enumerate(paras)]
    if one_section:
        merged = {"title": "全体", "headings": [h for s in sections for h in s["headings"]], "lines": []}
        for s in sections:
            merged["lines"].extend(s["lines"] + [""])
        sections = [merged]
    for s in sections:
        s["sentences"] = scorer.split_sentences(scorer.strip_markdown("\n".join(s["lines"])))
    return sections


# ---- 1. 文をまたいで前を指す言葉 ----

def _dist_to_prev_middle(texts, si, offset):
    return (len(texts[si - 1]) // 2 if si > 0 else 0) + offset


def find_anaphora(sent_docs):
    """文の Doc のリスト（見出しは含めない）から、指示語ごとに {sent, text, kind, status, back, dist} を返す。
    status: 解決（指す語が前の文にあった）/ 推定（見つからず前の文とみなした）/ 規則（種類により前の文）/ 文内（またがない）
    back: 何文前か。dist: 指す先までの文字数。"""
    items = []
    texts = [d.text for d in sent_docs]
    for si, doc in enumerate(sent_docs):
        spans = list(ginza.bunsetu_spans(doc))
        first_end = spans[0].end if spans else 0
        for t in doc:
            if t.text in DET_WORDS and (t.pos_ == "DET" or "連体詞" in t.tag_):
                h = t.head
                if h.text in FIXED_HEADS:
                    continue
                gap = h.idx - (t.idx + len(t.text))
                phrase = doc.text[t.idx:h.idx + len(h.text)] if 0 <= gap <= 8 else t.text + "…" + h.text
                if h.text in CONJ_HEADS:
                    items.append(dict(sent=si, text=phrase, kind="接続", status="規則", back=1, dist=_dist_to_prev_middle(texts, si, t.idx)))
                elif h.text in FORMAL_NOUNS or h.pos_ not in ("NOUN", "PROPN"):
                    items.append(dict(sent=si, text=phrase, kind="連体詞", status="規則", back=1, dist=_dist_to_prev_middle(texts, si, t.idx)))
                elif h.text in doc.text[:t.idx]:
                    items.append(dict(sent=si, text=phrase, kind="連体詞", status="文内", back=0, dist=0))
                else:
                    for d in range(1, si + 1):
                        pos = texts[si - d].rfind(h.text)
                        if pos >= 0:
                            dist = (len(texts[si - d]) - pos) + sum(len(x) for x in texts[si - d + 1:si]) + t.idx
                            items.append(dict(sent=si, text=phrase, kind="連体詞", status="解決", back=d, dist=dist, key=h.text))
                            break
                    else:
                        items.append(dict(sent=si, text=phrase, kind="連体詞", status="推定", back=1, dist=_dist_to_prev_middle(texts, si, t.idx), key=h.text))
            elif t.text in PRON_WORDS and t.pos_ == "PRON":
                if t.i < first_end:
                    items.append(dict(sent=si, text=t.text, kind="代名詞", status="規則", back=1, dist=_dist_to_prev_middle(texts, si, t.idx)))
                else:
                    items.append(dict(sent=si, text=t.text, kind="代名詞", status="文内", back=0, dist=0))
            elif t.text in ADV_WORDS and t.pos_ == "ADV":
                nxt = doc[t.i + 1].text if t.i + 1 < len(doc) else ""
                items.append(dict(sent=si, text=t.text + nxt, kind="副詞", status="規則", back=1, dist=_dist_to_prev_middle(texts, si, t.idx)))
    for it in items:
        if it["status"] == "文内":
            it["points"] = 0.0
        else:
            d = min(it["dist"], WEIGHTS["anaphora_cap_chars"])
            it["points"] = WEIGHTS["anaphora_base"] + max(0, d - WEIGHTS["anaphora_free_chars"]) * WEIGHTS["anaphora_per_char"]
    return items


# ---- 2. まとまりごとの新出の名前・名詞句 ----

def noun_runs(doc):
    """名詞の連なり（名詞・固有名詞・数詞・「・」）ごとに {surface, names, is_phrase} を返す。
    names: 連なりの中の固有名詞の部分（PROPN、固有名詞タグ、名前らしい固有表現）。
    is_phrase: 数詞を含まず、形式名詞・接辞以外の語を含む連なり（名詞句として数える）。"""
    ent_tok = set()
    for e in doc.ents:
        if NAME_LABEL_RE.search(e.label_) and not any(t.pos_ == "NUM" for t in e):
            ent_tok.update(range(e.start, e.end))

    def is_name(t):
        return t.pos_ == "PROPN" or "固有名詞" in t.tag_ or t.i in ent_tok

    runs = []
    toks = list(doc)
    i = 0
    while i < len(toks):
        if toks[i].pos_ not in RUN_POS:
            i += 1
            continue
        j = i + 1
        while j < len(toks):
            if toks[j].pos_ in RUN_POS:
                j += 1
            elif toks[j].text == "・" and j + 1 < len(toks) and toks[j + 1].pos_ in RUN_POS:
                j += 2
            else:
                break
        run = toks[i:j]
        names, cur = [], ""
        for k, t in enumerate(run):
            if is_name(t):
                cur += t.text
            elif t.text == "・" and cur and k + 1 < len(run) and is_name(run[k + 1]):
                cur += "・"
            elif cur:
                names.append(cur)
                cur = ""
        if cur:
            names.append(cur)
        has_num = any(t.pos_ == "NUM" for t in run)
        content = [t for t in run if t.text not in FORMAL_NOUNS and t.text != "・" and t.pos_ != "NUM"
                   and not t.tag_.startswith(("接尾辞", "接頭辞")) and any(c.isalpha() for c in t.text)]
        runs.append({"surface": "".join(t.text for t in run), "names": names, "is_phrase": bool(content) and not has_num})
        i = j
    return runs


def _unknown_name(name):
    z = scorer._zipf(name)
    return z is None or z < WEIGHTS["unknown_zipf"]


def _unknown_phrase(surface):
    z, _ = scorer.word_zipf(surface, surface)
    return z is None or z < WEIGHTS["unknown_zipf"]


def analyze(text, head_sections=None, head_paragraphs=None, one_section=False, meta=False):
    sections = parse_sections(text, head_sections, head_paragraphs, one_section, meta)
    nlp = scorer.nlp()
    seen_names, seen_phrases = set(), set()
    all_sent_docs = []
    out_sections = []
    for s in sections:
        head_docs = list(nlp.pipe(s["headings"]))
        sent_docs = list(nlp.pipe(s["sentences"]))
        all_sent_docs.extend(sent_docs)
        new_names, new_phrases, new_unknown = [], [], []
        for doc in head_docs + sent_docs:
            for r in noun_runs(doc):
                for nm in r["names"]:
                    if nm not in seen_names:
                        seen_names.add(nm)
                        new_names.append(nm)
                        if _unknown_name(nm):
                            new_unknown.append(nm)
                if r["is_phrase"] and r["surface"] not in seen_phrases:
                    seen_phrases.add(r["surface"])
                    new_phrases.append(r["surface"])
                    if not r["names"] and _unknown_phrase(r["surface"]):
                        new_unknown.append(r["surface"])
        points = max(0, len(new_unknown) - WEIGHTS["new_free_items"]) * WEIGHTS["new_per_item"]
        out_sections.append({
            "title": s["title"],
            "n_sentences": len(sent_docs),
            "chars": sum(len(x) for x in s["sentences"]),
            "new_names": new_names,
            "new_phrases": new_phrases,
            "new_unknown": new_unknown,
            "points": round(points, 1),
        })
    items = find_anaphora(all_sent_docs)
    cross = [it for it in items if it["status"] != "文内"]
    within = [it for it in items if it["status"] == "文内"]
    dists = [it["dist"] for it in cross]
    anaphora_points = sum(it["points"] for it in items)
    new_points = sum(s["points"] for s in out_sections)
    total_chars = sum(s["chars"] for s in out_sections)
    total = anaphora_points + new_points
    return {
        "total": round(total, 1),
        "score_per100": round(total / total_chars * 100, 1) if total_chars else 0.0,
        "components": {"anaphora": round(anaphora_points, 1), "new": round(new_points, 1)},
        "chars": total_chars,
        "n_sentences": len(all_sent_docs),
        "n_sections": len(out_sections),
        "anaphora": {
            "cross": len(cross),
            "within": len(within),
            "mean_back": round(sum(it["back"] for it in cross) / len(cross), 2) if cross else 0.0,
            "mean_dist": round(sum(dists) / len(dists), 1) if dists else 0.0,
            "max_dist": max(dists) if dists else 0,
            "estimated": sum(1 for it in cross if it["status"] == "推定"),
            "resolved": sum(1 for it in cross if it["status"] == "解決"),
            "items": items,
        },
        "sections": out_sections,
        "unknown_total": sum(len(s["new_unknown"]) for s in out_sections),
        "unknown_max": max((len(s["new_unknown"]) for s in out_sections), default=0),
        "names_total": sum(len(s["new_names"]) for s in out_sections),
        "phrases_total": sum(len(s["new_phrases"]) for s in out_sections),
        "weights": WEIGHTS,
    }


def _print(r, detail=False):
    a = r["anaphora"]
    print(f"C の総量: {r['total']} 点（高いほど読みにくい）  補助: {r['score_per100']} 点/100字 / {r['chars']} 字 / {r['n_sentences']} 文 / {r['n_sections']} まとまり")
    print(f"C の直し: {'必要（' + str(C_TRIGGER) + '点以上）' if r['total'] >= C_TRIGGER else '不要（' + str(C_TRIGGER) + '点未満）'}")
    print(f"内訳: 指示語 {r['components']['anaphora']}、新出 {r['components']['new']}")
    print(f"1. 文をまたいで前を指す言葉: {a['cross']} 個（文内は {a['within']} 個、数えない）"
          f"  平均 {a['mean_back']} 文前・{a['mean_dist']} 字前  最大 {a['max_dist']} 字"
          f"  内訳: 指す語が前の文にあった {a['resolved']}、推定 {a['estimated']}、種類による規則 {a['cross'] - a['resolved'] - a['estimated']}")
    if detail:
        for it in a["items"]:
            key = f"（指す語: {it['key']}）" if "key" in it else ""
            print(f"   文{it['sent'] + 1} 「{it['text']}」 {it['kind']} {it['status']} {it['back']}文前・{it['dist']}字前 → {it['points']:.1f}点{key}")
    print(f"2. まとまりごとの新出（知らない名前・名詞句）  合計 {r['unknown_total']}  1まとまりの最大 {r['unknown_max']}"
          f"  （参考: 名前 {r['names_total']}、名詞句 {r['phrases_total']}）")
    for i, s in enumerate(r["sections"]):
        print(f"   [{i + 1}] {s['title'][:30]}（{s['n_sentences']}文・{s['chars']}字）: 知らない {len(s['new_unknown'])} → {s['points']}点"
              f"（名前 {len(s['new_names'])} / 名詞句 {len(s['new_phrases'])}）")
        if detail:
            print(f"       知らない: {'、'.join(s['new_unknown']) or 'なし'}")


if __name__ == "__main__":
    args = sys.argv[1:]

    def take(flag, cast=int):
        if flag in args:
            i = args.index(flag)
            v = cast(args[i + 1])
            del args[i:i + 2]
            return v
        return None

    head_sections = take("--head-sections")
    head_paragraphs = take("--head-paragraphs")
    one_section = "--one-section" in args
    meta = "--drop-meta" in args
    detail = "--detail" in args
    as_json = "--json" in args
    args = [a for a in args if not a.startswith("--")]
    text = open(args[0], encoding="utf-8").read() if args else sys.stdin.read()
    result = analyze(text, head_sections, head_paragraphs, one_section, meta)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        _print(result, detail)
