#!/usr/bin/env python3
"""上級ウェブ解析士 採点ドラフト 日次バッチ（Cloud Run Jobs 用・Mac非依存）。

Cloud Scheduler → Cloud Run Jobs（このイメージ）を毎朝起動。1回の実行で:
  1. 採点基準スプレッドシート（RUBRIC_SHEET_ID の対象タブ）を都度読み込む（採点前に毎回最新化）。
  2. 対象コース（GRADE_COURSE_IDS）の各課題について、未採点の提出を Moodle REST から取得。
  3. Claude（Anthropic API 直接）に、採点基準（スプレッドシート＋AI使用ログルーブリック）と提出内容を渡し、
     根拠つきの点数とフィードバックを構造化出力させる。
  4. Moodle へ「未公開ドラフト」（workflowstate=readyforreview）として保存。人間が確認・公開するまで
     学生には一切見えない。released には絶対にしない。AI支援である旨のフッターを必ず付与。

安全規則（server.py と同じ思想。ここでも強制する）:
  - MOODLE_ALLOW_WRITE=1 かつ course が MOODLE_WRITE_COURSE_ALLOWLIST に無ければ書き込まない。
  - workflowstate は readyforreview 固定。released は一切扱わない。
  - 学生通知は送らない。
  - フィードバック末尾に AI開示フッターを必ず付与（無ければ追記）。
  - 採点根拠（提出物からの引用）を feedback に含めることを LLM に必須化する。
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import sys
from typing import Any

import httpx

# extract.py はリポジトリ直下（MCP サーバと共用）。イメージ内では /app に同梱する。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.exists(os.path.join(_p, "extract.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import extract  # noqa: E402
import rubric as rb  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), stream=sys.stdout,
                     format="%(asctime)s grading-job %(levelname)s %(message)s")
log = logging.getLogger("grading-job")

# ---------- 設定 ----------
MOODLE_URL = os.environ["MOODLE_URL"].rstrip("/")
MOODLE_TOKEN = os.environ["MOODLE_TOKEN"]
ENDPOINT = f"{MOODLE_URL}/webservice/rest/server.php"

ALLOW_WRITE = os.environ.get("MOODLE_ALLOW_WRITE", "0") == "1"
WRITE_COURSES = {c.strip() for c in os.environ.get("MOODLE_WRITE_COURSE_ALLOWLIST", "").split(",") if c.strip()}
GRADE_COURSE_IDS = [c.strip() for c in os.environ.get("GRADE_COURSE_IDS", "").split(",") if c.strip()]

RUBRIC_SHEET_ID = os.environ.get("RUBRIC_SHEET_ID", "1bpQvKMMxQtjmn3ruhuVR8UNQAhQuWnMQMIUm21E6zv0")
# FIX(2026-09-29 江尻指示): 採点の根拠は採点表の課題タブに一本化する。
#   従来は RUBRIC_SHEET_GID の1タブ（本番設定では「表紙」＝配点一覧）しか読んでおらず、
#   課題ごとの減点項目がモデルに渡っていなかった。これが採点基準外の減点の主因だった。
#   Moodle の課題名の先頭（事前課題2-1 等）→ 採点表のタブ（と、小計で区切られた範囲）の対応表。
#   ここに無い課題は採点せず保留する（講師が採点する）。RUBRIC_MAP_JSON で上書き可。
RUBRIC_MAP: dict[str, dict] = json.loads(os.environ.get("RUBRIC_MAP_JSON") or json.dumps({
    "事前課題1": {"tab": "事前課題1（上級ウェブ解析士とは）"},
    "事前課題2-1": {"tab": "事前課題2（事業分析）", "part": "事前課題2-1"},
    "事前課題2-2": {"tab": "事前課題2（事業分析）", "part": "事前課題2-2"},
    "事前課題2-3": {"tab": "事前課題2（事業分析）", "part": "事前課題2-3"},
    "事前課題3": {"tab": "事前課題3（WordPress記事案）"},
}))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("GRADING_MODEL", "claude-haiku-4-5-20251001")
# 性質の異なる3名で独立採点し、4人目が確定させる合議制。0 にすると従来の単独採点に戻る。
PANEL_MODE = os.environ.get("GRADING_PANEL", "1") == "1"

_FOOTER_MARK = os.environ.get("MOODLE_AI_FOOTER_MARK", "AI-assisted grading")

MAX_GRADE_PER_RUN = int(os.environ.get("MAX_GRADE_PER_RUN", "30"))  # 暴走防止の上限
# FIX: 4096 では日本語の講評が入り切らず tool_use が途中で切れていたため引き上げる。
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "12000"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(20 * 1024 * 1024)))
MAX_FILE_TEXT_CHARS = int(os.environ.get("MAX_FILE_TEXT_CHARS", "40000"))
# 採点済みでも採点し直す提出（"assignid:userid,assignid:userid"）。不具合で誤採点された分の
# やり直し用。通常の定時実行では空にしておく（gcloud run jobs execute --update-env-vars で1回だけ渡す）。
REGRADE_TARGETS = {
    (int(a), int(u)) for a, u in
    (t.strip().split(":", 1) for t in os.environ.get("REGRADE_TARGETS", "").split(",") if ":" in t)
}
# 1提出あたりモデルに渡す画像の上限（図・スクリーンショット。コストと入力上限のため）
MAX_IMAGES_PER_SUBMISSION = int(os.environ.get("MAX_IMAGES_PER_SUBMISSION", "20"))

def html_to_text(s: str) -> str:
    """講評 HTML を改行つきの文字列にする（ログ確認用）。"""
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s or "")
    s = re.sub(r"<li>", "・", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _footer() -> str:
    return (
        "<hr><p>──────────<br>"
        "<strong>【AI支援採点に関する開示（Diligence）】</strong><br>"
        f"本評点・講評は生成AI（{MODEL}）の支援により作成された<strong>採点ドラフト</strong>です。"
        "最終的な評点・フィードバックは担当講師が確認・承認のうえ確定し、学生へ公開（リリース）します。"
        "本コメントはマーキングワークフロー上「採点完了（要講師レビュー）」段階で、未公開です。<br>"
        f"{_FOOTER_MARK} ／ 要講師確認</p>"
    )


# FIX(江尻指示 2026-08-29): AIが判断に迷った採点は、講師が真っ先に気づけるようにする。
#   講評の一番上に「?」付きの見出しブロックを差し込み、迷った理由と、
#   迷ったうえでどちらを採ってこの点にしたのかを書く。
_UNSURE_MARK = "AI-grading-uncertain"


def prepend_uncertainty_note(feedback_html: str, result: dict,
                              score_text: str = "") -> str:
    """迷いがあった採点の講評の先頭に、目立つ注意ブロックを差し込む。"""
    if not result.get("needs_human_review"):
        return feedback_html
    if _UNSURE_MARK in (feedback_html or ""):
        return feedback_html
    reason = (result.get("review_reason") or "").strip()
    if not reason:
        reason = ("理由が返されませんでした。点数の根拠が薄い可能性があるため、"
                  "提出物と採点表を照合してください。")
    # 改行を残す（(1)(2)(3) の区切りが1行に潰れて読めなくなるため）
    reason = "<br>".join(html.escape(line) for line in reason.splitlines() if line.strip())
    conf = {"high": "高", "medium": "中", "low": "低"}.get(result.get("confidence"), "不明")
    decided = f"<br>この迷いを踏まえて付けた評点：<strong>{score_text}</strong>" if score_text else ""
    return (
        f'<div data-mark="{_UNSURE_MARK}" '
        'style="border:2px solid #A3342A;background:#F7ECEA;'
        'padding:12px 14px;margin:0 0 14px;border-radius:3px">'
        '<p style="margin:0 0 6px;color:#A3342A;font-weight:bold;font-size:1.05em">'
        '❓ この採点はAIが判断に迷いました（講師の確認をお願いします）</p>'
        f'<p style="margin:0">{reason}{decided}'
        f'<br>AIの確信度：{conf}</p>'
        '<p style="margin:6px 0 0">公開（リリース）の前に、この枠を削除してください。</p>'
        "</div>"
    ) + (feedback_html or "")


# ---------- Moodle REST ----------
def _call(fn: str, params: dict[str, Any]) -> Any:
    data = {"wstoken": MOODLE_TOKEN, "wsfunction": fn, "moodlewsrestformat": "json",
             **{k: str(v) for k, v in params.items()}}
    r = httpx.post(ENDPOINT, data=data, timeout=60.0)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and j.get("exception"):
        raise RuntimeError(f"Moodle error [{j.get('errorcode')}]: {j.get('message')}")
    return j


def list_assignments(courseid: str) -> list[dict]:
    j = _call("mod_assign_get_assignments", {"courseids[0]": courseid})
    out = []
    for c in j.get("courses", []):
        for a in c.get("assignments", []):
            out.append({"id": a["id"], "cmid": a.get("cmid"), "name": a["name"],
                        "grademax": a.get("grade")})
    return out


# FIX: 評定ガイド（marking guide）が有効な課題では mod_assign_save_grade の grade は無視され、
#      評点が -1 のまま保存される。基準ごとの配点を advancedgradingdata で送る必要があるため、
#      課題ごとに評定ガイドの定義（基準ID・満点）を取得する。
def get_guide_criteria(cmid: int | None) -> list[dict] | None:
    """評定ガイドが有効なら基準一覧を返す。通常評定なら None。"""
    if not cmid:
        return None
    try:
        j = _call("core_grading_get_definitions", {"cmids[0]": cmid, "areaname": "submissions"})
    except Exception as e:
        log.warning("評定ガイド定義を取得できません cmid=%s: %s（通常評定として扱います）", cmid, e)
        return None
    for area in j.get("areas", []):
        if area.get("activemethod") != "guide":
            continue
        for d in area.get("definitions", []):
            crits = [
                {"id": c["id"],
                 "name": c.get("shortname") or (c.get("description") or "")[:40],
                 "maxscore": float(c.get("maxscore") or 0)}
                for c in (d.get("guide") or {}).get("guide_criteria", [])
            ]
            if crits:
                return crits
    return None


def list_pending(assignid: int) -> list[dict]:
    subs = _call("mod_assign_get_submissions", {"assignmentids[0]": assignid, "status": "submitted"}).get(
        "assignments", [])
    submitted = []
    for a in subs:
        for s in a.get("submissions", []):
            if s.get("status") == "submitted":
                submitted.append({"userid": s["userid"], "submissionid": s.get("id")})
    grades = _call("mod_assign_get_grades", {"assignmentids[0]": assignid}).get("assignments", [])
    graded_users = {g["userid"] for a in grades for g in a.get("grades", [])
                    if g.get("grade") not in (None, "", "-1.00000")}
    return [s for s in submitted if s["userid"] not in graded_users]


# FIX: 従来は添付ファイル名しか採点に渡しておらず、ファイル本体を読んでいなかった。
#      回答本体をWord等で提出させる課題では、中身を見ないまま「内容」を採点していたため、
#      提出ファイルをダウンロードして本文を抽出する。
def _download_file(fileurl: str) -> bytes:
    sep = "&" if "?" in fileurl else "?"
    r = httpx.get(f"{fileurl}{sep}token={MOODLE_TOKEN}", timeout=180.0, follow_redirects=True)
    r.raise_for_status()
    return r.content


# FIX(2026-09-26): 独自の抽出器は .docx/.pdf/.xlsx しか読めず、pptx で提出された回答本体を
#   「読めなかった」として扱い、厳格採点の指示と重なって0点近くになっていた。
#   MCP サーバと同じ extract.py（pptx・表・ノート・埋め込み画像に対応、単体テストあり）に統一する。
def extract_submitted_files(files: list[dict]) -> tuple[str, list[str], list[dict]]:
    """提出ファイルの本文と画像を抽出する。

    戻り値は (本文, 読めなかったファイルの説明, 画像[{label, data, format}])。
    図・スクリーンショットだけで構成されたスライドも採点できるよう、埋め込み画像も返す。
    """
    chunks: list[str] = []
    unreadable: list[str] = []
    images: list[dict] = []
    for f in files:
        name = f.get("filename") or ""
        url = f.get("fileurl") or ""
        size = int(f.get("filesize") or 0)
        mimetype = f.get("mimetype") or ""
        kind = extract.sniff(name, mimetype)
        if not url:
            unreadable.append(f"{name}（URLなし）")
            continue
        if size > MAX_FILE_BYTES:
            unreadable.append(f"{name}（{size}バイトで上限超過）")
            continue
        if kind == "unknown":
            unreadable.append(f"{name}（未対応の形式 {os.path.splitext(name)[1] or '不明'}）")
            continue
        try:
            blob = _download_file(url)
        except Exception as e:
            unreadable.append(f"{name}（ダウンロード失敗: {type(e).__name__}: {e}）")
            continue

        text = ""
        if kind != "image":
            res = extract.extract_text(blob, name, mimetype, max_chars=MAX_FILE_TEXT_CHARS)
            text = (res["text"] or "").strip()
            if res["note"].startswith("抽出に失敗"):
                unreadable.append(f"{name}（{res['note']}）")
                continue
            if res["truncated"]:
                text += "\n…（長すぎるため以降を省略）"

        room = MAX_IMAGES_PER_SUBMISSION - len(images)
        file_images: list[dict] = []
        if room > 0:
            imgs, _ = extract.extract_images(blob, name, mimetype, limit=room)
            file_images = [{"label": f"{name} / {im['name']}", "data": im["data"],
                            "format": im["format"]} for im in imgs]

        if not text and not file_images:
            unreadable.append(f"{name}（本文も画像も取り出せず）")
            continue
        images.extend(file_images)
        if text:
            chunks.append(f"----- ファイル: {name} -----\n{text}")
        else:
            chunks.append(f"----- ファイル: {name} -----\n（テキストなし。画像{len(file_images)}枚を添付）")
    return "\n\n".join(chunks), unreadable, images


def get_submission(assignid: int, userid: int) -> dict:
    st = _call("mod_assign_get_submission_status", {"assignid": assignid, "userid": userid})
    last = st.get("lastattempt", {}) or {}
    submission = last.get("submission", {}) or last.get("teamsubmission", {}) or {}
    onlinetext, files = "", []
    for p in submission.get("plugins", []):
        if p.get("type") == "onlinetext":
            for ef in p.get("editorfields", []):
                onlinetext += ef.get("text", "") or ""
        if p.get("type") == "file":
            for fa in p.get("fileareas", []):
                for f in fa.get("files", []):
                    files.append({"filename": f.get("filename", ""),
                                  "fileurl": f.get("fileurl", ""),
                                  "filesize": f.get("filesize", 0),
                                  "mimetype": f.get("mimetype", "")})
    file_text, unreadable, images = extract_submitted_files(files)
    return {"onlinetext": onlinetext,
            "files": [f["filename"] for f in files],
            "file_text": file_text,
            "unreadable": unreadable,
            "images": images}


def save_grade_draft(assignid: int, userid: int, course_id: str, feedback_html: str,
                      grade: float | None = None,
                      criteria_scores: list[dict] | None = None) -> None:
    if not ALLOW_WRITE:
        raise RuntimeError("write disabled: MOODLE_ALLOW_WRITE!=1")
    if str(course_id) not in WRITE_COURSES:
        raise RuntimeError(f"course {course_id} not in allowlist {sorted(WRITE_COURSES)}")
    fb = feedback_html or ""
    if _FOOTER_MARK not in fb:
        fb += _footer()
    params: dict[str, Any] = {
        "assignmentid": assignid, "userid": userid,
        "attemptnumber": -1, "addattempt": 0,
        "workflowstate": "readyforreview",  # ★未公開ドラフト固定。released にしない
        "applytoall": 1,
        "plugindata[assignfeedbackcomments_editor][text]": fb,
        "plugindata[assignfeedbackcomments_editor][format]": 1,
    }
    if criteria_scores:
        # FIX: 評定ガイド課題。grade は Moodle 側が基準の合計から算出するため -1 を渡す。
        params["grade"] = -1
        for i, cs in enumerate(criteria_scores):
            base = f"advancedgradingdata[guide][criteria][{i}]"
            params[f"{base}[criterionid]"] = cs["criterionid"]
            params[f"{base}[fillings][0][criterionid]"] = cs["criterionid"]
            params[f"{base}[fillings][0][score]"] = cs["score"]
            params[f"{base}[fillings][0][remark]"] = cs.get("remark", "")
            params[f"{base}[fillings][0][remarkformat]"] = 1
    else:
        params["grade"] = grade
    _call("mod_assign_save_grade", params)

    # FIX: mod_assign_save_grade は評定ガイド課題で grade を黙って無視し -1 を保存する。
    #      「保存したつもりで未採点のまま」を防ぐため、書き戻した値を必ず読んで確認する。
    stored = read_back_grade(assignid, userid)
    expected = sum(c["score"] for c in criteria_scores) if criteria_scores else grade
    if stored is None or stored < 0:
        raise RuntimeError(
            f"評点が保存されていません（期待 {expected}、実際 {stored}）。"
            "評定ガイドが有効な課題の可能性があります（advancedgradingdata が必要）。")
    if expected is not None and abs(stored - float(expected)) > 0.01:
        log.warning("保存された評点 %s が想定 %s と一致しません（assign=%s user=%s）",
                    stored, expected, assignid, userid)


def read_back_grade(assignid: int, userid: int) -> float | None:
    j = _call("mod_assign_get_grades", {"assignmentids[0]": assignid})
    for a in j.get("assignments", []):
        for g in a.get("grades", []):
            if g.get("userid") == userid:
                try:
                    return float(g.get("grade"))
                except (TypeError, ValueError):
                    return None
    return None


# ---------- 採点基準の取得（毎回最新化） ----------
def fetch_rubric_tabs() -> dict[str, list[list]]:
    """採点表スプレッドシートの全タブを読む（実行ごとに1回。採点前に毎回最新化）。"""
    from googleapiclient.discovery import build
    import google.auth

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=creds)
    meta = svc.spreadsheets().get(spreadsheetId=RUBRIC_SHEET_ID).execute()
    out: dict[str, list[list]] = {}
    for sh in meta.get("sheets", []):
        title = sh["properties"]["title"]
        res = svc.spreadsheets().values().get(
            spreadsheetId=RUBRIC_SHEET_ID, range=f"'{title}'!A1:E300").execute()
        out[title] = res.get("values", [])
    return out


def assignment_key(name: str) -> str | None:
    """Moodle の課題名から対応表のキー（事前課題2-1 等）を取り出す。"""
    m = re.match(r"\s*(事前課題\d(?:-\d)?|中間課題\d|修了レポート)", name or "")
    return m.group(1) if m else None


def rubric_for(assignment_name: str, tabs: dict[str, list[list]],
               criteria: list[dict] | None) -> tuple[rb.Rubric | None, dict[str, dict], str]:
    """課題に対応する採点表と、評定ガイドの基準（内容／AI使用ログ）の対応を返す。

    戻り値: (採点表, {"content": 基準, "ai": 基準}, 保留理由)。保留理由が空でなければ採点しない。
    採点表と評定ガイドの満点が食い違う場合も、どちらが正しいか判断できないため保留する。
    """
    key = assignment_key(assignment_name)
    conf = RUBRIC_MAP.get(key or "")
    if not conf:
        return None, {}, f"採点表の対応が未設定（{key or assignment_name}）"
    rows = tabs.get(conf["tab"])
    if rows is None:
        return None, {}, f"採点表にタブ「{conf['tab']}」がありません"
    rubric = rb.build_rubric(key, conf["tab"], rows, conf.get("part"))
    if not criteria:
        return None, {}, "評定ガイドが設定されていません"
    by_kind: dict[str, dict] = {}
    for c in criteria:
        kind = rb.AI if "AI" in c["name"].upper() else rb.CONTENT if "内容" in c["name"] else None
        if kind is None or kind in by_kind:
            return None, {}, f"評定ガイドの基準「{c['name']}」を内容／AI使用ログに対応付けられません"
        by_kind[kind] = c
    for kind, c in by_kind.items():
        if abs(rubric.max_of(kind) - c["maxscore"]) > 0.01:
            return None, {}, (f"満点が一致しません：採点表「{conf['tab']}」の{rb.KIND_LABEL[kind]}は"
                              f"{rubric.max_of(kind):g}点、評定ガイド「{c['name']}」は{c['maxscore']:g}点")
    return rubric, by_kind, ""


# ---------- Claude 採点 ----------
def build_grade_tool(rubric: rb.Rubric) -> dict:
    """採点表の減点項目だけを選べるツール定義。項目IDは enum で縛り、表に無い減点を構造的に防ぐ。"""
    fb_props = {
        "strengths": {"type": "array", "items": {"type": "string"},
                      "description": "評価できる点を2〜3個。1個1論点・1文60字以内・です／ます調"},
        "suggestions": {"type": "array", "items": {"type": "string"},
                        "description": "さらに良くするための提案を0〜2個。減点しない観点はここに書く。"
                                       "改善点だけを書き、褒める内容は strengths に書く。1個1論点・1文60字以内"},
    }
    ai_ids = [i.id for i in rubric.items() if rubric.section_of(i).kind == rb.AI]
    return {
        "name": "submit_grade",
        "description": "採点表の減点項目に照らした採点結果を返す。点数はシステムが計算する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "deductions": {
                    "type": "array",
                    "description": "該当した減点項目。該当なしなら空配列（満点）。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "string", "enum": [i.id for i in rubric.items()]},
                            "count": {"type": "integer", "minimum": 1,
                                      "description": "「1項目につき」「1か所につき」の項目は該当した件数。それ以外は1"},
                            "reason": {"type": "string",
                                       "description": "受講生向けに、なぜ該当するかを1〜2文で。です／ます調"},
                            "quote": {"type": "string",
                                      "description": "根拠となる提出物の記述をそのまま40字以内で引用"},
                            "location": {"type": "string",
                                         "description": "引用元（例：企画レポート.pptx スライド3、AI使用ログ 節目2）"},
                        },
                        "required": ["item_id", "count", "reason", "quote", "location"],
                    },
                },
                # FIX(2026-09-29): AI使用ログの項目は見落としが出たため（講師が付けた −1 を2件見逃した）、
                #   全項目について該当／非該当と根拠を返させ、確認を飛ばせないようにする。
                "ai_checks": {
                    "type": "array",
                    "description": "AI使用ログの減点項目すべてについての確認結果。一覧の項目を1つも漏らさず並べる。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "string", "enum": ai_ids},
                            "verdict": {"type": "string", "enum": ["該当", "非該当"]},
                            "evidence": {"type": "string",
                                         "description": "判断の根拠（どの節目・どの記述を確認したか）を1文で"},
                        },
                        "required": ["item_id", "verdict", "evidence"],
                    },
                },
                "feedback": {
                    "type": "object",
                    "properties": {rb.CONTENT: {"type": "object", "properties": fb_props},
                                   rb.AI: {"type": "object", "properties": fb_props}},
                    "required": [rb.CONTENT, rb.AI],
                },
                "closing": {"type": "string", "description": "次の課題に向けた一言（1文・60字以内）"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "needs_human_review": {"type": "boolean",
                                       "description": "減点項目への該当判断が割れる・機密配慮違反の疑いがあるときだけ true"},
                "review_reason": {
                    "type": "string",
                    "description": "講師向け。needs_human_review=true のとき必須。"
                                   "(1)何を迷ったか (2)どちらを採ってこの点にしたか (3)講師に何を確認してほしいか"
                                   " を、それぞれ改行して書く。",
                },
            },
            "required": ["deductions", "ai_checks", "feedback", "closing", "confidence",
                         "needs_human_review"],
        },
    }


# 減点項目の解釈（講師の判断）。項目の文言を広く読んで見逃すことがあったため、講師が実際に
# 減点した判断を項目ごとの注記としてモデルに渡す。キーは項目の文言に含まれる語。
# 出典: 2026-09-28 講師レビュー（受講生別採点表の「講師AI採点レビュー」「講師評価コメント」）
ITEM_NOTES = {
    "会話ログ": "プロンプトとAIの出力の要約の両方が必要。プロンプトだけで、AIが何を出力したかの記録が無ければ該当する。",
    "外部追加の出所": "主張（例：以前は〜だった、という変化）を裏付ける記録が無い場合、"
                   "本人が「記録を残していない」と書いている場合も該当する。",
}


def item_note(text: str) -> str:
    return next((f"（講師の解釈: {n}）" for k, n in ITEM_NOTES.items() if k in text), "")


def rubric_prompt(rubric: rb.Rubric) -> str:
    lines = []
    for s in rubric.sections:
        if s.shared:
            head = f"【{rb.KIND_LABEL[s.kind]}】{s.name}（課題全体で1回だけ判定する減点）"
        else:
            head = f"【{rb.KIND_LABEL[s.kind]}】{s.name}（満点 {s.max:g}点）"
        lines.append(head)
        for i in s.items:
            lines.append(f"  - [{i.id}] {i.text}　−{rb.unit_of(rubric, i):g}点{item_note(i.text)}")
    return "\n".join(lines)


STYLE_RULES = (
    "【講評の書き方（受講生が読む）】\n"
    "- です／ます調。1文は60字以内。1つの箇条に1つの論点だけを書く。\n"
    "- 受講生が知らない採点の内部用語を使わない（採点者A/B/C、合議、節目平均、内部修正点、"
    "外部追加点、整形明示、ルーブリック、§、項目ID など）。\n"
    "- 提出物を引用するときは「」でくくり、40字以内にする。\n"
    "- 見出し・記号・改行はシステムが付けるので、各欄には文だけを書く。\n"
)

# FIX(2026-09-29 江尻指示): 合議の3名にも採点表の項目以外では減点させない。
#   観点の違いは「どの項目に該当するかの見方」と「改善提案」に出す。
PANEL = [
    ("A_照合", "あなたは採点表との照合を担当する。減点項目を上から1つずつ、提出物に該当する記述があるかを確認する。"),
    ("B_実務", "あなたは実務家の目で読む。数値・計測・施策が減点項目に該当するかを、実務で通用するかの観点で厳密に判定する。"
               "採点表の項目に当たらない実務上の指摘は、改善提案に書く。"),
    ("C_教育", "あなたは指導者の目で読む。過剰な減点が無いか、引用が本当に項目に該当するかを点検する。"
               "理解の浅さが採点表の項目に当たらないときは、改善提案に書く。"),
]


def build_user_content(text: str, images: list[dict] | None) -> str | list[dict]:
    """画像があれば Anthropic Messages API の image ブロックとして本文の後ろに並べる。"""
    if not images:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}]
    for im in images:
        blocks.append({"type": "text", "text": f"[画像: {im['label']}]"})
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": f"image/{im['format']}",
            "data": base64.b64encode(im["data"]).decode("ascii")}})
    return blocks


def _system(rubric: rb.Rubric, stance: str = "") -> str:
    stance_block = f"【あなたの担当】\n{stance}\n\n" if stance else ""
    return (
        "あなたは上級ウェブ解析士認定講座の採点者です。採点の根拠は、下に示す採点表の減点項目だけです。\n"
        f"{stance_block}"
        "【採点のしかた（必ず従うこと）】\n"
        "- 減点は、下の一覧にある項目だけから選ぶ。一覧に無い観点では減点しない。\n"
        "- 減点するときは、該当を立証する提出物の記述を quote に引用する。引用で立証できない減点はしない。\n"
        "- 該当する項目は見逃さずに拾う（厳しめに採点する）。一方で、一覧のどの項目にも該当しなければ満点でよい。\n"
        "- 一覧に無い改善点・物足りない点は、減点せず feedback の suggestions に書く。\n"
        "- 点数はシステムが計算する。点数や合計を文章に書かない。\n"
        "- 回答内容の項目は提出ファイルの本文を根拠にする。AI使用ログの項目は、オンラインテキストのAI使用ログを根拠にする。\n"
        "- 減点項目への該当判断が割れるとき、機密配慮違反の疑いがあるときは needs_human_review=true にする。\n\n"
        "【AI使用ログの確認手順（全項目を ai_checks に記録する）】\n"
        "- 節目（AIを使った作業の区切り）ごとに、次を確認する。\n"
        "  (a) プロンプトの要約だけでなく、AIの出力の要約も記録されているか（無ければ「会話ログ（要約可）が提出されていない」に該当）\n"
        "  (b) 外部追加ごとに、いつ・どこで・何にもとづくかが書かれ、その記録で主張を裏付けられるか。"
        "「以前は〜だった」など変化を主張しているのに、変化前の記録が無い場合は、出所を特定できない外部追加として扱う\n"
        "  (c) 修正種類の自己申告が、書かれた修正内容と一致しているか\n"
        "  (d) ログに書かれた修正が、提出物に反映されているか\n"
        "- ai_checks で「該当」とした項目は、必ず deductions にも入れる。「非該当」とした項目は deductions に入れない。\n\n"
        f"{STYLE_RULES}\n"
        f"=== 採点表「{rubric.tab}」の減点項目（{rubric.key}） ===\n{rubric_prompt(rubric)}\n"
    )


def _user(assignment_name: str, onlinetext: str, files: list[str], file_text: str,
          images: list[dict]) -> str:
    image_note = ""
    if images:
        image_note = (f"提出ファイルに含まれる図・画像を{len(images)}枚、このメッセージの後ろに添付した"
                      "（各画像の直前に「ファイル名 / 画像名」を記す）。図・表・スクリーンショットの内容も"
                      "採点根拠にしてよい。\n")
    return (
        f"課題名: {assignment_name}\n"
        f"=== オンラインテキスト（多くの課題ではAI使用ログ） ===\n{onlinetext or '(なし)'}\n\n"
        f"=== 提出ファイルの本文（多くの課題では回答本体） ===\n{file_text or '(添付ファイルなし)'}\n\n"
        f"添付ファイル名: {', '.join(files) if files else '(なし)'}\n{image_note}\n"
        "submit_grade ツールで採点結果を返してください。"
    )


def _call_tool(client, system: str, content, tool: dict) -> tuple[dict | None, str]:
    resp = client.messages.create(
        model=MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": "submit_grade"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_grade":
            return block.input, resp.stop_reason
    return None, resp.stop_reason


def check_result(rubric: rb.Rubric, result: dict | None) -> tuple[list[dict], list[str]]:
    """モデルの出力を検証する。戻り値は (採用する減点, 問題点)。"""
    if result is None:
        return [], ["submit_grade が呼ばれませんでした"]
    # 型が崩れた出力（配列の要素が文字列など）で落ちないよう、先に形を確かめる
    for key, typ in (("deductions", list), ("ai_checks", list), ("feedback", dict)):
        if key in result and not isinstance(result[key], typ):
            return [], [f"{key} の形式が不正（{type(result[key]).__name__}）"]
    if any(not isinstance(x, dict) for k in ("deductions", "ai_checks") for x in result.get(k) or []):
        return [], ["deductions / ai_checks の要素がオブジェクトではない"]
    for k in (rb.CONTENT, rb.AI):
        v = (result.get("feedback") or {}).get(k)
        if v is not None and not isinstance(v, dict):
            return [], [f"feedback.{k} の形式が不正（{type(v).__name__}）"]
    problems = [f"必須項目が欠落: {k}" for k in ("deductions", "feedback") if k not in result]
    deductions, bad = rb.validate_deductions(rubric, result.get("deductions"))
    problems += bad
    fb = result.get("feedback") or {}
    texts = [result.get("closing") or ""]
    texts += [t for k in (rb.CONTENT, rb.AI) for f in ("strengths", "suggestions")
              for t in ((fb.get(k) or {}).get(f) or [])]
    texts += [d.get("reason") or "" for d in deductions]
    ai_ids = {i.id for i in rubric.items() if rubric.section_of(i).kind == rb.AI}
    checks = {str(c.get("item_id")): c.get("verdict") for c in (result.get("ai_checks") or [])}
    missing = sorted(ai_ids - set(checks))
    if missing:
        problems.append(f"ai_checks に確認していない項目がある: {missing}")
    deducted = {d["item_id"] for d in deductions if d["item_id"] in ai_ids}
    hit_ids = {k for k, v in checks.items() if v == "該当"}
    if deducted != hit_ids:
        problems.append(f"ai_checks の「該当」と deductions が一致しない: 該当={sorted(hit_ids)} 減点={sorted(deducted)}")
    hit = rb.forbidden_in(texts)
    if hit:
        problems.append(f"受講生向けの文に内部用語: {hit}")
    return deductions, problems


def _grade_once(client, rubric: rb.Rubric, assignment_name: str, onlinetext: str,
                files: list[str], file_text: str, images: list[dict], stance: str = "") -> dict:
    """1名分の採点。出力を検証し、問題があれば1回だけ直させる。直らなければ要確認にする。"""
    tool = build_grade_tool(rubric)
    system = _system(rubric, stance)
    user = _user(assignment_name, onlinetext, files, file_text, images)
    extra = ""
    result, problems = None, []
    for attempt in (1, 2):
        result, stop = _call_tool(client, system, build_user_content(user + extra, images), tool)
        deductions, problems = check_result(rubric, result)
        if not problems:
            return {**fill_defaults(result), "deductions": deductions}
        log.warning("採点出力に問題 (%d回目, stop=%s): %s", attempt, stop, problems)
        extra = ("\n\n重要: 前回の出力に次の問題がありました。直して返してください。\n- "
                 + "\n- ".join(problems))
    if result is None:
        raise RuntimeError(f"採点出力が得られませんでした: {problems}")
    result = fill_defaults(result)
    result["deductions"] = deductions
    result["needs_human_review"] = True
    result["review_reason"] = ((result.get("review_reason") or "") +
                               "\n(システム) 採点出力の検証で問題が残りました: " + " / ".join(problems)).strip()
    return result


def grade_submission(client, rubric: rb.Rubric, assignment_name: str, onlinetext: str,
                     files: list[str], file_text: str = "", images: list[dict] | None = None) -> dict:
    """性質の異なる3名で独立採点し、4人目が確定させる。PANEL_MODE=0 で単独採点。"""
    images = images or []
    if not PANEL_MODE:
        return _grade_once(client, rubric, assignment_name, onlinetext, files, file_text, images)
    drafts = []
    for name, stance in PANEL:
        try:
            d = _grade_once(client, rubric, assignment_name, onlinetext, files, file_text, images, stance)
            drafts.append((name, d))
            log.info("  合議 %s: 減点 %s", name, [f"{x['item_id']}x{x['count']}" for x in d["deductions"]])
        except Exception as e:
            log.warning("  合議 %s が失敗: %s: %s", name, type(e).__name__, e)
    if not drafts:
        raise RuntimeError("3名の採点がすべて失敗しました")
    if len(drafts) == 1:
        return drafts[0][1]
    return synthesize(client, rubric, drafts, assignment_name, onlinetext, files, file_text)


def synthesize(client, rubric: rb.Rubric, drafts: list[tuple[str, dict]], assignment_name: str,
               onlinetext: str, files: list[str], file_text: str) -> dict:
    """3名の採点案を突き合わせ、講師に渡す1本に確定させる（4人目）。"""
    tool = build_grade_tool(rubric)
    blocks = []
    for name, d in drafts:
        ded = "\n".join(f"  - [{x['item_id']}]×{x['count']} 「{x.get('quote', '')}」 {x.get('reason', '')}"
                        for x in d["deductions"]) or "  （減点なし）"
        blocks.append(f"--- 採点案{name[0]} ---\n{ded}\n  要確認: {d.get('needs_human_review')}")
    system = _system(rubric) + (
        "\n【あなたの役割】3名の採点者が独立に付けた減点案を突き合わせ、最終案を1本にまとめる。\n"
        "- 各減点は、引用が本当にその項目に該当するかを提出物で確かめてから採る。人数では決めない。\n"
        "- 1名だけが見つけた減点でも、引用で立証できていれば採る。立証できていなければ採らない。\n"
        "- 3名の判断が割れ、提出物でも決めきれない減点があれば needs_human_review=true にし、"
        "review_reason に理由を書く。\n"
        "- 評価できる点・改善提案は3案から重複を除いて選び直す。採点者が複数いたことは受講生向けの文に書かない。\n"
    )
    user = (_user(assignment_name, onlinetext, files, file_text, []) +
            "\n\n=== 3名の減点案 ===\n" + "\n\n".join(blocks))
    for attempt in (1, 2):
        result, stop = _call_tool(client, system, user, tool)
        deductions, problems = check_result(rubric, result)
        if not problems:
            log.info("  合議 統合: 減点 %s", [f"{x['item_id']}x{x['count']}" for x in deductions])
            return {**fill_defaults(result), "deductions": deductions}
        log.warning("統合の出力に問題 (%d回目, stop=%s): %s", attempt, stop, problems)
        user += "\n\n重要: 前回の出力に次の問題がありました。直して返してください。\n- " + "\n- ".join(problems)
    # 統合に失敗したら、最も減点の多い案を採る（甘い側に倒さない）。講師に確認を求める。
    def lost(d: dict) -> float:
        return rb.compute_scores(rubric, d["deductions"])["total"]
    picked = dict(min(drafts, key=lambda nd: lost(nd[1]))[1])
    picked["needs_human_review"] = True
    picked["review_reason"] = "3名の採点を統合する処理が失敗したため、最も厳しい案をそのまま採用しました。"
    return picked


def fill_defaults(result: dict) -> dict:
    """補助項目が欠けていた場合に安全側の既定値を入れる。"""
    result.setdefault("confidence", "low")
    if "needs_human_review" not in result:
        result["needs_human_review"] = True
        log.warning("needs_human_review が返らなかったため true（要確認）として扱います")
    result.setdefault("feedback", {})
    return result


# ---------- メイン ----------
def main() -> None:
    if not GRADE_COURSE_IDS:
        log.error("GRADE_COURSE_IDS が未設定です。何もせず終了します。")
        return
    if not ALLOW_WRITE:
        log.warning("MOODLE_ALLOW_WRITE!=1 のため、このジョブは採点結果を Moodle に書き込みません（ドライラン）。")

    log.info("採点基準スプレッドシートを読み込み中（毎回最新化）...")
    tabs = fetch_rubric_tabs()

    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    graded = 0
    results = []
    failed: list[dict] = []
    skipped: list[dict] = []
    for course_id in GRADE_COURSE_IDS:
        try:
            assignments = list_assignments(course_id)
        except Exception as e:
            log.error("list_assignments failed course=%s: %s", course_id, e)
            continue
        for a in assignments:
            if graded >= MAX_GRADE_PER_RUN:
                log.warning("MAX_GRADE_PER_RUN(%d) に到達。今回はここで打ち切ります。", MAX_GRADE_PER_RUN)
                break
            # FIX: list_pending の処理状況を可視化
            try:
                pending = list_pending(a["id"])
            except Exception as e:
                log.error("list_pending failed course=%s assign=%s(%s): %s", course_id, a["id"], a["name"], e)
                continue
            forced = sorted(u for aid, u in REGRADE_TARGETS
                            if aid == a["id"] and u not in {p["userid"] for p in pending})
            if forced:
                log.info("  再採点の指定: assign=%s user=%s", a["id"], forced)
                pending = pending + [{"userid": u} for u in forced]
            # FIX: pending 件数をログに出力して可視化
            log.info("処理中: course=%s assign=%s(%s) pending=%d件（残枠%d）", course_id, a["id"], a["name"],
                     len(pending), MAX_GRADE_PER_RUN - graded)
            if not pending:
                continue
            criteria = get_guide_criteria(a.get("cmid"))
            rubric, kinds, hold = rubric_for(a["name"], tabs, criteria)
            if hold:
                # 採点の根拠が確定しない課題は採点しない（講師が採点する）
                for p in pending:
                    skipped.append({"course": course_id, "assignment": a["id"],
                                    "userid": p["userid"], "reason": hold})
                log.warning("  採点保留（%s）: assign=%s(%s) %d件", hold, a["id"], a["name"], len(pending))
                continue
            log.info("  採点表: 「%s」%s 内容%g点・AI使用ログ%g点・減点項目%d個", rubric.tab,
                     f"（{rubric.key}）", rubric.max_of(rb.CONTENT), rubric.max_of(rb.AI),
                     len(rubric.items()))
            for p in pending:
                if graded >= MAX_GRADE_PER_RUN:
                    # FIX: MAX到達時に詳細ログを出力して、どこで止まったか明確に
                    log.warning("MAX_GRADE_PER_RUN(%d) に到達。assign=%s(%s) で打ち切り。", MAX_GRADE_PER_RUN,
                                a["id"], a["name"])
                    break
                userid = p["userid"]
                try:
                    sub = get_submission(a["id"], userid)
                    log.info("  提出: オンラインテキスト%d字 / ファイル本文%d字 / 画像%d枚 %s",
                             len(sub["onlinetext"] or ""), len(sub["file_text"] or ""),
                             len(sub["images"]),
                             f"/ 読めず: {sub['unreadable']}" if sub["unreadable"] else "")
                    # FIX(2026-09-26): 読めない提出ファイルがあるまま採点すると、中身を見ずに
                    #   減点して0点近くを付けてしまう。採点も保存もせず未採点のまま残し、講師に回す
                    #   （一時的なダウンロード失敗なら翌日の実行で拾い直される）。
                    if sub["unreadable"]:
                        skipped.append({"course": course_id, "assignment": a["id"], "userid": userid,
                                        "reason": "読めない提出ファイル", "unreadable": sub["unreadable"]})
                        log.warning("  採点保留（読めない提出ファイルあり・保存しない）: assign=%s(%s) "
                                    "user=%s %s", a["id"], a["name"], userid, sub["unreadable"])
                        continue
                    result = grade_submission(client, rubric, a["name"], sub["onlinetext"],
                                              sub["files"], sub["file_text"], sub["images"])
                    deductions = result["deductions"]
                    sc = rb.compute_scores(rubric, deductions)
                    grademax = {k: c["maxscore"] for k, c in kinds.items()}
                    scores = [{"criterionid": kinds[k]["id"], "score": sc[k],
                               "remark": rb.render_remark(rubric, k, deductions, sc)} for k in kinds]
                    grade_repr = "+".join(f"{sc[k]:g}" for k in kinds) + f"={sc['total']:g}"
                    # 講評はコードが型に沿って組み立てる。迷った採点は一番上に「?」ブロックを出す。
                    fb = prepend_uncertainty_note(
                        rb.render_feedback(rubric, result, deductions, sc, grademax), result, grade_repr)
                    if result.get("needs_human_review"):
                        log.info("  ❓ 迷いあり: %s",
                                 (result.get("review_reason") or "(理由なし)")[:300])
                    if not ALLOW_WRITE:
                        # ドライランのときだけ、保存しない講評を確認用にログへ出す
                        log.info("  講評（ドライラン・保存しない）:\n%s\n  評定ガイドのコメント: %s",
                                 html_to_text(fb), [c["remark"] for c in scores])
                    # FIX: 保存前に「採点結果」を出すと、保存で失敗しても成功したように見えるため
                    #      保存が終わってからログを出す。
                    if ALLOW_WRITE:
                        save_grade_draft(a["id"], userid, course_id, fb, criteria_scores=scores)
                    log.info("採点保存%s: course=%s assign=%s(%s) user=%s grade=%s review=%s",
                             "" if ALLOW_WRITE else "(ドライラン)",
                             course_id, a["id"], a["name"], userid, grade_repr,
                             result.get("needs_human_review"))
                    results.append({"course": course_id, "assignment": a["id"], "userid": userid,
                                     "grade": grade_repr,
                                     "needs_human_review": result.get("needs_human_review"),
                                     "written": ALLOW_WRITE})
                    graded += 1
                except Exception as e:
                    # FIX: 失敗を握り潰さず件数に数える（従来は完了ログに現れず成功に見えていた）
                    failed.append({"course": course_id, "assignment": a["id"], "userid": userid,
                                   "error": f"{type(e).__name__}: {e}"})
                    log.error("grading failed course=%s assign=%s(%s) user=%s: %s: %s", course_id,
                              a["id"], a["name"], userid, type(e).__name__, e)

    log.info("完了: 保存%d件 / 失敗%d件 / 保留%d件。要レビュー: %d件",
             graded, len(failed), len(skipped),
             sum(1 for r in results if r.get("needs_human_review")))
    if failed:
        log.error("失敗した採点: %s", json.dumps(failed, ensure_ascii=False))
    if skipped:
        log.warning("講師の採点が必要（保留）: %s", json.dumps(skipped, ensure_ascii=False))
    print(json.dumps({"graded": graded, "failed": failed, "skipped": skipped, "results": results},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
