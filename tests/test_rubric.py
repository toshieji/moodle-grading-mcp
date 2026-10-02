#!/usr/bin/env python3
"""cloud/rubric.py と、grading_job の採点表まわりの検証（python tests/test_rubric.py）。

採点表は 2026-09-29 時点の本番スプレッドシート（課題採点表_260828）の全タブを
tests/fixtures/rubric_260828.json に保存したものを使う。失敗すると終了コード 1。
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("MOODLE_URL", "https://moodle.example.invalid")
os.environ.setdefault("MOODLE_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cloud"))
import grading_job as gj  # noqa: E402
import rubric as rb  # noqa: E402

TABS = json.load(open(os.path.join(ROOT, "tests", "fixtures", "rubric_260828.json"), encoding="utf-8"))
FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILED.append(label)


def guide(content: float, ai: float) -> list[dict]:
    return [{"id": 1, "name": f"内容（{content:g}点）", "maxscore": content},
            {"id": 2, "name": f"AI使用ログ（{ai:g}点）", "maxscore": ai}]


def R(name: str, content: float, ai: float) -> rb.Rubric:
    r, _, hold = gj.rubric_for(name, TABS, guide(content, ai))
    assert not hold, hold
    return r


def test_parse() -> None:
    print("採点表の読み取り（本番の採点表で、Moodle の評定ガイドの満点と一致すること）")
    for name, c, a in [("事前課題1：上級ウェブ解析士とは？（2027）", 10, 10),
                       ("事前課題2-1：事業分析", 25, 25), ("事前課題2-2：ユーザー分析", 12, 12),
                       ("事前課題2-3：ロジックツリー", 13, 13), ("事前課題3：ナレッジ記事", 15, 15)]:
        r, _, hold = gj.rubric_for(name, TABS, guide(c, a))
        check(not hold and r.max_of(rb.CONTENT) == c and r.max_of(rb.AI) == a,
              f"{name.split('：')[0]}: 内容{c}・AI{a}（{hold or 'OK'}）")
    r = R("事前課題2-1：事業分析", 25, 25)
    names = [s.name for s in r.sections if s.kind == rb.AI]
    check(any("セグメンテーション" in n for n in names),
          "表記ゆれ（セグメンション／セグメンテーション）があってもAIの節を対応付ける")
    check(not any("ペルソナ" in s.name for s in r.sections), "2-1 に 2-2 の節（ペルソナ）が混ざらない")
    check(any(s.shared for s in r.sections), "「全体で1回判定」の減点を含む")


def test_hold() -> None:
    print("採点を保留する条件")
    _, _, hold = gj.rubric_for("中間課題2：ウェブマーケティング計画書", TABS, guide(60, 0))
    check("未設定" in hold, "対応表に無い課題は保留")
    _, _, hold = gj.rubric_for("事前課題3：ナレッジ記事", TABS, guide(20, 10))
    check("満点が一致しません" in hold, "採点表と評定ガイドの満点が食い違えば保留")
    _, _, hold = gj.rubric_for("事前課題3：ナレッジ記事", TABS, None)
    check("評定ガイド" in hold, "評定ガイドが無ければ保留")


def test_scores() -> None:
    print("点数はコードで計算する")
    r = R("事前課題3：ナレッジ記事", 15, 15)
    full = rb.compute_scores(r, [])
    check(full["content"] == 15 and full["ai"] == 15 and full["total"] == 30, "減点なしなら満点")
    typo = next(i for i in r.items() if "誤字" in i.text and r.section_of(i).kind == rb.CONTENT)
    sc = rb.compute_scores(r, [{"item_id": typo.id, "count": 3}])
    check(sc["content"] == 12, "「1か所につき」は件数分を引く（誤字3か所で −3）")
    big = [{"item_id": i.id, "count": 5} for i in r.items() if r.section_of(i).kind == rb.CONTENT]
    check(rb.compute_scores(r, big)["content"] == 0, "節の点数は0点を下限にする")

    r21 = R("事前課題2-1：事業分析", 25, 25)
    shared = next(i for i in r21.items() if r21.section_of(i).shared and i.unit == 20)
    check(rb.unit_of(r21, shared) == 20, "全体で1回判定の減点は按分しない（講師は 2-1 でも −10 をそのまま引いている）")


def test_validate() -> None:
    print("モデル出力の検証")
    r = R("事前課題3：ナレッジ記事", 15, 15)
    iid = r.items()[0].id
    ok, bad = rb.validate_deductions(r, [
        {"item_id": "r999", "count": 1, "quote": "x"},
        {"item_id": iid, "count": 1, "quote": ""},
        {"item_id": iid, "count": 2, "quote": "「本文」"}])
    check(len(ok) == 1 and ok[0]["count"] == 2, "採点表に無い項目・引用の無い減点は採らない")
    check(len(bad) == 2, "採らなかった理由を返す")
    tool = gj.build_grade_tool(r)
    enum = tool["input_schema"]["properties"]["deductions"]["items"]["properties"]["item_id"]["enum"]
    check(sorted(enum) == sorted(i.id for i in r.items()), "ツール定義の選択肢は採点表の項目だけ")
    _, problems = gj.check_result(r, {"deductions": [], "closing": "採点者Aの見立てでは良好です",
                                      "feedback": {"content": {"strengths": ["節目平均は9点です"]}}})
    check(any("内部用語" in p for p in problems), "受講生向けの文に内部用語があれば直させる")

    ai_items = [i for i in r.items() if r.section_of(i).kind == rb.AI]
    log_item = next(i for i in ai_items if "会話ログ" in i.text)
    all_clear = [{"item_id": i.id, "verdict": "非該当", "evidence": "確認済み"} for i in ai_items]
    _, problems = gj.check_result(r, {"deductions": [], "feedback": {}, "ai_checks": all_clear[:-1]})
    check(any("確認していない項目" in p for p in problems), "AI使用ログの項目を1つでも確認していなければ直させる")
    _, problems = gj.check_result(r, {"deductions": [], "feedback": {}, "ai_checks": all_clear})
    check(not problems, "全項目を確認し、該当なしなら問題なし")
    flagged = [{**c, "verdict": "該当"} if c["item_id"] == log_item.id else c for c in all_clear]
    _, problems = gj.check_result(r, {"deductions": [], "feedback": {}, "ai_checks": flagged})
    check(any("deductions に入れていない" in p for p in problems), "「該当」としたのに減点していなければ直させる")
    ded = [{"item_id": log_item.id, "count": 1, "quote": "プロンプト：", "reason": "出力の記録がありません。"}]
    _, problems = gj.check_result(r, {"deductions": ded, "feedback": {}, "ai_checks": flagged})
    check(not problems, "「該当」と減点が一致していれば問題なし")
    _, problems = gj.check_result(r, {"deductions": ["r5"], "feedback": {"content": "良い"}, "ai_checks": []})
    check(bool(problems), "型の崩れた出力は落ちずに問題として返す")
    text = gj.rubric_prompt(r)
    check("講師の解釈" in text and "AIの出力の要約" in text, "会話ログの項目に講師の解釈を付けて渡す")


def test_render() -> None:
    print("講評の型（講師と同じく、基準欄に詳細・フィードバック欄に総評）")
    r = R("事前課題3：ナレッジ記事", 15, 15)
    typo = next(i for i in r.items() if "誤字" in i.text and r.section_of(i).kind == rb.CONTENT)
    ded = [{"item_id": typo.id, "count": 2, "reason": "表記の誤りが2か所あります。",
            "quote": "ウェブ解析士の資格を習得", "location": "記事原稿.docx"}]
    result = {"feedback": {"content": {"strengths": ["ペルソナと記事の読者がつながっています。"],
                                       "suggestions": ["計測条件を完全一致にすると確実です。"]},
                           "ai": {"strengths": ["修正の理由が具体的です。"]}},
              "summary": "記事企画から計測まで一貫しています。\n次は計測条件の精度を意識してください。"}
    sc = rb.compute_scores(r, ded)
    content = rb.render_remark(r, rb.CONTENT, ded, sc, result, 15)
    ai = rb.render_remark(r, rb.AI, ded, sc, result, 15)
    check(content.startswith("【内容：13点／15点】") and ai.startswith("【AI使用ログ：15点／15点】"),
          "基準欄の見出しに点数")
    check("△ 減点理由：誤字脱字がある（−2点・2件）" in content, "減点理由の見出しに項目名・点数・件数")
    check("該当箇所（記事原稿.docx）：「ウェブ解析士の資格を習得」" in content, "減点理由に引用")
    check("△ 減点理由" not in ai, "減点の無い基準には減点理由を出さない")
    check("△ さらに良くするなら" in content and "減点事項ではありません" in content, "改善提案は減点ではないと明記")
    check("\n" in content and "<" not in content, "基準欄は改行つきのプレーンテキスト")
    fb = rb.render_feedback(r, result, ded, sc, {rb.CONTENT: 15, rb.AI: 15})
    check("【採点結果：28点／30点】" in fb and "内容 13点／15点" in fb, "フィードバック欄に合計と内訳")
    check(fb.count("<p>") >= 3, "総評は段落ごとに分ける")
    check(not rb.forbidden_in([content, ai, fb]), "内部用語を含まない")
    check(rb.render_remark(r, rb.AI, ded, sc) == "減点なし", "講評なしの呼び出しは減点一覧（後方互換）")


def test_main_holds() -> None:
    print("main: 対応表に無い課題は採点しない")
    saved: list = []
    graded: list = []
    patches = {
        "fetch_rubric_tabs": lambda: TABS,
        "get_ai_user": lambda: 10,
        "list_assignments": lambda cid: [{"id": 10, "cmid": 20, "name": "中間課題2：計画書"},
                                         {"id": 11, "cmid": 21, "name": "事前課題3：記事"}],
        "list_pending": lambda aid: [{"userid": 1}],
        "get_guide_criteria": lambda cmid: guide(15, 15),
        "get_submission": lambda aid, uid: {"onlinetext": "log", "files": ["a.docx"], "file_text": "本文",
                                            "unreadable": [], "images": []},
        "grade_submission": lambda *a, **k: (graded.append(1) or
                                             {"deductions": [], "feedback": {}, "closing": "",
                                              "confidence": "high", "needs_human_review": False}),
        "save_grade_draft": lambda aid, uid, cid, fb, **k: saved.append((aid, k["criteria_scores"], fb)),
    }
    originals = {k: getattr(gj, k) for k in patches}
    import types
    sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=lambda api_key: object()))
    os.environ["MOODLE_ALLOW_WRITE"] = "1"
    gj.ALLOW_WRITE, gj.WRITE_COURSES, gj.GRADE_COURSE_IDS = True, {"900"}, ["900"]
    for k, v in patches.items():
        setattr(gj, k, v)
    try:
        gj.main()
    finally:
        for k, v in originals.items():
            setattr(gj, k, v)
    check([s[0] for s in saved] == [11], "対応表にある事前課題3だけ保存し、中間課題2は保存しない")
    check(len(graded) == 1, "対応表に無い課題は採点モデルを呼ばない")
    check(saved and [c["score"] for c in saved[0][1]] == [15, 15], "減点なしなら評定ガイドの両基準とも満点で保存")


def test_never_overwrite() -> None:
    """採点済みの提出は決して上書きしない（2026-10-03 江尻指示）。"""
    print("採点済みの提出を上書きしない")
    calls: list = []
    orig = (gj.current_grades, gj._call, gj.ALLOW_WRITE, gj.WRITE_COURSES)
    gj._call = lambda fn, params: calls.append(fn) or {}
    gj.ALLOW_WRITE, gj.WRITE_COURSES = True, {"900"}
    try:
        gj.current_grades = lambda aid: {5: (19.0, 654)}
        try:
            gj.save_grade_draft(11, 5, "900", "<p>x</p>", criteria_scores=[{"criterionid": 1, "score": 1}])
            refused = False
        except RuntimeError as e:
            refused = "上書きしません" in str(e)
        check(refused and "mod_assign_save_grade" not in calls, "人が採点済みなら保存を拒否し、Moodle に送らない")
        gj.current_grades = lambda aid: {5: (19.0, 10)}
        try:
            gj.save_grade_draft(11, 5, "900", "<p>x</p>", criteria_scores=[{"criterionid": 1, "score": 1}])
            refused = False
        except RuntimeError:
            refused = True
        check(refused and "mod_assign_save_grade" not in calls, "AI のアカウントで採点済みでも上書きしない")
    finally:
        gj.current_grades, gj._call, gj.ALLOW_WRITE, gj.WRITE_COURSES = orig


def _run_main(patches: dict, **flags) -> None:
    import types
    sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=lambda api_key: object()))
    saved_flags = {k: getattr(gj, k) for k in flags}
    originals = {k: getattr(gj, k) for k in patches}
    for k, v in {**patches, **flags}.items():
        setattr(gj, k, v)
    try:
        gj.main()
    finally:
        for k, v in {**originals, **saved_flags}.items():
            setattr(gj, k, v)


def test_regrade_and_calibrate() -> None:
    print("再採点の指定と校正モード")
    saved, graded = [], []
    base = {
        "fetch_rubric_tabs": lambda: TABS,
        "get_ai_user": lambda: 10,
        "list_assignments": lambda cid: [{"id": 11, "cmid": 21, "name": "事前課題3：記事"}],
        "list_pending": lambda aid: [],
        "list_human_graded": lambda aid, me: [{"userid": 5, "human_grade": 19.0, "grader": 654}],
        "get_guide_criteria": lambda cmid: guide(15, 15),
        "get_submission": lambda aid, uid: {"onlinetext": "log", "files": [], "file_text": "本文",
                                            "unreadable": [], "images": []},
        "grade_submission": lambda *a, **k: (graded.append(1) or
                                             {"deductions": [], "feedback": {}, "closing": "",
                                              "confidence": "high", "needs_human_review": False}),
        "save_grade_draft": lambda aid, uid, cid, fb, **k: saved.append(uid),
    }
    _run_main(base, ALLOW_WRITE=True, WRITE_COURSES={"900"}, GRADE_COURSE_IDS=["900"],
              REGRADE_TARGETS={(11, 5)}, CALIBRATE=False)
    check(not graded and not saved, "書き込みありのときは再採点の指定を無視する（採点済みを書き換えない）")
    _run_main(base, ALLOW_WRITE=False, WRITE_COURSES={"900"}, GRADE_COURSE_IDS=["900"],
              REGRADE_TARGETS=set(), CALIBRATE=True)
    check(len(graded) == 1 and not saved, "校正モードは人の採点済みを AI でも採点し、保存はしない")


if __name__ == "__main__":
    test_parse()
    test_hold()
    test_scores()
    test_validate()
    test_render()
    test_main_holds()
    test_never_overwrite()
    test_regrade_and_calibrate()
    print(f"\n{'FAILED: ' + str(len(FAILED)) if FAILED else 'all passed'}")
    sys.exit(1 if FAILED else 0)
