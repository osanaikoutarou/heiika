"""日本語の文章の「難しさ」を点数化する（LLMなし・たたき台）。

点数 = 文ごとの3要素（長さ・単語・係り受けの負荷）の合計。高いほど難しい。
全体は総量（各文の合計）を主とし、100字あたりは補助。重みと閾値は WEIGHTS で調整する。
測っているのは文の形の難しさ（外在的負荷）だけで、中身の難しさや理解できたかは測らない。
"""

import json
import math
import re
import sys
from functools import lru_cache

import ginza
import spacy
import wordfreq
from sudachipy import Dictionary, SplitMode

# どの数字も約10本のサンプルで手で決めた仮の値で、根拠はない。変数として扱う。
WEIGHTS = {
    # 長さ: 文字数がこれを超えた分 × 係数
    "length_free_chars": 40,
    "length_per_char": 0.25,
    # 単語: 出現頻度(Zipf値)がこれ未満の内容語に、不足分 × 係数
    "word_zipf_ok": 4.0,
    "word_per_zipf": 3.0,
    "word_unknown_zipf": 2.5,  # 頻度辞書にない語の扱い。「辞書にない」は「まれ」と同じではないので中間値にする
    "word_repeat_factor": 0.3,  # 2回目以降の登場は既知になったとみなして軽くする
    # 係り受けの負荷: 「係り先がまだ来ていない文節の数」の曲線の、ピーク（主）と面積（従）
    "dep_peak_free": 3,  # ピークがこれを超えた分 × 係数
    "dep_peak_per_item": 5.0,
    "dep_area_per_unit": 0.5,  # (平均依存距離 − 1) × 文節数 × 係数。距離の合計そのものは文の長さとほぼ同じ情報なので使わない
    # 入れ子（同時に待っている係り先の種類数）は表示だけで、点数にはしない
    "nest_free": 2,
}

CONTENT_POS = {"NOUN", "PROPN", "VERB", "ADJ", "ADV"}
CLAUSE_DEPS = {"acl", "advcl", "ccomp", "csubj"}  # 図で節の係り受けを区別するため
# 文を分ける候補: 文節の最後がこれらで終わる節の切れ目
SPLIT_MARK_DEPS = {"advcl", "conj"}
COMPONENTS = ["length", "word", "dependency"]
# 1セクション（1回に読む塊）の上限の目安。変数であり根拠はない。旧尺度の150点は、修正後の尺度では約105点に当たる
SECTION_LIMIT = 105
NUMERAL_CHARS = "一二三四五六七八九十百千万両数何幾"

_nlp = None
_freq = None
_splitter = None


def nlp():
    global _nlp
    if _nlp is None:
        _nlp = spacy.load("ja_ginza")
    return _nlp


def freq():
    global _freq
    if _freq is None:
        _freq = wordfreq.get_frequency_dict("ja", wordlist="best")
    return _freq


def splitter():
    global _splitter
    if _splitter is None:
        _splitter = Dictionary(dict="core").create(SplitMode.A)
    return _splitter


def _zipf(word):
    f = freq().get(word.lower())
    return None if f is None else math.log10(f * 1e9)


@lru_cache(maxsize=50000)
def word_zipf(surface, lemma):
    """語の出現頻度(Zipf値, 高いほどよく使う)。辞書にない複合語は細かく分けて最も難しい部分で代表させる。"""
    found = [z for z in (_zipf(surface), _zipf(lemma)) if z is not None]
    if found:
        return max(found), "辞書"
    parts = [m.surface() for m in splitter().tokenize(surface)]
    if len(parts) > 1:
        zs = [_zipf(p) for p in parts]
        if all(z is not None for z in zs):
            return min(zs), "分割: " + "+".join(parts)
    # 「一度」「三人」など数詞＋語は、頻度辞書側では分かれているので数詞を外して引く
    rest = surface.lstrip(NUMERAL_CHARS)
    if rest and rest != surface:
        z = _zipf(rest)
        if z is not None:
            return z, "数詞を除いて: " + rest
    return None, "辞書になし"


