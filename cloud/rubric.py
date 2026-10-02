#!/usr/bin/env python3
"""採点表スプレッドシートの課題タブを読み、減点項目・点数計算・講評の組み立てを行う。

Moodle・Anthropic に依存しない純粋関数だけを置く（単体テストできるようにするため）。

採点の根拠は採点表の課題タブに一本化する（2026-09-29 江尻指示）。
- モデルには、その課題のタブにある減点項目だけを選択肢として渡す
- 点数はモデルに書かせず、選ばれた減点項目からコードで計算する
- 講評の HTML もモデルに書かせず、決まった型でコードが組み立てる
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

CONTENT = "content"
AI = "ai"
KIND_LABEL = {CONTENT: "内容", AI: "AI使用ログ"}


@dataclass
class Item:
    id: str
    text: str
    unit: float          # 1件あたりの減点（正の数）
    section: str


@dataclass
class Section:
    key: str             # タブ内で一意（内容と AI で同じ節名があるため名前は使わない）
    name: str
    kind: str            # content / ai
    max: float | None
    items: list[Item] = field(default_factory=list)
    part: str | None = None      # 事前課題2-1 など（小計行で区切られたタブのみ）
    shared: bool = False         # 「全体で1回判定」の減点（複数の課題に按分する）


@dataclass
class Rubric:
    key: str             # 事前課題2-1 など
    tab: str
    sections: list[Section]
    tab_ai_total: float | None = None   # 按分の分母（shared を含むタブの AI 合計）

    def max_of(self, kind: str) -> float:
        return sum(s.max or 0 for s in self.sections if s.kind == kind and not s.shared)

    def items(self) -> list[Item]:
        return [i for s in self.sections for i in s.items]

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items() if i.id == item_id), None)

    def section_of(self, item: Item) -> Section:
        return next(s for s in self.sections if s.key == item.section)


def _num(v) -> float | None:
    try:
        return float(str(v).replace("−", "-").strip())
    except (TypeError, ValueError):
        return None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s)


def _cell(row: list, i: int) -> str:
    return (row[i] if i < len(row) else "") or ""


def parse_tab(rows: list[list], tab: str) -> list[Section]:
    """採点表の課題タブ（A:項目 B:減点内容 C:配点）を節と減点項目に分解する。

    - C が負の数の行 = 減点項目（直前の節に属する）
    - A に見出しがあり C が0以上の行 = 節（C がその節の満点）
    - 「└ …小計」行 = そこまでの節が属する課題（事前課題2-1 等）の区切り
    - 「■ AIの使い方」行 以降 = AI使用ログの節
    - 「合計」行 = 見出しの無い節（事前課題1 など）の満点
    """
    sections: list[Section] = []
    kind = CONTENT
    cur: Section | None = None
    pending_part: list[Section] = []
    for r, row in enumerate(rows, 1):
        a, b, c = _cell(row, 0).strip(), _cell(row, 1).strip(), _cell(row, 2).strip()
        if a.startswith("■") and "AI" in a:
            kind, cur, pending_part = AI, None, []
            continue
        cv = _num(c)
        if a.startswith("└"):
            m = re.search(r"(事前課題\d-\d|中間課題\d|修了レポート\d?)", a)
            if m and kind == CONTENT:
                for s in pending_part:
                    s.part = m.group(1)
            pending_part = []
            cur = None
            continue
        if a in ("合計", "AIの使い方 合計") or a.endswith("合計"):
            if cur is not None and cur.max is None and cv is not None:
                cur.max = cv
            cur = None
            continue
        if cv is not None and cv < 0 and b:
            if cur is None:
                cur = Section(key=f"s{r}", name=f"{KIND_LABEL[kind]}（全体）", kind=kind, max=None)
                sections.append(cur)
                pending_part.append(cur)
            cur.items.append(Item(id=f"r{r}", text=b, unit=-cv, section=cur.key))
            continue
        if a and cv is not None and cv >= 0 and not a.isdigit():
            cur = Section(key=f"s{r}", name=a, kind=kind, max=cv,
                          shared=(kind == AI and "全体で1回" in a))
            sections.append(cur)
            if kind == CONTENT:
                pending_part.append(cur)
    return [s for s in sections if s.items or (s.max or 0) > 0]


def _label(name: str) -> str:
    """節名の突き合わせ用キー（空白・全半角・綴りの揺れを吸収するため「立案（n）」までを使う）。"""
    n = _norm(name)
    m = re.match(r"(.*?立案\(\d\))", n)
    return m.group(1) if m else n


def build_rubric(key: str, tab: str, rows: list[list], part: str | None = None) -> Rubric:
    """タブ全体から、課題 key の分だけを取り出す。part 指定時は小計で区切られた範囲だけ。"""
    sections = parse_tab(rows, tab)
    ai_total = None
    for row in rows:
        if _cell(row, 0).strip() == "AIの使い方 合計":
            ai_total = _num(_cell(row, 2))
    if part:
        content = [s for s in sections if s.kind == CONTENT and s.part == part]
        names = {_norm(s.name) for s in content}
        all_labels = [_label(s.name) for s in sections if s.kind == CONTENT]
        labels = {_label(s.name) for s in content if all_labels.count(_label(s.name)) == 1}
        ai = [s for s in sections if s.kind == AI and
              (s.shared or _norm(s.name) in names or _label(s.name) in labels)]
        sections = content + ai
    ids = {i.id for s in sections for i in s.items}
    assert len(ids) == sum(len(s.items) for s in sections)
    return Rubric(key=key, tab=tab, sections=sections, tab_ai_total=ai_total)


def unit_of(rubric: Rubric, item: Item) -> float:
    """1件あたりの減点。

    『全体で1回判定』（事前課題2の AI使用ログの質）の項目も按分しない。講師は 2-1・2-3 の
    それぞれで「修正が提出物に反映されていない −10」をそのまま引いている（2026-10-03 講師採点）。
    """
    return item.unit


def compute_scores(rubric: Rubric, deductions: list[dict]) -> dict:
    """選ばれた減点項目から点数を計算する。節ごとに0点を下限とする。

    deductions: [{item_id, count, ...}]（検証済みのもの）
    戻り値: {content: 点, ai: 点, total: 点, by_item: {item_id: 減点}}
    """
    lost: dict[str, float] = {}
    by_item: dict[str, float] = {}
    for d in deductions:
        it = rubric.item(d["item_id"])
        pts = unit_of(rubric, it) * int(d.get("count") or 1)
        by_item[it.id] = by_item.get(it.id, 0) + pts
        lost[it.section] = lost.get(it.section, 0) + pts
    out = {CONTENT: 0.0, AI: 0.0}
    shared_lost = 0.0
    for s in rubric.sections:
        if s.shared:
            shared_lost += lost.get(s.key, 0)
            continue
        out[s.kind] += max(0.0, (s.max or 0) - lost.get(s.key, 0))
    out[AI] = max(0.0, out[AI] - shared_lost)
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(out[CONTENT] + out[AI], 2)
    out["by_item"] = by_item
    return out


def validate_deductions(rubric: Rubric, deductions) -> tuple[list[dict], list[str]]:
    """モデルが返した減点リストを検証する。戻り値は (採用する減点, 問題点)。"""
    ok, problems = [], []
    for d in deductions or []:
        iid = str(d.get("item_id") or "")
        it = rubric.item(iid)
        if it is None:
            problems.append(f"採点表に無い項目 {iid!r}")
            continue
        try:
            count = int(d.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            problems.append(f"{iid} の件数が不正 {d.get('count')!r}")
            continue
        if not (d.get("quote") or "").strip():
            problems.append(f"{iid} に提出物からの引用が無い")
            continue
        ok.append({**d, "item_id": iid, "count": count})
    return ok, problems


# ---------- 講評の組み立て（型はコードで決める） ----------
# 受講生が知らない採点の内部用語。講評に出たらモデルに書き直させる。
FORBIDDEN_TERMS = [
    "採点者A", "採点者B", "採点者C", "採点者Ａ", "採点者Ｂ", "採点者Ｃ", "合議", "3名の採点", "3名の点",
    "節目平均", "節目得点", "内部修正点", "外部追加点", "整形明示", "ルーブリック", "v2.4", "§",
    "needs_human_review", "item_id", "criterionid",
]


def forbidden_in(texts: list[str]) -> list[str]:
    joined = "\n".join(t or "" for t in texts)
    return [t for t in FORBIDDEN_TERMS if t in joined]


def _esc(s: str) -> str:
    return html.escape((s or "").strip())


def _fmt(p: float) -> str:
    return f"{p:g}"


def short_item_text(text: str) -> str:
    """減点項目名から「：1項目につき」等の注記を落として短くする。"""
    t = re.split(r"\s*[:：]\s*(?:1|１|不足1)", text)[0]
    return t.strip()


SUGGEST_LABEL = "さらに良くするなら"   # 講師の講評の見出しに合わせる


def _lines(items) -> list[str]:
    return [t.strip() for t in (items or []) if isinstance(t, str) and t.strip()]


def render_criterion(rubric: Rubric, kind: str, result: dict, deductions: list[dict],
                     scores: dict, grademax: float) -> str:
    """基準ごとの講評（評定ガイドの基準欄に入れる）。講師と同じ型の改行つきテキスト。

    【内容：X点／Y点】
    ◎ 評価できる点 … / △ 減点理由：項目（−N点） … / △ さらに良くするなら …
    """
    fb = (result.get("feedback") or {}).get(kind) or {}
    out = [f"【{KIND_LABEL[kind]}：{_fmt(scores[kind])}点／{_fmt(grademax)}点】"]
    good = _lines(fb.get("strengths"))[:3]
    if good:
        out += ["", "◎ 評価できる点"] + [f"・{g}" for g in good]
    mine = [d for d in deductions if rubric.section_of(rubric.item(d["item_id"])).kind == kind]
    for d in mine:
        it = rubric.item(d["item_id"])
        pts = unit_of(rubric, it) * d["count"]
        cnt = f"・{d['count']}件" if d["count"] > 1 else ""
        mark = "【講師確認】" if d.get("certain") is False else ""
        out += ["", f"△ 減点理由：{short_item_text(it.text)}（−{_fmt(pts)}点{cnt}）{mark}",
                (d.get("reason") or "").strip()]
        quote = (d.get("quote") or "").strip()
        if quote:
            loc = (d.get("location") or "").strip()
            out.append(f"該当箇所{('（' + loc + '）') if loc else ''}：「{quote}」")
    tips = _lines(fb.get("suggestions"))[:3]
    if tips:
        out += ["", f"△ {SUGGEST_LABEL}"] + [f"・{t}" for t in tips]
        out.append("※採点基準上の減点事項ではありません。")
    return "\n".join(out)


def render_feedback(rubric: Rubric, result: dict, deductions: list[dict], scores: dict,
                    grademax: dict[str, float]) -> str:
    """フィードバック欄（総評）。講師は基準ごとの詳細を評定ガイドの基準欄に、総評をここに書いている。"""
    total_max = sum(v for v in grademax.values() if v)
    parts = [f"<p><strong>【採点結果：{_fmt(scores['total'])}点／{_fmt(total_max)}点】</strong>"
             f"（" + "・".join(f"{KIND_LABEL[k]} {_fmt(scores[k])}点／{_fmt(grademax[k])}点"
                              for k in (CONTENT, AI) if grademax.get(k)) + "）</p>"]
    summary = (result.get("summary") or result.get("closing") or "").strip()
    for para in [p for p in summary.split("\n") if p.strip()]:
        parts.append(f"<p>{_esc(para)}</p>")
    parts.append("<p>基準ごとの評価と減点理由は、評定ガイドの各基準欄をご覧ください。</p>")
    return "\n".join(parts)


def render_remark(rubric: Rubric, kind: str, deductions: list[dict], scores: dict,
                  result: dict | None = None, grademax: float | None = None) -> str:
    """評定ガイドの基準ごとのコメント。講評が渡されれば講師と同じ型の全文、無ければ減点の一覧。"""
    if result is not None and grademax is not None:
        return render_criterion(rubric, kind, result, deductions, scores, grademax)
    mine = [d for d in deductions if rubric.section_of(rubric.item(d["item_id"])).kind == kind]
    if not mine:
        return "減点なし"
    return "\n".join(f"{short_item_text(rubric.item(d['item_id']).text)} "
                     f"−{_fmt(unit_of(rubric, rubric.item(d['item_id'])) * d['count'])}点" for d in mine)
