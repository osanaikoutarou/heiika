# heiika（平易化）

AI の文章が長くて難しいときに、読みやすく書き換える Claude Code の skill と、文章の難しさを点数にする道具です。日本語向け。

できることは5つ。ふだんは 2 がおすすめです。

| 番号 | skill | 何をするか | 重さの目安（1,000字） |
|---|---|---|---|
| 1 | `heiika-1-simplify` | 意味を変えずに読みやすく。文を切る・並べ替える・指示語を名詞に戻す | 2〜3分 |
| 2 | `heiika-2-plain` | 1 ＋ 用語を言い換える。書き換え1回の軽い手順。文のつながり（指示語・新出語）も必要なときだけ直す | 1〜2分 |
| 3 | `heiika-3-restructure` | 構造化して、見出しと段落に組み直す。意味は変えない | 3〜4分 |
| 4 | `heiika-4-restructure-plain` | 3 の結果を平易にする | 4〜5分 |
| 5 | `heiika-5-spec` | 条件・例外・定義にラベルを付けた「1項目＝1仕様」の箇条書きにする（読みやすさではなく、確かめるための形） | 3〜4分 |

どれも AI の発言1つごとに自動でかける（hook）には重いので、頼まれたときに使います。3〜5 は構造化が入るので、条件・例外・定義が込み入った文章（仕様書、契約、法令、長い設計の説明）向けです。

部品の skill: `heiika`（入口。番号を選ばせる）、`heiika-check`（【0】測る。点数と、誤解しやすい箇所の点検）、`heiika-structure`（構造化）、`heiika-compress`（圧縮・調整）。

## 入れ方

```bash
git clone https://github.com/osanaikoutarou/heiika.git ~/heiika
cd ~/heiika && ./install.sh
export HEIIKA=~/heiika   # ~/.zshrc などに
```

`install.sh` は、Python の仮想環境（`.venv`）を作って依存を入れ、`skills/*` を `~/.claude/skills/` に symlink します。Python 3.10 以上。GiNZA の日本語モデル（`ja_ginza`）が入るので、初回は数分かかります。

## 使い方

Claude Code で、AI の返答が難しかったときに:

```
平易化スキルを使って
```

と言うと、`heiika` が番号と中身の一覧を出して、番号を聞いてきます。番号を直接言ってもかまいません（「2で」「仕様書風にして」など）。

読み手は、指定がなければ AI がそのユーザーについて知っていること（仕事や専門）に合わせます。ただし少し簡単寄りにして、知らない分野は中高生〜大学生くらいの知識とみなします。「〇〇向けに」と言えば、その読み手に合わせます。

## 道具（`tools/`）

| ファイル | 何をするか |
|---|---|
| `scorer.py` | 文の難しさを点数にする（LLM なし）。長さ・単語の頻度・係り受けの負荷の3要素。高いほど難しい。`--brief` で短い要約、`--compounds` で単語の点が取りこぼした複合語の候補 |
| `c_metrics.py` | 文のつながりの読みにくさを点数にする（LLM なし）。文をまたぐ指示語の数と距離、まとまりごとに初めて出る知らない語の数。高いほど読みにくい |
| `rikai.py` | 構造化した JSON（rikai 形式）の `init`（ひな形。`--light` で文番号つきの軽モード）、`check`（引用が元文にあるかの検証）、`render`（人が読む表示） |
| `md_page.py` | Markdown の見出しごとに採点する |
| `変換手順.md` | rikai 形式と、構造化の決まり |

```bash
$HEIIKA/.venv/bin/python $HEIIKA/tools/scorer.py --brief < 文章.md
$HEIIKA/.venv/bin/python $HEIIKA/tools/c_metrics.py 文章.md --detail
```

## 点数について

採点の道具は、skill が書き換えの判断に内部で使うもので、点数はユーザーには見せません。重みと上限は、10本ほどのサンプルを見て手で決めた仮の値で、根拠はありません。

## 背景

作った経緯と、何が測れて何が測れなかったかは、ブログに書きました（公開したらここにリンクを置きます）。

要点だけ: 文の形の難しさは点数にできて、文を切るだけで意味をほとんど落とさずに読みやすくなります。ただ、点数が低いことと分かることは別で、構造の難しさは文の点数には出ません。構造を取り出して組み直すと読みやすくなり、その効果は「つながりの点数」のほうに出ます。

## 参考にしたもの

### 使っているライブラリ
| ライブラリ | 用途 | ライセンス |
|---|---|---|
| [GiNZA](https://github.com/megagonlabs/ginza)（ja_ginza） | 日本語の形態素解析・係り受け・固有表現 | MIT |
| [spaCy](https://spacy.io) | GiNZA の土台 | MIT |
| [SudachiPy](https://github.com/WorksApplications/sudachi.rs) / [SudachiDict](https://github.com/WorksApplications/SudachiDict) | 複合語を部品に分ける | Apache-2.0 |
| [wordfreq](https://github.com/rspeer/wordfreq/)（Robyn Speer） | 単語の出現頻度（Zipf 値） | Apache-2.0 |

### 考え方のもとになった研究
点数の設計は、次の研究の考え方を借りています。式や重みは、この道具のために手で決めたもので、研究の値をそのまま使ってはいません。

- 文の中の難しさ（`scorer.py`）
  - 係り受けの距離と、係り先を待つ負荷: Gibson, E. (1998). *Linguistic complexity: Locality of syntactic dependencies*（依存局所性理論）
  - 入れ子の限界、袋小路文、単語の頻度効果: 心理言語学の文処理研究の一般的な知見
- 中身の難しさと、見せ方の難しさの区別
  - Sweller, J. (1994). *Cognitive load during problem solving*（認知負荷理論。内在的負荷と外在的負荷）
  - Kintsch, W. & Keenan, J. (1973) ほか、Kintsch の命題分析（文章を命題に分け、命題の数と新しい概念の数で読みやすさを見る）。構造化（`rikai.py`）はこれに近い発想
- 文のつながりの読みにくさ（`c_metrics.py`）
  - Halliday, M.A.K. & Hasan, R. (1976). *Cohesion in English*（結束性）
  - Graesser, A.C., McNamara, D.S. ら (2004). *Coh-Metrix*（文のつながりを多数の指標で測る道具）
  - Givón, T. (1983). *Topic Continuity in Discourse*（referential distance。指す先まで何節さかのぼるか）
  - Grosz, Joshi & Weinstein (1995). *Centering*、Barzilay & Lapata (2008). *Entity Grid*（話題の中心の移り方）
  - Pitler, E. & Nenkova, A. (2008). *Revisiting Readability*（談話の指標が読みやすさの予測に効く）

### 試験に使った文章
skill の調整には、著作権の切れた小説（青空文庫: 横光利一『機械』、小栗虫太郎『黒死館殺人事件』、中島敦『山月記』）、法令（e-Gov 法令検索: 特定商取引法 第九条）、公的機関の公開文書（日本銀行の総裁記者会見、内閣府の経済財政白書、金融庁の審議会議事録）を使いました。このリポジトリには含めていません。