def _is_word_target(token):
    if token.pos_ not in CONTENT_POS:
        return False
    if token.like_num or all(not c.isalpha() for c in token.text):
        return False
    return True


def _word_points(zipf):
    w = WEIGHTS
    z = w["word_unknown_zipf"] if zipf is None else zipf
    return max(0.0, w["word_zipf_ok"] - z) * w["word_per_zipf"]


def _bunsetsu(sent):
    """文節のリストと、各文節の係り先文節index（なければ-1）を返す。"""
    spans = list(ginza.bunsetu_spans(sent))
    owner = {}
    for bi, sp in enumerate(spans):
        for t in sp:
            owner[t.i] = bi
    heads = []
    for bi, sp in enumerate(spans):
        outward = [t for t in sp if t.head.i not in owner or owner[t.head.i] != bi or t.head.i == t.i]
        head_bi = -1
        for t in reversed(outward):
            if t.head.i != t.i and t.head.i in owner:
                head_bi = owner[t.head.i]
                break
        heads.append(head_bi)
    return spans, heads


def _structure(sent):
    """文の構造面（長さ・係り受けの負荷）を計算する。単語は別。"""
    w = WEIGHTS
    spans, heads = _bunsetsu(sent)
    n_chars = len(sent.text.strip())

    arcs = []
    for bi, hb in enumerate(heads):
        if hb < 0:
            continue
        d = abs(hb - bi)
        root_tok = [t for t in spans[bi] if t.head.i not in range(spans[bi].start, spans[bi].end) or t.head.i == t.i]
        rel = root_tok[-1].dep_ if root_tok else ""
        arcs.append({"from": bi, "to": hb, "dist": d, "clause": rel in CLAUSE_DEPS, "dep": rel})
    mdd = sum(a["dist"] for a in arcs) / len(arcs) if arcs else 0.0  # 平均依存距離

    # 各文節を読み終えた時点で、係り先がまだ来ていない文節（pending）と、その待ち先の種類数（depths）。
    # 待ち先が複数種類あるのは、ある構造の途中に別の構造が割り込んでいる状態（入れ子）。
    pending, depths = [], []
    for i in range(len(spans)):
        waiting = [heads[j] for j in range(i + 1) if heads[j] > i]
        pending.append(len(waiting))
        depths.append(len(set(waiting)))
    peak = max(pending) if pending else 0
    max_depth = max(depths) if depths else 0

    dep_peak_points = max(0, peak - w["dep_peak_free"]) * w["dep_peak_per_item"]
    dep_area_points = max(0.0, mdd - 1) * len(spans) * w["dep_area_per_unit"]
    points = {
        "length": max(0, n_chars - w["length_free_chars"]) * w["length_per_char"],
        "dependency": dep_peak_points + dep_area_points,
    }
    return {
        "spans": spans,
        "heads": heads,
        "arcs": arcs,
        "pending": pending,
        "peak": peak,
        "depths": depths,
        "max_depth": max_depth,
        "mdd": mdd,
        "dep_peak_points": dep_peak_points,
        "dep_area_points": dep_area_points,
        "n_chars": n_chars,
        "points": points,
    }


SENT_END = "。！？!?"
BRACKETS = {"「": "」", "『": "』", "（": "）", "(": ")"}


def split_sentences(text):
    """「。！？」と改行だけで文を区切る。括弧の中では区切らない。
    GiNZA の文分割は「案A」のような英字で切れることがあるため使わない。"""
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


MD_PATTERNS = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),        # 見出しの記号
    (re.compile(r"^\s*>\s?", re.M), ""),                 # 引用
    (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M), ""),  # 箇条書きの記号
    (re.compile(r"\*\*|__|`"), ""),                       # 太字・コード
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),      # リンクは文字だけ残す
]


