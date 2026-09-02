# moodle-grading-mcp

An MCP server (stdio) that exposes **Moodle Web Services** as grading I/O tools. The LLM client decides the grades; this server only **fetches submissions and writes grades as unreleased drafts**, enforcing safety rules server-side. Read-only course/content tools are included for syllabus/assignment discovery.

## Safety (enforced server-side)
- Grades are written as `workflowstate=readyforreview` (graded but **UNRELEASED**). This server **never releases**.
- No student notification (draft state).
- An AI-assistance disclosure footer is appended if missing (override via `MOODLE_AI_FOOTER_FILE`).
- Writes go **only to allowlisted course IDs** (`MOODLE_WRITE_COURSE_ALLOWLIST`); empty = no write target. `MOODLE_ALLOW_WRITE=1` is also required.
- Every write attempt/denial/success is appended to an audit log (JSONL).

## Tools (9)
| Tool | Purpose |
|---|---|
| `verify` | Connection check (site, available functions, allow_write, allowlist) |
| `find_courses` | List visible courses (`name_like` AND-substring filter) |
| `course_contents` | Sections & module list of a course |
| `list_assignments` | Assignments in a course (id = assignment instance id) |
| `list_pending` | Submitted-but-ungraded submissions (polling start point) |
| `get_submission` | A user's submission (onlinetext, attachment metadata) |
| `read_submission_file` | Extract text from attachments (docx/pptx/xlsx/pdf/text) |
| `read_submission_images` | Return embedded images so the model can read figures |
| `save_grade_draft` | Write a grade as an **unreleased draft** (the only writer) |

## Setup

### 1. Moodle token
In Moodle: enable Web Services, then issue a token for a user with grading permission (Site administration → Server → Web services → Manage tokens). The token needs the functions used here: `core_webservice_get_site_info`, `core_course_get_courses_by_field`, `core_course_get_contents`, `mod_assign_get_assignments`, `mod_assign_get_submissions`, `mod_assign_get_grades`, `mod_assign_get_submission_status`, `mod_assign_save_grade`.

### 2. `.env`
Place `.env` in the **parent directory of `server.py`** (the code loads `../.env`), or export the variables in your shell.
```sh
MOODLE_URL="https://moodle.example.com"
MOODLE_TOKEN="<your token>"
MOODLE_ALLOW_WRITE=0                 # 1 to enable writing grades
MOODLE_WRITE_COURSE_ALLOWLIST=       # comma-separated course IDs allowed to write (empty = none)
MOODLE_AI_FOOTER_FILE=               # optional: HTML file to replace the disclosure footer
MOODLE_MAX_FILE_MB=150               # max size per attachment to download
MOODLE_MAX_TEXT_CHARS=800000         # max extracted characters
MOODLE_MAX_IMAGES=20                 # max images returned per call
MOODLE_IMAGE_MAX_PX=1568             # images longer than this are downscaled
```

