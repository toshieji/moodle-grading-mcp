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
RUBRIC_SHEET_GID = os.environ.get("RUBRIC_SHEET_GID", "")  # 対象タブの gid（空なら表紙のみ）

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("GRADING_MODEL", "claude-haiku-4-5-20251001")
# 性質の異なる3名で独立採点し、4人目が確定させる合議制。0 にすると従来の単独採点に戻る。
PANEL_MODE = os.environ.get("GRADING_PANEL", "1") == "1"

_FOOTER_MARK = os.environ.get("MOODLE_AI_FOOTER_MARK", "AI-assisted grading")
_AI_USAGE_RUBRIC_PATH = os.environ.get("AI_USAGE_RUBRIC_PATH", "/app/ai-usage-log-rubric.md")

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

# 講評の長さ上限。修了レポートのみ長めに許容する。Moodle 側の上限ではなく運用上の目安。
# 2026-08-28 江尻指示で 400/1000 としたが、実際の講評は 750〜1100 字になり毎回書き直し
# （1件約10分・API費用が倍）になっていたため、2026-09-26 江尻指示で 4000/6000 に緩めた。
FEEDBACK_MAX_CHARS = int(os.environ.get("FEEDBACK_MAX_CHARS", "4000"))
FEEDBACK_MAX_CHARS_FINAL = int(os.environ.get("FEEDBACK_MAX_CHARS_FINAL", "6000"))
FINAL_REPORT_KEYWORD = os.environ.get("FINAL_REPORT_KEYWORD", "修了レポート")


def feedback_limit_for(assignment_name: str) -> int:
    return FEEDBACK_MAX_CHARS_FINAL if FINAL_REPORT_KEYWORD in (assignment_name or "") \
        else FEEDBACK_MAX_CHARS


def visible_len(html: str) -> int:
    """タグを除いた実際の文字数。空白・改行は数えない。"""
    text = re.sub(r"<[^>]+>", "", html or "")
    text = re.sub(r"&[a-zA-Z]+;|&#\d+;", "x", text)
    return len(re.sub(r"\s", "", text))


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
def fetch_rubric_sheet_text() -> str:
    """スプレッドシート（採点基準）を毎回読み直す。gid 指定タブ＋表紙を結合してテキスト化。"""
    from googleapiclient.discovery import build
    import google.auth

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=creds)
    meta = svc.spreadsheets().get(spreadsheetId=RUBRIC_SHEET_ID).execute()
    sheets = meta.get("sheets", [])
    titles = []
    if RUBRIC_SHEET_GID:
        for s in sheets:
            if str(s["properties"]["sheetId"]) == str(RUBRIC_SHEET_GID):
                titles.append(s["properties"]["title"])
    if not titles:
        titles = [sheets[0]["properties"]["title"]] if sheets else []

    chunks = []
    for title in titles:
        rng = f"'{title}'!A1:Z200"
        res = svc.spreadsheets().values().get(spreadsheetId=RUBRIC_SHEET_ID, range=rng).execute()
        rows = res.get("values", [])
        chunks.append(f"# シート: {title}\n" + "\n".join(" | ".join(r) for r in rows))
    return "\n\n".join(chunks)