def strip_markdown(text):
    """Markdown の記号を取り除く（記号を語として数えないため）。"""
    for pat, rep in MD_PATTERNS:
        text = pat.sub(rep, text)
    return text


def _sentence_spans(text):
    return [doc[:] for doc in nlp().pipe(split_sentences(strip_markdown(text)))]


def _structure_points_of_text(text):
    return sum(sum(_structure(s)["points"].values()) for s in _sentence_spans(text))


def _split_candidates(sent, st):
    """文節の切れ目のうち、節の終わり（〜ので、〜が、〜て、など）を分割候補にする。"""
    cands = []
    spans = st["spans"]
    for bi, sp in enumerate(spans[:-1]):
        hb = st["heads"][bi]
        if hb < 0:
            continue
        arc = next((a for a in st["arcs"] if a["from"] == bi), None)
        if not arc or arc["dep"] not in SPLIT_MARK_DEPS:
            continue
        body = [t for t in sp if t.pos_ != "PUNCT"]
        ends_with_comma = sp[-1].text in ("、", ",")
        ends_with_conj = bool(body) and (body[-1].pos_ == "SCONJ" or "接続助詞" in body[-1].tag_)
        if not (ends_with_comma or ends_with_conj):
            continue
        cut = sp.end_char - sent.start_char
        left = sent.text[:cut].rstrip("、, ")
        right = sent.text[cut:].lstrip("、, ")
        if len(left) < 6 or len(right) < 6:
            continue
        cands.append((bi, left + "。", right))
    return cands


def _best_split(sent, st):
    before = sum(st["points"].values())
    if before <= 0:
        return None
    best = None
    for bi, left, right in _split_candidates(sent, st):
        after = _structure_points_of_text(left) + _structure_points_of_text(right)
        delta = after - before
        if delta < -0.5 and (best is None or delta < best["delta"]):
            best = {"after_bunsetsu": bi, "delta": round(delta, 1), "left": left, "right": right}
    return best


OPEN_PARENS = ("（", "(")
CLOSE_PARENS = ("）", ")")


def _word_candidates(sent, known):
    """内容語ごとの (頻度, 判定方法, 基本点)。併記「X（Y）」は1組とみなし、最も難しい語だけ数える。"""
    cand = {}
    for t in sent:
        if not _is_word_target(t):
            continue
        if known and (t.text in known or t.lemma_ in known):
            cand[t.i] = (None, "既知語", 0.0)
            continue
        z, how = word_zipf(t.text, t.lemma_)
        cand[t.i] = (z, how, _word_points(z))
    toks = list(sent)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.i in cand and i + 1 < len(toks) and toks[i + 1].text in OPEN_PARENS:
            j, depth = i + 2, 1
            while j < len(toks) and depth:
                if toks[j].text in OPEN_PARENS:
                    depth += 1
                elif toks[j].text in CLOSE_PARENS:
                    depth -= 1
                j += 1
            members = [t.i] + [x.i for x in toks[i + 2:j - 1] if x.i in cand]
            if len(members) > 1:
                keep = max(members, key=lambda m: cand[m][2])
                for m in members:
                    if m != keep:
                        z, how, _ = cand[m]
                        cand[m] = (z, how + "・併記なので数えない", 0.0)
            i = j
        else:
            i += 1
    return cand


