# Redash MCP サーバー

Claude Code から Redash を直接叩くための MCP サーバー。
本番レプリカ(read-only)に対するアドホック SQL 実行・保存クエリ実行・各種参照を提供します。
「SQL を手で Redash にコピペ」する運用を、Claude Code 内で完結させます。

設置場所は法人中立な `~/Developments/tools/redash-mcp/`（特定法人のリポジトリに依存させない）。
**法人ごとに 1 つの Redash** を、**法人ディレクトリで起動したときの既定 `redash`** として割り当てる運用を想定。

## 構成

- `redash_mcp.py` — MCP サーバー本体 (Python / `uv run` で自己完結。PEP 723 で依存を内蔵)
- `.env.example` — 接続情報テンプレート
- `profiles/` — 法人ごとの接続情報（`<法人>.env`。gitignore 済み。雛形は `prod.env.example`）
- `smoke_test.sh` — 起動・ツール登録・Redash 疎通をまとめて確認するスモークテスト

## 提供ツール

| ツール | 用途 |
|---|---|
| `list_data_sources` | データソース一覧 (`run_query` の `data_source_id` を調べる) |
| `run_query` | **アドホック SQL を実行**して結果を取得 (read-only ガードあり) |
| `list_queries` | 保存クエリの検索/一覧 |
| `get_query` | 保存クエリの SQL 本文・パラメータ定義を取得 |
| `update_query` | 保存クエリの SQL 本文を書き換える (read-only ガードと version 照合あり) |
| `run_saved_query` | 保存クエリを ID 指定で実行 |
| `get_cached_result` | 保存クエリの最新キャッシュ結果を再実行せず取得 |
| `list_dashboards` | ダッシュボード検索/一覧 |
| `get_dashboard` | ダッシュボードの構成 (含まれるクエリ) を取得 |

## セットアップ（法人を1つ追加する手順）

新しい法人を足すたびに、次の2ステップを繰り返すだけ。

### 1. その法人の接続情報を作成

```bash
cd ~/Developments/tools/redash-mcp/profiles
cp prod.env.example <法人>.env       # 例: lx.env
# <法人>.env を編集して REDASH_URL と REDASH_API_KEY を記入
```

API キーは Redash 右上アバター → **Edit Profile → API Key** で取得できる
**ユーザー API キー**を使ってください (クエリ個別のキーではありません)。
`profiles/*.env` は gitignore 済みでコミットされません。

### 2. その法人ディレクトリで local スコープ登録

**その法人の作業ディレクトリで `claude` を起動し**、以下を実行（または `! claude mcp add ...`）。

```bash
claude mcp add redash --scope local \
  -e REDASH_ENV_FILE=$HOME/Developments/tools/redash-mcp/profiles/<法人>.env \
  -- uv run $HOME/Developments/tools/redash-mcp/redash_mcp.py
```

これで「その法人ディレクトリで起動したときの既定 `redash`」がその法人の Redash になります。
別の法人ディレクトリでは、その法人で同じ登録をすればそちらの Redash が既定に。

### 3. 確認

```bash
claude mcp list      # redash が ✔ Connected
claude mcp get redash # Scope: Local config / REDASH_ENV_FILE を確認
```

Claude Code を再起動すると `mcp__redash__*` ツールが使えます。

## 仕組み（接続情報の優先順）

読み込み優先順は **`-e` 環境変数 > `REDASH_ENV_FILE` のファイル > 隣の `.env`**。
本運用では各法人の local スコープ登録が `-e REDASH_ENV_FILE=...` でその法人のプロファイルを指すため、
**起動した法人ディレクトリ = 既定の Redash** が自動で決まります。

> スコープ優先順は **local > project > user**。各法人ディレクトリの local 登録が、
> （もし将来 user スコープに既定を置いても）それを上書きします。
> ここでの「ディレクトリ」は **`claude` を起動した作業ディレクトリ**（セッション中の `cd` では切り替わりません）。

### 任意: 1法人内に複数環境(prod/staging)がある場合

別名で追加登録すれば、法人既定の `redash` と併用してツール名で呼び分けられます。

```bash
claude mcp add redash-staging --scope local \
  -e REDASH_ENV_FILE=$HOME/Developments/tools/redash-mcp/profiles/<法人>-staging.env \
  -- uv run $HOME/Developments/tools/redash-mcp/redash_mcp.py
# → mcp__redash__*（既定=本番） と mcp__redash-staging__* を使い分け
```

## 安全設計

- **read-only ガード**: `run_query` は既定で `SELECT / WITH / EXPLAIN / SHOW` 始まりのみ許可し、
  `INSERT/UPDATE/DELETE/DROP/...` 等を検出すると拒否します (文字列関数の `REPLACE()` は対象外)。
  テーブルを作ってしまう `SELECT ... INTO` も拒否対象です
  (一次防御はあくまで Redash データソースが read-only レプリカであること)。
  解除する場合のみ `REDASH_ALLOW_WRITE=1`。
- **保存クエリの更新**: `update_query` は SQL 本文だけを書き換え、名前・パラメータ定義・可視化は変えません。
  保存する SQL にも `run_query` と同じ read-only ガードをかけます。
  `get_query` で取得した `version` を必須とし、一致しなければ Redash が 409 を返します。
  ただし Redash は本文を更新しても `version` を増やさないため、他者の編集はこれでは検出できません
  (2026-09 に実機で確認)。上書きする前に `get_query` で本文を取り直して確かめてください。
  書き換えられる範囲は、API キーの持ち主の Redash 上の権限に従います。
- **行数キャップ**: 結果は既定 1000 行で切り、超過時は `truncated: true` と `note` で通知します
  (`max_rows` 引数 / `REDASH_MAX_ROWS` で調整)。
- `profiles/*.env` / `.env` は `.gitignore` 済み。API キーはコミットされません。

## 依存 SDK のバージョン

`mcp` SDK は `>=2.2,<3` に固定しています。以前は上限を切っておらず、
v2 の公開に追随して v1 の `FastMCP` が消え、サーバーが起動不能になりました。

v2 で変わった点のうち、このサーバーに効いてくるのは次の 2 つです。

- `FastMCP` は `MCPServer` に改名 (`mcp.server.mcpserver`)
- ツールが投げた例外は `ToolError` の派生でないと本文が伏せられ、
  モデルには `Error executing tool <名前>` としか見えない。
  そのため `RedashError` は `ToolError` を継承し、通信エラーや
  JSON でない応答も `RedashError` に翻訳しています

## 動作確認

スモークテスト (起動・ツール登録・エラー本文の透過・Redash 疎通をまとめて確認):

```bash
cd ~/Developments/tools/redash-mcp
REDASH_ENV_FILE=$HOME/Developments/tools/redash-mcp/profiles/<法人>.env ./smoke_test.sh
```

サーバー単体の起動確認:

```bash
cd ~/Developments/tools/redash-mcp
REDASH_ENV_FILE=$HOME/Developments/tools/redash-mcp/profiles/<法人>.env uv run redash_mcp.py
# 依存を解決して stdio で待受 (Ctrl-C で終了)
```
