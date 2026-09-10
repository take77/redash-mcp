#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=2.2,<3",  # メジャーアップで API が変わるため上限を固定
#   "httpx>=0.27",
# ]
# ///
"""
Redash MCP server (read-only oriented).

Claude Code から Redash を直接叩くための MCP サーバー。
本番レプリカ(read-only)に対するアドホック SQL 実行 / 保存クエリ実行 / 各種参照を提供する。
書き込みは保存クエリの SQL 本文の更新 (update_query) だけで、DB には書き込まない。

接続情報は環境変数、または env ファイルから読む。読み込み元は次の優先順:
    1. MCP 設定の -e で渡された環境変数 (最優先)
    2. REDASH_ENV_FILE で指定した env ファイル (複数環境の切替に使う)
    3. 本ファイルと同じディレクトリの .env (既定)

    REDASH_URL          例: https://redash.example.com   (末尾スラッシュ不要)
    REDASH_API_KEY      Redash のユーザー API キー (個人キー推奨)
    REDASH_ENV_FILE     読み込む env ファイルのパス (任意。環境ごとに切替)
    REDASH_TIMEOUT      クエリ完了待ちの最大秒数 (任意, 既定 120)
    REDASH_MAX_ROWS     1 回の結果で返す既定の最大行数 (任意, 既定 1000)
    REDASH_ALLOW_WRITE  "1"/"true" のとき SELECT 以外の SQL も許可 (既定 無効)
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


# --------------------------------------------------------------------------- #
# 設定読み込み (.env を環境変数より弱い優先度でロード)
# --------------------------------------------------------------------------- #
def _load_dotenv() -> None:
    # REDASH_ENV_FILE が指定されていればそのファイルを、なければ隣の .env を読む。
    # 既存の環境変数 (MCP 設定の -e 等) は setdefault で上書きしない。
    explicit = os.environ.get("REDASH_ENV_FILE")
    env_path = (
        Path(explicit).expanduser()
        if explicit
        else Path(__file__).resolve().parent / ".env"
    )
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 既存の環境変数を上書きしない (実環境の値を優先)
        os.environ.setdefault(key, value)


_load_dotenv()

REDASH_URL = os.environ.get("REDASH_URL", "").rstrip("/")
REDASH_API_KEY = os.environ.get("REDASH_API_KEY", "")
REDASH_TIMEOUT = float(os.environ.get("REDASH_TIMEOUT", "120"))
REDASH_MAX_ROWS = int(os.environ.get("REDASH_MAX_ROWS", "1000"))
REDASH_ALLOW_WRITE = os.environ.get("REDASH_ALLOW_WRITE", "").lower() in ("1", "true", "yes")

# Redash の job ステータス
_JOB_PENDING, _JOB_STARTED, _JOB_SUCCESS, _JOB_FAILURE, _JOB_CANCELLED = 1, 2, 3, 4, 5

mcp = MCPServer("redash", version="0.1.0")


# --------------------------------------------------------------------------- #
# HTTP ヘルパ
# --------------------------------------------------------------------------- #
class RedashError(ToolError):
    """Redash 由来 / 設定不備のエラー。ツールから読める形で投げ直す。

    mcp 2.x はツールが投げた例外のうち ToolError の派生だけを本文ごと
    クライアントに渡し、それ以外は "Error executing tool <名前>" に
    差し替えて本文を伏せる。原因をモデルに読ませたいので継承する。
    """


def _require_config() -> None:
    missing = [
        name
        for name, val in (("REDASH_URL", REDASH_URL), ("REDASH_API_KEY", REDASH_API_KEY))
        if not val
    ]
    if missing:
        raise RedashError(
            f"環境変数が未設定です: {', '.join(missing)}。"
            " 本ファイルと同じディレクトリの .env か MCP 設定の env で指定してください。"
        )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=REDASH_URL,
        headers={
            "Authorization": f"Key {REDASH_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(30.0, read=60.0),
    )


# 通信と JSON 解釈の失敗は RedashError に翻訳する。mcp 2.x は ToolError 派生以外の
# 例外の本文を伏せるため、翻訳しないと接続不能や設定ミスの原因がモデルに届かない。
async def _get(client: httpx.AsyncClient, path: str, **kwargs: Any) -> Any:
    try:
        resp = await client.get(path, **kwargs)
    except httpx.HTTPError as exc:
        raise RedashError(f"Redash への接続に失敗しました: {exc}") from exc
    _raise_for_status(resp)
    return _parse_json(resp)


async def _post(client: httpx.AsyncClient, path: str, json: dict) -> Any:
    try:
        resp = await client.post(path, json=json)
    except httpx.HTTPError as exc:
        raise RedashError(f"Redash への接続に失敗しました: {exc}") from exc
    _raise_for_status(resp)
    return _parse_json(resp)


def _parse_json(resp: httpx.Response) -> Any:
    # SSO のログイン画面が返る等、JSON でない応答を読める失敗にする。
    try:
        return resp.json()
    except ValueError as exc:
        raise RedashError(
            f"Redash の応答が JSON ではありません: {resp.text[:200]}"
        ) from exc


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    detail = ""
    try:
        body = resp.json()
        detail = body.get("message") or body.get("error") or str(body)
    except Exception:
        detail = resp.text[:500]
    raise RedashError(f"Redash API {resp.status_code} {resp.request.url}: {detail}")


# --------------------------------------------------------------------------- #
# SQL 安全チェック (多層防御。一次防御はあくまで read-only データソース)
# --------------------------------------------------------------------------- #
# into を含むのは SELECT ... INTO 対策。PostgreSQL では CREATE TABLE AS と等価に
# テーブルを作るため、SELECT 始まりでも通してはいけない。
# (INSERT INTO は insert 側で既に弾かれる)
# replace は入れない。REPLACE 文 (MySQL の REPLACE INTO) は into 側で、
# CREATE OR REPLACE は create 側で弾かれる。入れると文字列関数 REPLACE() を含む
# 読み取り SQL まで拒否してしまう。
_FORBIDDEN = re.compile(
    r"\b(insert|into|update|delete|drop|alter|truncate|create|grant|revoke|"
    r"merge|call|do|copy|vacuum|analyze|reindex|cluster|comment\s+on)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(with|select|explain|show)\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)  # ブロックコメント
    sql = re.sub(r"--[^\n]*", " ", sql)  # 行コメント
    return sql


def _assert_read_only(sql: str) -> None:
    if REDASH_ALLOW_WRITE:
        return
    cleaned = _strip_sql_comments(sql)
    if not _ALLOWED_START.match(cleaned):
        raise RedashError(
            "SELECT / WITH / EXPLAIN / SHOW で始まる読み取り専用 SQL のみ許可しています"
            " (REDASH_ALLOW_WRITE=1 で解除可)。"
        )
    forbidden = _FORBIDDEN.search(cleaned)
    if forbidden:
        raise RedashError(
            f"書き込み・DDL 系キーワード ({forbidden.group(0)}) を検出したため実行を拒否しました"
            " (REDASH_ALLOW_WRITE=1 で解除可)。"
        )


# --------------------------------------------------------------------------- #
# クエリ実行 (ジョブのポーリング + 結果整形)
# --------------------------------------------------------------------------- #
async def _wait_for_result(client: httpx.AsyncClient, body: dict) -> int:
    """query_results / queries/{id}/results のレスポンスから結果 ID を得る。"""
    if "query_result" in body:
        return int(body["query_result"]["id"])
    if "job" not in body:
        raise RedashError(f"想定外のレスポンス形式: {str(body)[:300]}")

    job = body["job"]
    deadline = asyncio.get_event_loop().time() + REDASH_TIMEOUT
    interval = 0.5
    while True:
        status = job.get("status")
        if status == _JOB_SUCCESS:
            qrid = job.get("query_result_id")
            if not qrid:
                raise RedashError("ジョブは成功しましたが query_result_id がありません。")
            return int(qrid)
        if status == _JOB_FAILURE:
            raise RedashError(f"クエリ実行に失敗: {job.get('error') or '不明なエラー'}")
        if status == _JOB_CANCELLED:
            raise RedashError("クエリ実行がキャンセルされました。")
        if asyncio.get_event_loop().time() > deadline:
            raise RedashError(
                f"{REDASH_TIMEOUT:.0f} 秒以内に完了しませんでした (REDASH_TIMEOUT で延長可)。"
            )
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 3.0)
        data = await _get(client, f"/api/jobs/{job['id']}")
        job = data.get("job", data)


async def _fetch_result(client: httpx.AsyncClient, result_id: int, max_rows: int) -> dict:
    data = await _get(client, f"/api/query_results/{result_id}.json")
    return _format_result(data.get("query_result", data), max_rows)


def _format_result(query_result: dict, max_rows: int) -> dict:
    qr_data = query_result.get("data", {})
    columns = [c.get("name") for c in qr_data.get("columns", [])]
    rows = qr_data.get("rows", [])
    total = len(rows)
    truncated = total > max_rows
    return {
        "columns": columns,
        "rows": rows[:max_rows],
        "row_count": min(total, max_rows),
        "total_rows": total,
        "truncated": truncated,
        "note": (
            f"全 {total} 行のうち先頭 {max_rows} 行のみ表示 (max_rows で調整可)"
            if truncated
            else None
        ),
        "runtime_sec": query_result.get("runtime"),
        "retrieved_at": query_result.get("retrieved_at"),
    }


# --------------------------------------------------------------------------- #
# MCP ツール
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_data_sources() -> list[dict]:
    """利用可能なデータソース一覧を返す。run_query の data_source_id を調べる用途。

    各要素: id / name / type / view_only(読み取り専用フラグ)。
    """
    _require_config()
    async with _client() as client:
        data = await _get(client, "/api/data_sources")
        return [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "type": d.get("type"),
                "view_only": d.get("view_only"),
            }
            for d in data
        ]


@mcp.tool()
async def run_query(
    sql: str,
    data_source_id: int,
    parameters: dict | None = None,
    max_rows: int | None = None,
    max_age: int = 0,
) -> dict:
    """アドホック SQL を指定データソースで実行し結果を返す (read-only)。

    Args:
        sql: 実行する SQL。既定では SELECT/WITH/EXPLAIN/SHOW のみ許可。
        data_source_id: 対象データソース ID (list_data_sources で確認)。
        parameters: Redash パラメータ {{name}} への値マップ。
        max_rows: 返す最大行数 (既定 REDASH_MAX_ROWS)。超過分は切り捨てて note で通知。
        max_age: キャッシュ許容秒。0 で必ず再実行、>0 で同条件の既存結果を再利用。
    """
    # SQL の妥当性は接続設定に依存しないので、設定チェックより先に判定する。
    _assert_read_only(sql)
    _require_config()
    rows_cap = max_rows or REDASH_MAX_ROWS
    payload = {
        "query": sql,
        "data_source_id": data_source_id,
        "max_age": max_age,
        "parameters": parameters or {},
    }
    async with _client() as client:
        body = await _post(client, "/api/query_results", payload)
        result_id = await _wait_for_result(client, body)
        return await _fetch_result(client, result_id, rows_cap)


@mcp.tool()
async def list_queries(search: str | None = None, page_size: int = 25, page: int = 1) -> list[dict]:
    """保存済みクエリを検索/一覧する。

    Args:
        search: 名前・本文の検索語 (未指定なら最近のクエリ)。
        page_size: 1 ページの件数 (既定 25)。
        page: ページ番号 (1 始まり)。
    """
    _require_config()
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    # 検索も一覧と同じエンドポイントに q を付けて行う。/api/queries/search は
    # 新しい Redash で廃止され、301 で /api/queries?q= に転送される (httpx は追わない)。
    if search:
        params["q"] = search
    async with _client() as client:
        data = await _get(client, "/api/queries", params=params)
        results = data.get("results", data) if isinstance(data, dict) else data
        return [
            {
                "id": q.get("id"),
                "name": q.get("name"),
                "data_source_id": q.get("data_source_id"),
                "is_draft": q.get("is_draft"),
                "updated_at": q.get("updated_at"),
                "tags": q.get("tags"),
            }
            for q in results
        ]


@mcp.tool()
async def get_query(query_id: int) -> dict:
    """保存クエリの詳細 (SQL 本文・パラメータ定義・データソース等) を返す。

    version は update_query に渡す値。schedule が null なら自動更新は無い。
    """
    _require_config()
    async with _client() as client:
        q = await _get(client, f"/api/queries/{query_id}")
        return {
            "id": q.get("id"),
            "name": q.get("name"),
            "data_source_id": q.get("data_source_id"),
            "query": q.get("query"),
            "options": q.get("options"),
            "latest_query_data_id": q.get("latest_query_data_id"),
            "updated_at": q.get("updated_at"),
            "tags": q.get("tags"),
            "is_draft": q.get("is_draft"),
            "is_archived": q.get("is_archived"),
            "schedule": q.get("schedule"),
            "version": q.get("version"),
        }


@mcp.tool()
async def update_query(query_id: int, query: str, version: int) -> dict:
    """保存クエリの SQL 本文だけを書き換える。名前・パラメータ定義・可視化は変えない。

    Args:
        query_id: 書き換える保存クエリの ID。
        query: 新しい SQL 本文。run_query と同じ read-only ガードを通す。
        version: 直前に get_query で取得した version。一致しなければ Redash が 409 を返す。
            ただし Redash は本文を更新しても version を増やさない (2026-09 に実機で確認)
            ため、これでは他者の編集を検出できない。上書きしてよいかは、直前に
            get_query で本文を取り直して確かめること。
    """
    # 保存した SQL は後で誰かが実行するので、実行時と同じ基準で保存時にも弾く。
    _assert_read_only(query)
    _require_config()
    async with _client() as client:
        q = await _post(
            client, f"/api/queries/{query_id}", {"query": query, "version": version}
        )
        return {
            "id": q.get("id"),
            "name": q.get("name"),
            "version": q.get("version"),
            "updated_at": q.get("updated_at"),
        }


@mcp.tool()
async def run_saved_query(
    query_id: int,
    parameters: dict | None = None,
    max_rows: int | None = None,
    max_age: int = 0,
) -> dict:
    """保存クエリを ID 指定で実行し結果を返す。

    Args:
        query_id: 実行する保存クエリの ID。
        parameters: クエリパラメータへの値マップ。
        max_rows: 返す最大行数 (既定 REDASH_MAX_ROWS)。
        max_age: キャッシュ許容秒。0 で必ず再実行。
    """
    _require_config()
    rows_cap = max_rows or REDASH_MAX_ROWS
    payload = {"parameters": parameters or {}, "max_age": max_age}
    async with _client() as client:
        body = await _post(client, f"/api/queries/{query_id}/results", payload)
        result_id = await _wait_for_result(client, body)
        return await _fetch_result(client, result_id, rows_cap)


@mcp.tool()
async def get_cached_result(query_id: int, max_rows: int | None = None) -> dict:
    """保存クエリの最新キャッシュ結果を再実行せずに取得する (実行負荷ゼロ)。"""
    _require_config()
    rows_cap = max_rows or REDASH_MAX_ROWS
    async with _client() as client:
        q = await _get(client, f"/api/queries/{query_id}")
        result_id = q.get("latest_query_data_id")
        if not result_id:
            raise RedashError(
                "このクエリにはキャッシュ結果がありません。run_saved_query で実行してください。"
            )
        return await _fetch_result(client, int(result_id), rows_cap)


@mcp.tool()
async def list_dashboards(search: str | None = None, page_size: int = 25, page: int = 1) -> list[dict]:
    """ダッシュボードを検索/一覧する (slug は get_dashboard で使用)。"""
    _require_config()
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if search:
        params["q"] = search
    async with _client() as client:
        data = await _get(client, "/api/dashboards", params=params)
        results = data.get("results", data) if isinstance(data, dict) else data
        return [
            {
                "id": d.get("id"),
                "slug": d.get("slug"),
                "name": d.get("name"),
                "tags": d.get("tags"),
                "updated_at": d.get("updated_at"),
            }
            for d in results
        ]


@mcp.tool()
async def get_dashboard(slug_or_id: str) -> dict:
    """ダッシュボードの構成 (含まれるウィジェット/クエリ) を返す。"""
    _require_config()
    async with _client() as client:
        d = await _get(client, f"/api/dashboards/{slug_or_id}")
        widgets = []
        for w in d.get("widgets", []):
            viz = w.get("visualization") or {}
            q = viz.get("query") or {}
            widgets.append(
                {
                    "widget_id": w.get("id"),
                    "text": w.get("text"),
                    "query_id": q.get("id"),
                    "query_name": q.get("name"),
                    "visualization": viz.get("name"),
                }
            )
        return {
            "id": d.get("id"),
            "slug": d.get("slug"),
            "name": d.get("name"),
            "widgets": widgets,
        }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