def analyze(text, seen=None, known=None):
    """seen を渡すと、複数の文章（セクション）をまたいで「2回目以降の語」を数えられる。
    known は読み手やプロジェクトにとって既知の語の集合で、単語の点を0にする。"""
    if seen is None:
        seen = set()
    sentences = []
    total_chars = 0
    for si, sent in enumerate(_sentence_spans(text)):
        st = _structure(sent)
        total_chars += st["n_chars"]

        tok_owner = {}
        for bi, sp in enumerate(st["spans"]):
            for t in sp:
                tok_owner[t.i] = bi

        words = []
        word_points = 0.0
        cand = _word_candidates(sent, known)
        for t in sent:
            if t.i not in cand:
                continue
            z, how, pts = cand[t.i]
            key = t.lemma_
            repeat = key in seen
            if repeat:
                pts *= WEIGHTS["word_repeat_factor"]
            seen.add(key)
            word_points += pts
            if pts >= 0.1:
                words.append({
                    "text": t.text,
                    "lemma": t.lemma_,
                    "zipf": None if z is None else round(z, 2),
                    "how": how,
                    "repeat": repeat,
                    "points": round(pts, 1),
                    "bunsetsu": tok_owner.get(t.i),
                    "offset": t.idx - sent.start_char,
                })

        points = dict(st["points"])
        points["word"] = word_points
        score = sum(points.values())

        bunsetsu = []
        for bi, sp in enumerate(st["spans"]):
            bunsetsu.append({
                "text": sp.text,
                "start": sp.start_char - sent.start_char,
                "end": sp.end_char - sent.start_char,
                "head": st["heads"][bi],
                "pending": st["pending"][bi],
                "depth": st["depths"][bi],
            })

        sentences.append({
            "index": si,
            "text": sent.text,
            "chars": st["n_chars"],
            "score": round(score, 1),
            "points": {k: round(v, 1) for k, v in points.items()},
            "detail": {"peak": st["peak"], "max_depth": st["max_depth"], "mdd": round(st["mdd"], 2),
                       "dep_peak_points": round(st["dep_peak_points"], 1), "dep_area_points": round(st["dep_area_points"], 1)},
            "bunsetsu": bunsetsu,
            "arcs": st["arcs"],
            "words": words,
            "split": _best_split(sent, st),
        })

    total = sum(s["score"] for s in sentences)
    per100 = total / total_chars * 100 if total_chars else 0.0
    comp_totals = {c: round(sum(s["points"][c] for s in sentences), 1) for c in COMPONENTS}
    hardest = max(sentences, key=lambda s: s["score"])["index"] if sentences else None
    return {
        "score_per100": round(per100, 1),
        "total": round(total, 1),
        "chars": total_chars,
        "n_sentences": len(sentences),
        "components": comp_totals,
        "hardest": hardest,
        "sentences": sentences,
        "weights": WEIGHTS,
    }


def _print_summary(r):
    print(f"難しさの総量: {r['total']} 点（高いほど難しい）  補助: {r['score_per100']} 点/100字 / {r['chars']} 字 / {r['n_sentences']} 文")
    print("内訳:", ", ".join(f"{k}={v}" for k, v in r["components"].items()))
    for s in r["sentences"]:
        p = s["points"]; d = s["detail"]
        print(f"\n[{s['index'] + 1}] {s['score']}点  " + " ".join(f"{k}={p[k]}" for k in COMPONENTS)
              + f"  (係り先を待つ文節のピーク={d['peak']}, 平均依存距離={d['mdd']}, 入れ子={d['max_depth']})")
        print("   ", s["text"])
        for wd in s["words"]:
            print(f"     語: {wd['text']} zipf={wd['zipf']} {wd['how']} → {wd['points']}点" + ("（再登場）" if wd["repeat"] else ""))
        if s["split"]:
            print(f"     分割案: 「{s['split']['left']}」|「{s['split']['right']}」 → {s['split']['delta']}点")


def _print_brief(r, top=3):
    """スキル向けの短い要約: 総量、内訳、難しい文の上位、難しい語の上位。"""
    print(f"難しさの総量: {r['total']} 点（高いほど難しい。文の形の難しさだけを測る）")
    print(f"補助: {r['score_per100']} 点/100字、{r['chars']} 字、{r['n_sentences']} 文")
    print("内訳: " + "、".join(f"{ {'length': '長さ', 'word': '単語', 'dependency': '係り受けの負荷'}[k] } {v}" for k, v in r["components"].items()))
    hard = sorted(r["sentences"], key=lambda s: -s["score"])[:top]
    print(f"\n難しい文（上位{len(hard)}）:")
    for s in hard:
        p = s["points"]
        print(f"- {s['score']}点（長さ {p['length']}・単語 {p['word']}・係り受け {p['dependency']}）: {s['text']}")
        if s["split"]:
            print(f"  分割案（{s['split']['delta']}点）: 「{s['split']['left']}」｜「{s['split']['right']}」")
    words = {}
    for s in r["sentences"]:
        for w in s["words"]:
            if not w["repeat"]:
                words[w["lemma"]] = max(words.get(w["lemma"], 0), w["points"])
    top_words = sorted(words.items(), key=lambda x: -x[1])[:5]
    if top_words:
        print("\n難しい語（上位5）: " + "、".join(f"{w}（{p}点）" for w, p in top_words))