### 3. venv + deps & run
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py        # stdio
```

### 4. Register in your MCP client (e.g. `.mcp.json`)
```json
{
  "mcpServers": {
    "moodle-grading": {
      "command": "/ABSOLUTE/PATH/TO/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/server.py"]
    }
  }
}
```

## How grading works
This server has **no grading logic**. You drive it from an LLM with your own grading prompt (see `prompts/example_grade_prompt.md`):
`list_pending` → `get_submission` → *(the LLM scores per your rubric)* → `save_grade_draft`. A human then reviews and releases in Moodle.

## Remote / Auth
For HTTP exposure: `MCP_TRANSPORT=streamable-http` + `MCP_BEARER_TOKENS=...` (+ `MCP_HOST`/`MCP_PORT`). For claude.ai-style connectors (OAuth 2.1) see `CONNECTOR-ROADMAP.md`.

## Secrets (never commit)
`.env` and `*.log` (incl. `audit.log`) are git-ignored. Never commit your Moodle token.

## Operations manual
A step-by-step Japanese manual for non-engineer operators (setup, day-to-day use, what the server can and cannot do, safety rules, troubleshooting): [`docs/OPERATIONS-ja.md`](docs/OPERATIONS-ja.md).

## License
MIT License (see `LICENSE`).

---

# moodle-grading-mcp（日本語）

**Moodle Web Services** を採点の入出力ツールとして公開する MCP サーバ（stdio）。採点の判断は LLM クライアントが行い、本サーバは**提出の取得と採点の「未公開ドラフト」書き込み**に徹し、安全規則をサーバ側で強制します。コース構成・課題の読み取りツールも同梱。

## 安全規則（サーバ側で強制）
- 採点は `workflowstate=readyforreview`（採点済みだが**未公開**）で書き込み。本サーバは**絶対に公開（released）にしない**。
- 学生通知なし（ドラフト状態）。
- AI開示フッターが無ければ付与（`MOODLE_AI_FOOTER_FILE` で差し替え可）。
- 書き込みは**許可コースID（`MOODLE_WRITE_COURSE_ALLOWLIST`）のみ**。空＝書込先なし。`MOODLE_ALLOW_WRITE=1` も必須。
- 書き込みの試行/拒否/成功は監査ログ（JSONL）に追記。

## ツール（9）
| ツール | 用途 |
|---|---|
| `verify` | 接続確認（サイト・利用可能関数・書込設定） |
| `find_courses` | 閲覧可能コース一覧（`name_like` 全語AND部分一致フィルタ） |
| `course_contents` | コースのセクション・教材一覧 |
| `list_assignments` | コース内の課題（id=assignment instance id） |
| `list_pending` | 提出済み×未採点（ポーリングの起点） |
| `get_submission` | あるユーザの提出（onlinetext・添付のメタデータ） |
| `read_submission_file` | 添付の本文をテキスト抽出（docx/pptx/xlsx/pdf/テキスト） |
| `read_submission_images` | 添付に含まれる画像を画像として返す（図の読み取り用） |
| `save_grade_draft` | 採点を**未公開ドラフト**で書き込み（唯一の書き込みツール） |

## セットアップ

### 1. Moodle トークン
Moodle で Web Services を有効化し、採点権限を持つユーザのトークンを発行（サイト管理 → サーバー → Web サービス → トークンの管理）。上記ツールが使う WS 関数を許可してください。

### 2. `.env`
`.env` は **`server.py` の 1 つ上の階層**に置きます（コードは `../.env` を読み込みます）。シェルで `export` しても構いません。
```sh
MOODLE_URL="https://moodle.example.com"
MOODLE_TOKEN="<トークン>"
MOODLE_ALLOW_WRITE=0                 # 採点書込を有効化するなら 1
MOODLE_WRITE_COURSE_ALLOWLIST=       # 書込を許すコースID（カンマ区切り・空=なし）
MOODLE_AI_FOOTER_FILE=               # 任意: 開示フッターを差し替えるHTMLファイル
MOODLE_MAX_FILE_MB=150               # 添付1件の取得上限(MB)
MOODLE_MAX_TEXT_CHARS=800000         # 抽出テキストの上限(文字)
MOODLE_MAX_IMAGES=20                 # 1回に返す画像の上限(枚)
MOODLE_IMAGE_MAX_PX=1568             # 画像の長辺上限(px)
```

### 3. venv + 依存 & 起動
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py        # stdio
```

### 4. MCP クライアントに登録（例: `.mcp.json`）
`/ABSOLUTE/PATH/TO/` を自分のパスに置換して登録し、クライアントを再起動。

## 採点の流れ
本サーバは**採点ロジックを持ちません**。LLM に自分の採点プロンプト（`prompts/example_grade_prompt.md` 参照）を渡して駆動します：
`list_pending` → `get_submission` →（LLM がルーブリックで採点）→ `save_grade_draft`。その後、人が Moodle で確認・公開します。

## リモート / 認証
HTTP 公開: `MCP_TRANSPORT=streamable-http` + `MCP_BEARER_TOKENS=...`。OAuth コネクタ化は `CONNECTOR-ROADMAP.md` 参照。

## 秘密情報（コミットしない）
`.env` と `*.log`（`audit.log` 含む）は `.gitignore` 済み。トークンは絶対にコミットしないこと。

## 運用マニュアル
エンジニア以外の運用担当者向けの手順書（導入・日常の使い方・できること/できないこと・安全規則・トラブルシューティング）: [`docs/OPERATIONS-ja.md`](docs/OPERATIONS-ja.md)

## ライセンス
MIT License（`LICENSE` 参照）。
