#!/usr/bin/env python3
"""Moodle Grading MCP server / Moodle 採点 MCP サーバ（汎用）。

Exposes Moodle Web Services REST as MCP tools. Holds no grading "brain" (the LLM client decides
grades); it only does I/O — fetching submissions and writing grades. Writes enforce safe rules
server-side:
  - workflowstate=readyforreview (graded but UNRELEASED). **Never set to released.**
  - No student notification (Moodle does not notify for draft state).
  - Appends an AI-assistance disclosure footer if missing (override via MOODLE_AI_FOOTER_FILE).
  - Writes only to allowlisted course IDs (MOODLE_WRITE_COURSE_ALLOWLIST). Empty = no write target.

リモート/公開向け:
  - transport: MCP_TRANSPORT=stdio(default) | streamable-http | sse （mcp.run）。
  - 認証: MCP_BEARER_TOKENS（カンマ区切り）で Bearer 検証を有効化（HTTP公開時は必須）。
  - 監査ログ: save_grade_draft を JSONL で MOODLE_AUDIT_LOG（既定 mcp/audit.log）へ追記。
  - ログ: stdout は JSON-RPC のため汚さない。サーバログは stderr、クライアントへは MCP logging 通知。

.env (place in the PARENT directory of this file — the code loads ../.env — or export):
  MOODLE_URL="https://moodle.example.com"
  MOODLE_TOKEN="<token of a user with grading permission>"
  MOODLE_ALLOW_WRITE=0                       # 1 to enable writes
  MOODLE_WRITE_COURSE_ALLOWLIST=            # comma-separated course IDs allowed to write (empty=none)
  MOODLE_AI_FOOTER_FILE=                    # optional: path to a custom disclosure footer (HTML)
  MOODLE_MAX_FILE_MB=150                    # 提出ファイル1件の取得上限(MB)
  MOODLE_MAX_TEXT_CHARS=800000              # 抽出テキストの上限(文字)
  MOODLE_MAX_IMAGES=20                      # 1回に返す画像の上限(枚)
  MOODLE_IMAGE_MAX_PX=1568                  # 画像の長辺上限(px)。超えたら縮小

依存: mcp(<2), httpx, python-dotenv, python-docx, python-pptx, openpyxl, pypdf, pillow
起動（stdio）: python server.py / 起動（HTTP）: MCP_TRANSPORT=streamable-http ... python server.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context, Image

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # extract.py を server.py の隣から確実に読む
    sys.path.insert(0, _HERE)
import extract  # noqa: E402  (sys.path 調整後に読む必要がある)

# .env は moodle ディレクトリ直下（mcp の一つ上）
load_dotenv(os.path.join(_HERE, "..", ".env"))

MOODLE_URL = os.environ.get("MOODLE_URL", "").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "")
ALLOW_WRITE = os.environ.get("MOODLE_ALLOW_WRITE", "0") == "1"
WRITE_COURSES = {
    c.strip()
    for c in os.environ.get("MOODLE_WRITE_COURSE_ALLOWLIST", "").split(",")
    if c.strip()
}
ENDPOINT = f"{MOODLE_URL}/webservice/rest/server.php"
AUDIT_PATH = os.environ.get("MOODLE_AUDIT_LOG", os.path.join(_HERE, "audit.log"))

# 提出ファイルの取得上限。修了レポートの pptx は数十MBになるため既定は大きめ。
MAX_FILE_MB = int(os.environ.get("MOODLE_MAX_FILE_MB", "150"))
MAX_TEXT_CHARS = int(os.environ.get("MOODLE_MAX_TEXT_CHARS", "800000"))
MAX_IMAGES = int(os.environ.get("MOODLE_MAX_IMAGES", "20"))
IMAGE_MAX_PX = int(os.environ.get("MOODLE_IMAGE_MAX_PX", "1568"))
DOWNLOAD_TIMEOUT = float(os.environ.get("MOODLE_DOWNLOAD_TIMEOUT", "180"))

# AI開示フッター。MOODLE_AI_FOOTER_FILE（HTMLファイル）で全文を差し替え可能。
# 検出マークは MOODLE_AI_FOOTER_MARK で上書き可（フッター文面に必ず含める語にすること）。
_FOOTER_MARK = os.environ.get("MOODLE_AI_FOOTER_MARK", "AI-assisted grading")


def _load_footer() -> str:
    p = os.environ.get("MOODLE_AI_FOOTER_FILE")
    if p and os.path.exists(p):
        return open(p, encoding="utf-8").read()
    return (
        "<hr><p>──────────<br>"
        "<strong>[AI-assisted grading disclosure / AI支援採点に関する開示]</strong><br>"
        "This grade and feedback were drafted with the help of generative AI. "
        "The final grade/feedback is confirmed and released by the grader after review. "
        "本評点・講評は生成AIの支援で作成された採点ドラフトで、採点者の確認・承認後に公開されます。<br>"
        "AI-assisted grading / pending review</p>"
    )


_FALLBACK_FOOTER = _load_footer()

# ---------- logging（stdout は JSON-RPC のため使わない。stderr へ） ----------
logging.basicConfig(
    level=os.environ.get("MOODLE_LOG_LEVEL", "INFO").upper(),
    stream=sys.stderr,
    format="%(asctime)s moodle-grading %(levelname)s %(message)s",
)
log = logging.getLogger("moodle-grading")


# ---------- 認証（Bearer / TokenVerifier）----------
def _auth_kwargs() -> dict:
    """MCP_BEARER_TOKENS が設定されていれば FastMCP に渡す token_verifier/auth を構築する。
    静的トークン検証（OAuth 認可サーバ不要の Resource Server）。claude.ai の一般公開コネクタには
    別途 OAuth 2.1 認可サーバが要る（README 参照）。"""
    raw = os.environ.get("MCP_BEARER_TOKENS", "").strip()
    if not raw:
        return {}
    from mcp.server.auth.provider import TokenVerifier, AccessToken
    from mcp.server.auth.settings import AuthSettings

    tokens = {t.strip() for t in raw.split(",") if t.strip()}
    scopes = ["mcp"]

    class _StaticVerifier(TokenVerifier):
        async def verify_token(self, token: str):
            if token in tokens:
                return AccessToken(token=token, client_id="static", scopes=scopes, expires_at=None)
            return None

    issuer = os.environ.get("MCP_ISSUER_URL", "http://localhost:8000")
    resource = os.environ.get("MCP_RESOURCE_SERVER_URL", issuer)
    return {
        "token_verifier": _StaticVerifier(),
        "auth": AuthSettings(issuer_url=issuer, resource_server_url=resource, required_scopes=scopes),
    }


def _server_kwargs() -> dict:
    kw: dict = {}
    host = os.environ.get("MCP_HOST")
    port = os.environ.get("MCP_PORT")
    if host:
        kw["host"] = host
    if port:
        kw["port"] = int(port)
    kw.update(_auth_kwargs())
    return kw


mcp = FastMCP("moodle-grading", **_server_kwargs())


# ---------- 監査ログ / クライアントログ ----------
def _token_tail() -> str:
    return ("…" + MOODLE_TOKEN[-4:]) if len(MOODLE_TOKEN) >= 4 else "(unset)"


def _caller() -> str:
    """認証主体（HTTP/Bearer/OAuth クライアントの識別）。stdio/無認証時は 'local'。
    コネクタ化(フェーズ2)で『誰が採点したか』を監査に残すための識別子。OAuth subject がそのまま入る。"""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        tok = get_access_token()
        if tok:
            return tok.subject or tok.client_id or "authed"
    except Exception:
        pass
    return "local"


def _audit(event: str, **fields) -> None:
    """成績書き込み等の監査記録を JSONL で追記（トークンは末尾4桁のみ・認証主体つき）。"""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
           "caller": _caller(), "token": _token_tail(), **fields}
    line = json.dumps(rec, ensure_ascii=False)
    log.info("AUDIT %s", line)
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log.error("audit write failed: %s", e)


async def _safe_log(ctx, level, msg):
    """stderr ロガー＋（可能なら）クライアントへ MCP logging 通知。失敗してもツールを止めない。"""
    log.log(getattr(logging, level.upper(), logging.INFO), msg)
    if ctx is None:
        return
    try:
        await getattr(ctx, level)(msg)
    except Exception:
        pass


def _strip_html(s: str, limit: int = 600) -> str:
    """HTML タグを除去し空白を畳んで返す（コース概要・ラベル本文の要約表示用・読み取り）。"""
    if not s:
        return ""
    text = re.sub(r"<[^>]+>", " ", s)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _call(fn: str, params: dict[str, Any]) -> Any:
    """REST 呼び出し。Moodle はエラーも HTTP 200 で返すため例外フィールドを検査する。"""
    if not MOODLE_URL or not MOODLE_TOKEN:
        raise RuntimeError(".env の MOODLE_URL / MOODLE_TOKEN が未設定です（TOKEN-setup.md 参照）")
    data = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": fn,
        "moodlewsrestformat": "json",
        **{k: str(v) for k, v in params.items()},
    }
    r = httpx.post(ENDPOINT, data=data, timeout=60.0)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and j.get("exception"):
        raise RuntimeError(f"Moodle error [{j.get('errorcode')}]: {j.get('message')}")
    return j


@mcp.tool()
async def verify(ctx: Context = None) -> dict:
    """接続確認。サイト名・ユーザ名・利用可能関数数・書込設定を返す。"""
    si = _call("core_webservice_get_site_info", {})
    await _safe_log(ctx, "info", f"verify ok: site={si.get('sitename')} write={ALLOW_WRITE}")
    return {
        "site": si.get("sitename"),
        "user": si.get("fullname"),
        "functions": len(si.get("functions", [])),
        "allow_write": ALLOW_WRITE,
        "write_courses": sorted(WRITE_COURSES),
    }


@mcp.tool()
async def list_assignments(courseid: int, ctx: Context = None) -> list[dict]:
    """コース内の課題(assign)一覧。返り値: [{id(=assignmentid), cmid, name, duedate, grademax}]。

    注意: 採点/取得で使うのは id（assignment instance id）。cmid（コースモジュールID）とは別物。
    """
    j = _call("mod_assign_get_assignments", {"courseids[0]": courseid})
    out: list[dict] = []
    for c in j.get("courses", []):
        for a in c.get("assignments", []):
            out.append(
                {
                    "id": a["id"],
                    "cmid": a.get("cmid"),
                    "name": a["name"],
                    "duedate": a.get("duedate"),
                    "grademax": a.get("grade"),
                }
            )
    await _safe_log(ctx, "info", f"list_assignments course={courseid} n={len(out)}")
    return out


@mcp.tool()
async def find_courses(field: str = "", value: str = "", name_like: str = "", ctx: Context = None) -> list[dict]:
    """閲覧可能なコースを返す（授業内容収集の起点＝courseid を引く読み取りツール）。

    field/value 省略時は権限内の全コース。field は core_course_get_courses_by_field 準拠
    （id / ids / shortname / idnumber / category）。
    name_like 指定時は fullname/shortname を**空白区切りの全語AND部分一致**で絞り込む
    （全コースが数百件あるときに有用。例: name_like="2024 spring"）。
    返り値: [{id(=courseid), shortname, fullname, categoryname, visible}]
    """
    params: dict[str, Any] = {}
    if field:
        params["field"] = field
        params["value"] = value
    j = _call("core_course_get_courses_by_field", params)
    out = [
        {
            "id": c.get("id"),
            "shortname": c.get("shortname"),
            "fullname": c.get("fullname"),
            "categoryname": c.get("categoryname"),
            "visible": c.get("visible"),
        }
        for c in j.get("courses", [])
    ]
    if name_like:
        terms = name_like.lower().split()
        out = [
            c for c in out
            if all(t in f"{c.get('fullname', '') or ''} {c.get('shortname', '') or ''}".lower() for t in terms)
        ]
    await _safe_log(ctx, "info", f"find_courses field={field or '*'} name_like={name_like or '-'} n={len(out)}")
    return out


@mcp.tool()
async def course_contents(
    courseid: int, include_summaries: bool = True, ctx: Context = None
) -> list[dict]:
    """コースの週次トピック（セクション）と各回の教材モジュールを返す（論文Ⅳ「各回で何をしたか」の一次証拠）。

    core_course_get_contents を要約整形。録画本体やファイルバイナリは取得せず、名称とリンクのみ返す。
    返り値: [{section, name, summary, modules:[{modname, name, url, contents:[{filename, fileurl}]}]}]
      - modname 例: resource(ファイル) / url(リンク) / label(掲示) / page / assign(課題) / quiz(小テスト) / forum 等
    """
    sections = _call("core_course_get_contents", {"courseid": courseid})
    out: list[dict] = []
    for sec in sections:
        mods = []
        for m in sec.get("modules", []):
            files = []
            for c in m.get("contents", []) or []:
                if c.get("type") == "file":
                    files.append({"filename": c.get("filename"), "fileurl": c.get("fileurl")})
            mods.append(
                {
                    "modname": m.get("modname"),
                    "name": m.get("name"),
                    "url": m.get("url"),
                    "contents": files,
                }
            )
        out.append(
            {
                "section": sec.get("section"),
                "name": sec.get("name"),
                "summary": _strip_html(sec.get("summary", "")) if include_summaries else "",
                "modules": mods,
            }
        )
    nmods = sum(len(s["modules"]) for s in out)
    await _safe_log(ctx, "info", f"course_contents course={courseid} sections={len(out)} modules={nmods}")
    return out


@mcp.tool()
async def list_pending(assignid: int, ctx: Context = None) -> list[dict]:
    """採点待ち（提出済み かつ 未採点）の提出を返す。ポーリングの起点。

    「未採点」= まだ評点が付いていない（自分が入れたドラフトも評点ありとして除外＝二重採点防止）。
    返り値: [{userid, timemodified, submissionid}]
    """
    subs = _call(
        "mod_assign_get_submissions",
        {"assignmentids[0]": assignid, "status": "submitted"},
    ).get("assignments", [])
    submitted: list[dict] = []
    for a in subs:
        for s in a.get("submissions", []):
            if s.get("status") == "submitted":
                submitted.append(
                    {
                        "userid": s["userid"],
                        "timemodified": s.get("timemodified"),
                        "submissionid": s.get("id"),
                    }
                )
    grades = _call("mod_assign_get_grades", {"assignmentids[0]": assignid}).get(
        "assignments", []
    )
    graded_users = {
        g["userid"] for a in grades for g in a.get("grades", []) if g.get("grade") not in (None, "", "-1.00000")
    }
    pending = [s for s in submitted if s["userid"] not in graded_users]
    await _safe_log(ctx, "info", f"list_pending assign={assignid} pending={len(pending)}")
    return pending


def _submission_parts(assignid: int, userid: int) -> dict:
    """提出の生データを整形して返す（onlinetext と添付ファイルのメタデータ）。

    添付は filename だけでなく fileurl / mimetype / filesize も保持する。fileurl が無いと
    本文を取りに行けないため（以前はここで捨てていた）。
    """
    st = _call(
        "mod_assign_get_submission_status", {"assignid": assignid, "userid": userid}
    )
    last = st.get("lastattempt", {}) or {}
    submission = last.get("submission", {}) or last.get("teamsubmission", {}) or {}
    onlinetext = ""
    files: list[dict] = []
    for p in submission.get("plugins", []):
        if p.get("type") == "onlinetext":
            for ef in p.get("editorfields", []):
                onlinetext += ef.get("text", "") or ""
        if p.get("type") == "file":
            for fa in p.get("fileareas", []):
                for f in fa.get("files", []):
                    files.append(
                        {
                            "filename": f.get("filename", ""),
                            "fileurl": f.get("fileurl", ""),
                            "mimetype": f.get("mimetype", ""),
                            "filesize": f.get("filesize", 0) or 0,
                        }
                    )
    return {
        "status": submission.get("status"),
        "gradingstatus": last.get("gradingstatus"),
        "onlinetext": onlinetext,
        "files": files,
    }


def _pick_files(files: list[dict], filename: str) -> list[dict]:
    if not filename:
        return files
    hit = [f for f in files if f["filename"] == filename]
    if not hit:
        names = ", ".join(f["filename"] for f in files) or "(添付なし)"
        raise RuntimeError(f"'{filename}' は提出物にありません。添付: {names}")
    return hit


def _download(f: dict) -> bytes:
    """添付ファイルの実体を取得する。URL にトークンを付けるため、URL は絶対にログへ出さない。"""
    url = f.get("fileurl") or ""
    if not url:
        raise RuntimeError(f"{f.get('filename')}: fileurl がありません")
    size = int(f.get("filesize") or 0)
    if size and size > MAX_FILE_MB * 1024 * 1024:
        raise RuntimeError(
            f"{f['filename']}: {size / 1048576:.1f}MB は上限 {MAX_FILE_MB}MB を超えます"
            "（MOODLE_MAX_FILE_MB で変更可）"
        )
    sep = "&" if "?" in url else "?"
    r = httpx.get(f"{url}{sep}token={MOODLE_TOKEN}", timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    body = r.content
    # Moodle は認証失敗も HTTP 200 + JSON で返すことがある
    if body[:1] == b"{" and b"exception" in body[:400]:
        raise RuntimeError(f"{f['filename']}: ダウンロードに失敗しました（トークン権限を確認してください）")
    if len(body) > MAX_FILE_MB * 1024 * 1024:
        raise RuntimeError(f"{f['filename']}: 取得サイズが上限 {MAX_FILE_MB}MB を超えました")
    return body


@mcp.tool()
async def get_submission(assignid: int, userid: int, ctx: Context = None) -> dict:
    """指定ユーザの提出内容を返す。返り値: {status, gradingstatus, onlinetext, files:[...]}。

    files は [{filename, fileurl, mimetype, filesize}]。**本文は含まれない**（一覧のみ）。
    添付の中身は read_submission_file（本文）／ read_submission_images（図・画像）で取得する。
    ほとんどの課題は本文が添付ファイル側にあるため、onlinetext だけで採点しないこと。
    """
    parts = _submission_parts(assignid, userid)
    await _safe_log(
        ctx, "info",
        f"get_submission assign={assignid} user={userid} files={len(parts['files'])}",
    )
    return parts


@mcp.tool()
async def read_submission_file(
    assignid: int, userid: int, filename: str = "", max_chars: int = 0,
    ctx: Context = None,
) -> dict:
    """添付ファイルの本文をテキストで返す（docx / pptx / xlsx / pdf / テキスト）。

    filename 省略時は添付すべてを処理する。max_chars 省略時は MOODLE_MAX_TEXT_CHARS。
    返り値: {files:[{filename, kind, chars, truncated, note, text}]}

    1ファイルの失敗で全体を止めない（note にエラーを入れて続行する）。
    text が空で note が「画像として貼り付けられている可能性」を示す場合は
    read_submission_images を使うこと。
    """
    parts = _submission_parts(assignid, userid)
    targets = _pick_files(parts["files"], filename)
    limit = max_chars if max_chars > 0 else MAX_TEXT_CHARS
    out: list[dict] = []
    for f in targets:
        try:
            data = _download(f)
        except Exception as e:
            out.append({"filename": f["filename"], "kind": "unknown", "text": "",
                        "chars": 0, "truncated": False, "note": str(e)})
            continue
        res = extract.extract_text(data, f["filename"], f.get("mimetype", ""), limit)
        res["filename"] = f["filename"]
        out.append(res)
    await _safe_log(
        ctx, "info",
        f"read_submission_file assign={assignid} user={userid} files={len(out)} "
        f"chars={sum(r['chars'] for r in out)}",
    )
    return {"files": out}


@mcp.tool()
async def read_submission_images(
    assignid: int, userid: int, filename: str = "", limit: int = 0,
    ctx: Context = None,
) -> list:
    """添付に含まれる画像を画像として返す（モデルが見て書き起こす・内容を読み取るため）。

    pptx はスライド番号つき（slide3_img1 など）、pdf はページ番号つきで返すので、
    「どのページの図か」を採点コメントに書ける。docx / xlsx / 画像ファイルにも対応。

    テキスト抽出では取れない情報（スクリーンショット、ワイヤーフレーム、グラフ、
    手書き図、画像化された表）を読むために使う。**呼び出し側は、返ってきた画像を見て
    文字を書き起こし、図の内容を説明すること。** サーバ側では OCR しない。

    filename 省略時は添付すべてが対象。limit 省略時は MOODLE_MAX_IMAGES。
    長辺 MOODLE_IMAGE_MAX_PX を超える画像は縮小する（既定 1568px）。
    """
    parts = _submission_parts(assignid, userid)
    targets = _pick_files(parts["files"], filename)
    cap = limit if limit > 0 else MAX_IMAGES
    blocks: list = []
    notes: list[str] = []
    remaining = cap
    for f in targets:
        if remaining <= 0:
            notes.append(f"{f['filename']}: 上限{cap}枚に達したため未処理")
            continue
        try:
            data = _download(f)
        except Exception as e:
            notes.append(f"{f['filename']}: {e}")
            continue
        images, note = extract.extract_images(
            data, f["filename"], f.get("mimetype", ""), remaining, IMAGE_MAX_PX
        )
        if note:
            notes.append(f"{f['filename']}: {note}")
        for img in images:
            blocks.append(f"[{f['filename']} / {img['name']}]")
            blocks.append(Image(data=img["data"], format=img["format"]))
            remaining -= 1
    header = f"画像 {len(blocks) // 2} 枚。文字を書き起こし、図の内容を説明してください。"
    if notes:
        header += " 注記: " + " / ".join(notes)
    await _safe_log(
        ctx, "info",
        f"read_submission_images assign={assignid} user={userid} images={len(blocks) // 2}",
    )
    return [header, *blocks]


@mcp.tool()
async def save_grade_draft(
    assignid: int, userid: int, course_id: int, grade: float, feedback_html: str,
    ctx: Context = None,
) -> dict:
    """採点を【ドラフト】で保存する（唯一の書き込みツール）。

    安全規則（強制・上書き不可）:
      - workflowstate=readyforreview（採点完了＝未公開）。released には決してしない。
      - 学生通知なし（ドラフト状態のため Moodle は通知しない）。
      - AI開示フッターが無ければ付与。
      - course_id が許可コース(allowlist)に無ければ拒否。MOODLE_ALLOW_WRITE=1 必須。
    すべての試行・成否は監査ログ(JSONL)に記録される。
    """
    if not ALLOW_WRITE:
        _audit("grade_denied", reason="write_disabled", course=course_id, assign=assignid, userid=userid)
        await _safe_log(ctx, "warning", f"save_grade_draft denied (write disabled) user={userid}")
        raise RuntimeError("write disabled: .env で MOODLE_ALLOW_WRITE=1 にしてください")
    if str(course_id) not in WRITE_COURSES:
        _audit("grade_denied", reason="course_not_allowed", course=course_id, assign=assignid, userid=userid)
        await _safe_log(ctx, "warning", f"save_grade_draft denied (course {course_id} not allowed) user={userid}")
        raise RuntimeError(
            f"course {course_id} は書込許可リスト {sorted(WRITE_COURSES)} に含まれません（本番保護）"
        )
    fb = feedback_html or ""
    footer_added = False
    if _FOOTER_MARK not in fb:
        fb += _FALLBACK_FOOTER
        footer_added = True
    _audit("grade_attempt", course=course_id, assign=assignid, userid=userid, grade=grade)
    _call(
        "mod_assign_save_grade",
        {
            "assignmentid": assignid,
            "userid": userid,
            "grade": grade,
            "attemptnumber": -1,
            "addattempt": 0,
            "workflowstate": "readyforreview",  # ★未公開ドラフト。releasedにしない
            "applytoall": 1,
            "plugindata[assignfeedbackcomments_editor][text]": fb,
            "plugindata[assignfeedbackcomments_editor][format]": 1,
        },
    )
    _audit("grade_saved", course=course_id, assign=assignid, userid=userid, grade=grade,
           workflowstate="readyforreview", released=False, footer_added=footer_added)
    await _safe_log(ctx, "info", f"save_grade_draft OK user={userid} grade={grade} (readyforreview, unreleased)")
    return {
        "ok": True,
        "userid": userid,
        "grade": grade,
        "workflowstate": "readyforreview",
        "released": False,
        "footer_added": footer_added,
    }


def main():
    """MCP_TRANSPORT=stdio(既定) | streamable-http | sse で待受方式を選ぶ。"""
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    authed = bool(os.environ.get("MCP_BEARER_TOKENS", "").strip())
    log.info("moodle-grading MCP start: transport=%s auth=%s allow_write=%s courses=%s",
             transport, authed, ALLOW_WRITE, sorted(WRITE_COURSES))
    if transport in ("streamable-http", "http", "streamable_http"):
        if not authed:
            log.warning("HTTP transport without MCP_BEARER_TOKENS — 成績書込APIを無認証公開しないこと")
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