COMPOUND_JOINERS = {"・", "－", "-"}
# 複合語の取りこぼし候補にする、単語の点の合計の上限。変数であり根拠はない
COMPOUND_MAX_POINTS = 3.0


def compound_candidates(text, known=None, max_points=COMPOUND_MAX_POINTS):
    """名詞が続くまとまり（複合語）のうち、丸ごとでは辞書になく、部品の単語の点の合計が低いものを返す。
    部品がよく使う語だと、まとまりとしては見慣れなくても単語の点がほぼ0になる（取りこぼし）。
    同じまとまりは最初に出てきたものだけ返す。"""
    out = []
    seen = set()
    for si, sent in enumerate(_sentence_spans(text)):
        cand = _word_candidates(sent, known)
        toks = list(sent)
        i = 0
        while i < len(toks):
            if toks[i].pos_ not in ("NOUN", "PROPN"):
                i += 1
                continue
            j = i + 1
            while j < len(toks):
                if toks[j].pos_ in ("NOUN", "PROPN"):
                    j += 1
                elif toks[j].text in COMPOUND_JOINERS and j + 1 < len(toks) and toks[j + 1].pos_ in ("NOUN", "PROPN"):
                    j += 2
                else:
                    break
            run = toks[i:j]
            # 接尾辞・接頭辞（「後」「等」「ご」）や記号（「％」）は部品に数えない（「変更後」「ご説明」は複合語として扱わない）
            nouns = [t for t in run if t.text not in COMPOUND_JOINERS and not t.tag_.startswith(("接尾辞", "接頭辞"))
                     and any(c.isalpha() for c in t.text)]
            if len(nouns) >= 2:
                surface = "".join(t.text for t in run)
                points = sum(cand[t.i][2] for t in run if t.i in cand)
                in_known = bool(known) and surface in known
                if points < max_points and _zipf(surface) is None and not in_known and surface not in seen:
                    seen.add(surface)
                    out.append({"sentence": si, "text": surface, "points": round(points, 1),
                                "sentence_text": sent.text})
            i = j
    return out


def load_known(path):
    """既知語ファイル（1行1語。# で始まる行は無視）を読む。なければ空。"""
    try:
        with open(path, encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip() and not l.startswith("#")}
    except FileNotFoundError:
        return set()


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    brief = "--brief" in args
    args = [a for a in args if a != "--brief"]
    known = set()
    if "--known" in args:
        known = load_known(args[args.index("--known") + 1])
        del args[args.index("--known"):args.index("--known") + 2]
    args = [a for a in args if a != "--json"]
    compounds = "--compounds" in args
    args = [a for a in args if a != "--compounds"]
    text = " ".join(args) if args else sys.stdin.read()
    if compounds:
        cs = compound_candidates(text, known=known)
        if as_json:
            print(json.dumps(cs, ensure_ascii=False, indent=1))
        else:
            print(f"複合語の取りこぼし候補（単語の点の合計が{COMPOUND_MAX_POINTS}点未満）: {len(cs)}件")
            for c in cs:
                print(f"[{c['sentence'] + 1}] {c['text']}（{c['points']}点）")
        sys.exit(0)
    result = analyze(text, known=known)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif brief:
        _print_brief(result)
    else:
        _print_summary(result)