def load_ai_usage_rubric() -> str:
    try:
        with open(_AI_USAGE_RUBRIC_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        log.warning("AI使用ログルーブリックが見つかりません: %s", _AI_USAGE_RUBRIC_PATH)
        return ""


# ---------- Claude (Vertex AI) 採点 ----------
GRADE_TOOL = {
    "name": "submit_grade",
    "description": "採点結果を構造化して返す。",
    "input_schema": {
        "type": "object",
        "properties": {
            "grade": {"type": "number", "description": "0〜100の点数（配点シートに従う）"},
            "feedback_html": {
                "type": "string",
                "description": "学生向けフィードバック（HTML）。観点ごとに◎/○/△/×と根拠（提出物からの引用）を明記。"
                               "AIが下書きした旨は本文にも一言触れる（末尾の開示フッターは別途システムが付与）。",
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "needs_human_review": {"type": "boolean", "description": "機密配慮違反・事実誤認・判断が割れる場合は true"},
            "review_reason": {
                "type": "string",
                "description": "採点に迷った点。needs_human_review=true のときは必須。"
                               "『何を迷ったか』『どちらの解釈を採ってその点数にしたか』"
                               "『講師に何を確認してほしいか』の3点を、"
                               "提出物の該当箇所を引いて具体的に書く。"
                               "迷わなかった場合は空でよい（迷っていないのに埋めない）。",
            },
        },
        "required": ["grade", "feedback_html", "confidence", "needs_human_review"],
    },
}


# FIX: 評定ガイド課題では合計点ではなく基準ごとの配点を返させる（合計は Moodle が算出する）。
def build_grade_tool(criteria: list[dict] | None) -> dict:
    if not criteria:
        return GRADE_TOOL
    tool = json.loads(json.dumps(GRADE_TOOL))
    props = tool["input_schema"]["properties"]
    props.pop("grade")
    props["criteria"] = {
        "type": "array",
        "description": "評定ガイドの各基準の採点。すべての基準を必ず1つずつ含めること。",
        "items": {
            "type": "object",
            "properties": {
                "criterionid": {"type": "integer", "description": "基準ID（提示したものをそのまま使う）"},
                "score": {"type": "number", "description": "その基準の点数（0以上、満点以下）"},
                "remark": {"type": "string", "description": "その基準の減点・加点理由。提出物からの引用を含める。"},
            },
            "required": ["criterionid", "score", "remark"],
        },
    }
    tool["input_schema"]["required"] = ["criteria", "feedback_html", "confidence", "needs_human_review"]
    return tool


# FIX(江尻指示 2026-08-29): 単独モデルの採点は観点が偏り、点数も実行ごとに振れる。
#   性質の異なる3名で独立に採点し、4人目が突き合わせて確定する合議制にする。
#   3名は「同じ質問に別々の角度から答える」ためのものなので、互いの答えは見せない。
PANEL = [
    ("A_減点照合", (
        "あなたは採点表との照合を専門とする採点者である。\n"
        "- 採点表の減点条件を上から一つずつ、提出物に該当する記述があるかを機械的に確認する。\n"
        "- 必須要件（指定の項目数・図・キャプチャ・指定フォーマット・字数）の欠落を最優先で拾う。\n"
        "- 提案の中身が良いかどうかは他の採点者に任せ、あなたは要件充足だけを見る。\n"
        "- 『書いてあるように読めなくもない』は不充足として扱う。"
    )),
    ("B_実務妥当性", (
        "あなたはウェブ解析の実務家として提出物を読む採点者である。\n"
        "- この施策・数値・計測設計が、実際の現場でそのまま通用するかを見る。\n"
        "- KPIの因果が飛んでいないか、数値の出所が示されているか、"
        "コストや工数の見積りが相場から外れていないか、計測が技術的に実現可能かを問う。\n"
        "- 形式が整っていても中身が空虚なら減点する。逆に形式の軽微な不備は他の採点者に任せる。"
    )),
    ("C_教育的観点", (
        "あなたは受講生の到達度を見る指導者としての採点者である。\n"
        "- 何が身についていて、何が身についていないかを切り分ける。\n"
        "- 提出物の背後にある理解を評価する。用語をなぞっただけか、自分の事業に落とせているか。\n"
        "- 過剰な減点を疑う役割も担う。形式不備で実質的な理解まで低く見積もっていないかを点検する。\n"
        "- ただし甘くつけてよいという意味ではない。理解が浅い箇所は明確に減点する。"
    )),
]


def grade_submission(client, rubric_text: str, ai_usage_rubric: str, assignment_name: str,
                      onlinetext: str, files: list[str],
                      criteria: list[dict] | None = None,
                      file_text: str = "", unreadable: list[str] | None = None,
                      feedback_limit: int = FEEDBACK_MAX_CHARS,
                      images: list[dict] | None = None) -> dict:
    """性質の異なる3名で独立採点し、4人目が確定させる。PANEL_MODE=0 で従来の単独採点。"""
    if not PANEL_MODE:
        return _grade_once(client, rubric_text, ai_usage_rubric, assignment_name, onlinetext,
                           files, criteria, file_text, unreadable, feedback_limit, images=images)
    drafts = []
    for name, stance in PANEL:
        try:
            d = _grade_once(client, rubric_text, ai_usage_rubric, assignment_name, onlinetext,
                            files, criteria, file_text, unreadable, feedback_limit, stance=stance,
                            images=images)
            drafts.append((name, d))
            log.info("  合議 %s: %s", name, _score_digest(d, criteria))
        except Exception as e:
            log.warning("  合議 %s が失敗: %s: %s", name, type(e).__name__, e)
    if not drafts:
        raise RuntimeError("3名の採点がすべて失敗しました")
    if len(drafts) == 1:
        log.warning("  合議: 1名しか成立しなかったため、その結果をそのまま採用します")
        return drafts[0][1]
    return synthesize(client, drafts, rubric_text, assignment_name, criteria, feedback_limit)


def _score_digest(d: dict, criteria: list[dict] | None) -> str:
    if criteria:
        return " / ".join(f"{c.get('criterionid')}={c.get('score')}"
                          for c in (d.get("criteria") or []))
    return str(d.get("grade"))


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


def _grade_once(client, rubric_text: str, ai_usage_rubric: str, assignment_name: str,
                onlinetext: str, files: list[str],
                criteria: list[dict] | None = None,
                file_text: str = "", unreadable: list[str] | None = None,
                feedback_limit: int = FEEDBACK_MAX_CHARS,
                stance: str = "", images: list[dict] | None = None) -> dict:
    tool = build_grade_tool(criteria)
    # FIX(江尻指示 2026-08-29): 基準ごとの remark は「減点根拠」であり、講師が判断を追うための欄。
    #   上限を課すと根拠が途中で切れて講師も判断がつかなくなるため、字数制限をかけない。
    #   全体講評（feedback_html）の上限は従来どおり維持する。
    remark_limit = None
    length_rule = (
        "【講評の長さ（厳守）】\n"
        f"- feedback_html は本文{feedback_limit}文字以内。タグを除いた文字数で数える。\n"
        "- 各基準の remark に字数制限はない。減点根拠は省略せず、"
        "何点引いたか・提出物のどの記述が根拠かを必要なだけ書く。\n"
        "- feedback_html では冗長な前置き、提出物の要約、励ましの定型文は書かない。"
        "何点減点したか、その根拠、次にどう直すかだけを簡潔に書く。\n"
        "- 見出しや箇条書きの多用で行数を稼がない。\n\n"
    )
    guide_note = ""
    if criteria:
        rows = "\n".join(f"  - criterionid={c['id']} 「{c['name']}」 満点 {c['maxscore']:g}点"
                         for c in criteria)
        guide_note = ("\n=== この課題の評定ガイド（基準ごとに採点すること） ===\n"
                      f"{rows}\n"
                      "各基準について criterionid をそのまま使い、0以上・満点以下の点数と理由を返すこと。"
                      "合計点は Moodle 側が算出するため、合計を返す必要はない。\n")
    stance_block = ""
    if stance:
        stance_block = ("【あなたの担当観点】\n"
                        f"{stance}\n"
                        "他の採点者が別の観点から同じ提出物を採点している。"
                        "あなたは自分の観点を最後まで貫くこと。全体のバランスを取ろうとしなくてよい。\n\n")
    system = (
        "あなたは上級ウェブ解析士認定講座の採点者です。以下の採点基準スプレッドシートの内容と、"
        "AI使用ログ採点ルーブリックに厳密に従い、提出物を採点してください。\n"
        f"{stance_block}"
        "厳守事項:\n"
        "- 点数だけでなく、必ず提出物からの具体的な引用を根拠として示すこと。\n"
        "- 機密配慮チェック（実在の顧客名・PII・第三者情報の無断使用等）に違反が1件でもあれば "
        "AI使用ログ該当部分は0点とし、needs_human_review=true にすること。\n"
        "- 事実誤認がある場合はその節目を0点とし、根拠を明記すること。\n"
        "- あなたはドラフトを作るだけであり、最終判断・公開は人間の講師が行う。フィードバックは"
        "『AIによる採点ドラフトである』ことが伝わる書き方にすること。\n"
        # FIX(江尻指示 2026-08-29): 迷いを隠さず、迷ったうえでの判断として明示させる。
        "【迷ったときの書き方（重要）】\n"
        "- 採点に迷ったら、迷いを消して断定するのではなく needs_human_review=true にし、"
        "review_reason に次の3点を必ず書くこと。\n"
        "  (1) 何を迷ったか（どの要件・どの記述の解釈が割れるか）\n"
        "  (2) どちらの解釈を採って、その結果この点数にしたか\n"
        "  (3) 講師に何を確認してほしいか\n"
        "- review_reason には提出物の該当箇所を引用すること。抽象的な『判断が難しい』は書かない。\n"
        "- 迷っていないのに needs_human_review=true にしてはならない。"
        "本当に解釈が割れる論点があるときだけ立てること。\n\n"
        "- 判断に迷う場合は needs_human_review=true にし、理由を書くこと（保留せず0点扱いにはしない。"
        "ただし機密配慮違反・事実誤認は上記の通り即0点）。\n\n"
        # 2026-08-28 江尻指示：甘い採点は講師レビューの意味を失わせるため、厳格側に倒す。
        "【採点の厳しさ（必ず従うこと）】\n"
        "- この採点は講師が確認するための素案である。甘くつけると確認の意味がなくなる。\n"
        "- 合格ラインは満点の7割。平均的な提出物が『不合格〜ギリギリ合格』（満点の6〜7割）に"
        "収まるよう厳格に採点すること。\n"
        "- 満点は原則としてつけない。満点にしてよいのは、要求事項をすべて満たしていることを"
        "提出物の具体的な記述で一つ残らず立証できる場合のみである。\n"
        "- 減点条件に形式的にでも該当するものは必ず減点する。見逃して加点する側に倒さない。\n"
        "- 『概ね良い』『特に問題ない』という印象で点を与えてはならない。加点は提出物の"
        "該当箇所を引用できるときに限る。\n"
        "- 判断が割れる論点、記述が薄い観点、裏付けのない主張は減点し、その旨を講評に書くこと。\n"
        "- 点数の目安: 満点の9割超は『模範解答として他の受講生に配布できる』水準に限る。"
        "要求を一通り満たしただけの提出物は7割（合格ライン）。"
        "どこかに不足・薄さがあれば6割台（不合格）に落とす。\n"
        "- 各基準について、適用した減点を最低1つは具体的に挙げること。"
        "減点が1つも見つからない場合は、減点条件を見落としていないか一覧を再確認したうえで、"
        "なぜ無減点なのかを提出物の記述を引いて説明すること。\n"
        "- 採点は減点方式で計算すること。加点を積み上げるのではなく、満点から出発し、"
        "採点表の減点条件に該当するものを1件ずつ差し引く。remark には"
        "『満点−(条件A)−(条件B)=点数』の形で、適用した減点条件とその根拠となる"
        "提出物の記述を必ず明示すること。\n"
        "- 減点条件は『明確に満たしている』と言い切れない限り該当と見なす。"
        "受講生に有利な解釈で見逃してはならない。\n"
        "- 採点を確定する前に自己点検すること: 合計が満点の8割を超えているなら、"
        "『甘すぎないか』『引用した根拠は本当に要求を満たしているか』を見直し、"
        "根拠が薄い加点は取り消すこと。\n\n"
        f"{length_rule}"
        f"=== 採点基準スプレッドシート ===\n{rubric_text}\n\n"
        f"=== AI使用ログ採点ルーブリック ===\n{ai_usage_rubric}\n"
        f"{guide_note}"
    )
    # FIX: 提出ファイルの本文を採点対象に含める。読めなかった場合はその旨を明示し、
    #      中身を見ずに満点を付けることがないようにする。
    unreadable = unreadable or []
    warn = ""
    if unreadable:
        warn = ("\n【重要】次の提出ファイルは本文を読み取れませんでした: "
                f"{', '.join(unreadable)}\n"
                "読めなかった提出物の内容について推測で加点してはならない。"
                "その観点は満点にせず、needs_human_review=true とし、"
                "講評に『ファイルを確認できなかったため講師の確認が必要』と明記すること。\n")
    images = images or []
    image_note = ""
    if images:
        image_note = (f"提出ファイルに含まれる図・画像を{len(images)}枚、このメッセージの後ろに添付した"
                      "（各画像の直前に「ファイル名 / 画像名」を記す）。図・表・スクリーンショットの内容も"
                      "採点根拠にしてよい。画像から読み取った内容を引用するときは、どの画像かを示すこと。\n")
    user = (
        f"課題名: {assignment_name}\n"
        f"=== オンラインテキスト（多くの課題ではAI使用ログ） ===\n"
        f"{onlinetext or '(なし)'}\n\n"
        f"=== 提出ファイルの本文（多くの課題では回答本体） ===\n"
        f"{file_text or '(添付ファイルなし、または本文を抽出できませんでした)'}\n\n"
        f"添付ファイル名: {', '.join(files) if files else '(なし)'}\n"
        f"{image_note}"
        f"{warn}\n"
        "注意: オンラインテキストのAI使用ログは『AIをどう使ったか』の記録であり、"
        "回答本体そのものではない。回答内容の観点は、必ず提出ファイルの本文を根拠に採点し、"
        "AI使用ログの自己申告を回答本体の記述として引用しないこと。\n\n"
        "上記の採点基準に従い submit_grade ツールで採点結果を返してください。"
    )
    # FIX: max_tokens 不足で tool_use の JSON が途中で切れ、feedback_html が欠落して
    #      KeyError になり採点が保存されない事故があったため、出力枠を広げたうえで
    #      stop_reason と必須キーを検証し、欠けていれば簡潔化を指示して1回だけ再試行する。
    # FIX: confidence 等の補助項目が欠けただけで採点全体を落とすのは過剰。
    #      点数と講評という本質的な項目だけを必須とし、残りは既定値で補う。
    required = ["criteria", "feedback_html"] if criteria else ["grade", "feedback_html"]
    last_err = ""
    extra = ""
    for attempt in (1, 2):
        resp = client.messages.create(
            # NOTE: 導入済み SDK の messages.create は temperature を受け付けないため指定しない。
            # 点数のばらつきは減点方式の明示（システムプロンプト）で抑える。
            model=MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=system,
            tools=[tool], tool_choice={"type": "tool", "name": "submit_grade"},
            messages=[{"role": "user", "content": build_user_content(user + extra, images)}],
        )
        tool_input = None
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_grade":
                tool_input = block.input
                break
        if tool_input is None:
            last_err = f"submit_grade が呼ばれませんでした (stop_reason={resp.stop_reason})"
            extra = ("\n\n重要: 前回は出力上限で応答が切れました。"
                     f"feedback_html を{feedback_limit}文字以内に収めてください。")
        else:
            missing = [k for k in required if k not in tool_input]
            if missing:
                last_err = (f"必須項目が欠落: {missing} (stop_reason={resp.stop_reason}, "
                            f"output_tokens={resp.usage.output_tokens})")
                extra = ("\n\n重要: 前回は出力上限で応答が切れ、項目が欠落しました。"
                         f"feedback_html を{feedback_limit}文字以内に収めてください。")
            else:
                # 江尻指示の文字数上限。超えていたら一度だけ短く書き直させる。
                # 機械的に切り詰めると文の途中で切れるため、再生成を優先する。
                # remark（減点根拠）は字数を見ない。全体講評だけを上限判定する。
                fb_len = visible_len(tool_input.get("feedback_html", ""))
                if fb_len <= feedback_limit:
                    return fill_defaults(tool_input)
                last_err = f"講評が長すぎます: feedback_html が{fb_len}文字（上限{feedback_limit}文字）"
                extra = (f"\n\n重要: 前回の feedback_html が{fb_len}文字で上限を超えました。"
                         "減点の根拠と改善点だけに絞り、文を途中で切らずに"
                         f"{feedback_limit}文字以内で書き直してください。"
                         "各基準の remark は字数制限がないので短くしないでください。")
                if attempt == 2:
                    log.warning("2回目も上限超過（%s）。末尾を切り詰めます。", last_err)
                    return fill_defaults(trim_feedback(tool_input, feedback_limit, remark_limit))
        log.warning("採点出力が不完全 (%d回目): %s", attempt, last_err)
    raise RuntimeError(f"採点出力が不完全なため中止: {last_err}")


def synthesize(client, drafts: list[tuple[str, dict]], rubric_text: str,
               assignment_name: str, criteria: list[dict] | None,
               feedback_limit: int) -> dict:
    """3名の採点案を突き合わせ、講師が読む1本の採点に確定させる（4人目）。"""
    tool = build_grade_tool(criteria)
    blocks = []
    for name, d in drafts:
        if criteria:
            rows = "\n".join(
                f"  基準{c.get('criterionid')}: {c.get('score')}点\n  根拠: {c.get('remark') or ''}"
                for c in (d.get("criteria") or []))
        else:
            rows = f"  評点: {d.get('grade')}点"
        blocks.append(f"--- 採点者{name} ---\n{rows}\n  講評: "
                      f"{re.sub(r'<[^>]+>', ' ', d.get('feedback_html') or '')}\n"
                      f"  要人手確認: {d.get('needs_human_review')}")
    guide_note = ""
    if criteria:
        guide_note = "\n".join(f"  - criterionid={c['id']} 「{c['name']}」 満点 {c['maxscore']:g}点"
                               for c in criteria)
        guide_note = f"\n=== この課題の評定ガイド ===\n{guide_note}\n"
    system = (
        "あなたは上級ウェブ解析士認定講座の主任採点者である。"
        "性質の異なる3名の採点者が、同じ提出物を独立に採点した。"
        "あなたの仕事は、3案を突き合わせて講師に渡す最終案を1本にまとめることである。\n\n"
        "【確定のしかた】\n"
        "- 点数は平均や多数決で決めない。提出物の具体的な記述を引用できている根拠が"
        "最も強い意見を採る。根拠のない主張は、何名が言っていても採らない。\n"
        "- 3名の点が割れた基準は、なぜその点にしたかを remark に一行で書く。\n"
        "- ある採点者だけが見つけた減点でも、根拠が具体的なら採用する。"
        "見落としを拾うのが3名で採点する目的である。\n"
        "- 誰か1名でも機密配慮違反・事実誤認を指摘していたら、その指摘を検証し、"
        "妥当なら該当部分を0点にして needs_human_review=true にする。\n"
        "- 3名の点が大きく割れた（満点の2割以上の開き）場合は needs_human_review=true にする。\n\n"
        "【迷いの申告（重要）】\n"
        "- needs_human_review=true にしたときは、review_reason に次の3点を必ず書く。\n"
        "  (1) 3名のどこが割れたか、または何の解釈が定まらないか\n"
        "  (2) どちらを採って、その結果この点数にしたか\n"
        "  (3) 講師に何を確認してほしいか\n"
        "- 提出物の該当箇所を引いて具体的に書く。『判断が難しい』だけでは不可。\n"
        "- 3名の意見が一致し、根拠も揃っているなら needs_human_review=false でよい。"
        "迷っていないのに立てない。\n\n"
        "【講評の書き方（最重要）】\n"
        "- 講師が短時間で判断できることだけを目的に書く。3名の議論の経過は書かない。\n"
        f"- feedback_html は本文{feedback_limit}文字以内。結論から書く。\n"
        "- 各基準の remark に字数制限はない。減点根拠は"
        "『満点−(条件A)−(条件B)=点数』の形で、根拠となる提出物の記述とともに省略せず書く。\n"
        "- 3名の採点者名（A/B/C）を講評に出さない。1人の採点者が書いたように読めること。\n"
        f"{guide_note}\n"
        f"=== 採点基準スプレッドシート ===\n{rubric_text}\n"
    )
    user = (f"課題名: {assignment_name}\n\n" + "\n\n".join(blocks) +
            "\n\n上記3案を踏まえ、submit_grade ツールで最終的な採点を1本返してください。")
    resp = client.messages.create(
        model=MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": "submit_grade"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_grade":
            out = block.input
            if visible_len(out.get("feedback_html", "")) > feedback_limit:
                out = trim_feedback(out, feedback_limit)
            log.info("  合議 統合: %s", _score_digest(out, criteria))
            return fill_defaults(out)
    # 統合に失敗したら、独立採点のうち最も辛いものを採る（甘い側に倒さない）。
    log.warning("  合議 統合に失敗（stop_reason=%s）。最も厳しい採点案を採用します", resp.stop_reason)
    def total(d: dict) -> float:
        if criteria:
            return sum(float(c.get("score") or 0) for c in (d.get("criteria") or []))
        return float(d.get("grade") or 0)
    spread = ", ".join(f"{n}={total(d):g}点" for n, d in drafts)
    picked = min(drafts, key=lambda nd: total(nd[1]))[1]
    picked["needs_human_review"] = True
    picked["review_reason"] = (
        f"3名の採点を統合する処理が失敗したため、最も厳しい採点案をそのまま採用しました"
        f"（3名の合計点: {spread}）。統合を経ていないため、"
        "点数と講評が他の観点を反映できていません。講師による確認をお願いします。")
    return picked


def fill_defaults(result: dict) -> dict:
    """補助項目が欠けていた場合に安全側の既定値を入れる。"""
    if "confidence" not in result:
        result["confidence"] = "low"
        log.warning("confidence が返らなかったため low として扱います")
    if "needs_human_review" not in result:
        result["needs_human_review"] = True
        log.warning("needs_human_review が返らなかったため true（要確認）として扱います")
    return result


def _cut_at_sentence(text: str, limit: int) -> str:
    """上限以内で、できるだけ文の切れ目（句点）で切る。語の途中で切らないため。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for mark in ("。", "\n", "、"):
        pos = head.rfind(mark)
        if pos >= limit * 0.6:  # 極端に短くなるなら諦めて素直に切る
            return head[:pos + 1]
    return head + "…"


def trim_feedback(result: dict, feedback_limit: int, remark_limit: int | None = None) -> dict:
    """上限を超えた全体講評を安全に切り詰める（再生成しても収まらなかったときの最後の砦）。

    remark（減点根拠）は講師が判断を追う欄なので、remark_limit が None なら一切切らない。
    """
    fb = result.get("feedback_html") or ""
    if visible_len(fb) > feedback_limit:
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fb)).strip()
        result["feedback_html"] = f"<p>{_cut_at_sentence(plain, feedback_limit)}</p>"
    if remark_limit is not None:
        for c in result.get("criteria") or []:
            r = c.get("remark") or ""
            if len(r) > remark_limit:
                c["remark"] = _cut_at_sentence(r, remark_limit)
    return result


# FIX: LLM が基準を取りこぼしたり満点を超える点数を返すことがあるため、保存前に検証する。
def normalize_criteria_scores(returned: Any, criteria: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in criteria}
    got: dict[int, dict] = {}
    for item in returned or []:
        try:
            cid = int(item["criterionid"])
        except (KeyError, TypeError, ValueError):
            continue
        if cid in by_id:
            got[cid] = item
    missing = [by_id[cid]["name"] for cid in by_id if cid not in got]
    if missing:
        raise RuntimeError(f"評定ガイドの基準が採点されていません: {missing}")

    out = []
    for cid, c in by_id.items():
        score = float(got[cid].get("score") or 0)
        if score < 0 or score > c["maxscore"]:
            clipped = min(max(score, 0.0), c["maxscore"])
            log.warning("基準『%s』の点数 %g が範囲外のため %g に補正しました（満点 %g）",
                        c["name"], score, clipped, c["maxscore"])
            score = clipped
        out.append({"criterionid": cid, "score": score,
                    "remark": (got[cid].get("remark") or "")})
    return out


# ---------- メイン ----------
def main() -> None:
    if not GRADE_COURSE_IDS:
        log.error("GRADE_COURSE_IDS が未設定です。何もせず終了します。")
        return
    if not ALLOW_WRITE:
        log.warning("MOODLE_ALLOW_WRITE!=1 のため、このジョブは採点結果を Moodle に書き込みません（ドライラン）。")

    log.info("採点基準スプレッドシートを読み込み中（毎回最新化）...")
    rubric_text = fetch_rubric_sheet_text()
    ai_usage_rubric = load_ai_usage_rubric()

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
            criteria = get_guide_criteria(a.get("cmid")) if pending else None
            if criteria:
                log.info("  評定ガイド: %s",
                         " / ".join(f"{c['name']}({c['maxscore']:g}点)" for c in criteria))
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
                        skipped.append({"course": course_id, "assignment": a["id"],
                                        "userid": userid, "unreadable": sub["unreadable"]})
                        log.warning("  採点保留（読めない提出ファイルあり・保存しない）: assign=%s(%s) "
                                    "user=%s %s", a["id"], a["name"], userid, sub["unreadable"])
                        continue
                    result = grade_submission(client, rubric_text, ai_usage_rubric, a["name"],
                                               sub["onlinetext"], sub["files"], criteria,
                                               sub["file_text"], sub["unreadable"],
                                               feedback_limit_for(a["name"]),
                                               images=sub["images"])
                    scores = None
                    if criteria:
                        scores = normalize_criteria_scores(result.get("criteria"), criteria)
                        shown = "+".join(f"{s['score']:g}" for s in scores)
                        total = sum(s["score"] for s in scores)
                        grade_repr = f"{shown}={total:g}"
                    else:
                        grade_repr = f"{float(result['grade']):g}"
                    # FIX(江尻指示 2026-08-29): 迷った採点は講評の一番上に「?」ブロックを出す。
                    fb = prepend_uncertainty_note(result["feedback_html"], result, grade_repr)
                    if result.get("needs_human_review"):
                        log.info("  ❓ 迷いあり: %s",
                                 (result.get("review_reason") or "(理由なし)")[:200])
                    # FIX: 保存前に「採点結果」を出すと、保存で失敗しても成功したように見えるため
                    #      保存が終わってからログを出す。
                    if ALLOW_WRITE:
                        save_grade_draft(a["id"], userid, course_id, fb,
                                          grade=None if scores else float(result["grade"]),
                                          criteria_scores=scores)
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

    log.info("完了: 保存%d件 / 失敗%d件 / 保留（読めないファイル）%d件。要レビュー: %d件",
             graded, len(failed), len(skipped),
             sum(1 for r in results if r.get("needs_human_review")))
    if failed:
        log.error("失敗した採点: %s", json.dumps(failed, ensure_ascii=False))
    if skipped:
        log.warning("講師の採点が必要（読めない提出ファイル）: %s", json.dumps(skipped, ensure_ascii=False))
    print(json.dumps({"graded": graded, "failed": failed, "skipped": skipped, "results": results},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
