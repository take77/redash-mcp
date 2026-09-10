#!/usr/bin/env bash
# redash MCP サーバーのスモークテスト。
#
# 「起動して主要な応答を返せるか」を 1 コマンドで確かめる。
# mcp SDK のメジャーアップ (v1 -> v2) でサーバーが起動不能になった事故があり、
# この種の破壊は Claude Code を再起動しなくても手元で検知できるようにしておく。
#
# 使い方:
#   ./smoke_test.sh                                   # 隣の .env を読む
#   REDASH_ENV_FILE=profiles/lx.env ./smoke_test.sh   # プロファイルを指定
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
server_path="$script_dir/redash_mcp.py"
expected_tool_count=8
response_wait_sec=8

output_file="$(mktemp)"
trap 'rm -f "$output_file"' EXIT
failures=0

# 接続情報が無い環境では Redash への実アクセスを伴う検査だけ飛ばす。
has_redash_credentials() {
  local env_file="${REDASH_ENV_FILE:-$script_dir/.env}"
  [ -f "$env_file" ] || [ -n "${REDASH_URL:-}" ]
}

# 依存の初回ダウンロードを本番セッションの外で済ませる (待ち時間が伸びるのを防ぐ)。
warm_up_dependencies() {
  uv run "$server_path" </dev/null >/dev/null 2>&1 || true
}

# MCP の初期化からツール呼び出しまでを stdio に流し込み、応答を output_file に集める。
collect_responses() {
  {
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-test","version":"0"}}}'
    printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"PRAGMA smoke_test","data_source_id":1}}}'
    printf '%s\n' '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_data_sources","arguments":{}}}'
    sleep "$response_wait_sec"
  } | uv run "$server_path" >"$output_file" 2>&1 || true
}

report() {
  local passed="$1" label="$2"
  if [ "$passed" = "yes" ]; then
    echo "  OK   $label"
    return
  fi
  echo "  FAIL $label"
  failures=$((failures + 1))
}

assert_contains() {
  local label="$1" expected="$2"
  if grep -qF "$expected" "$output_file"; then
    report yes "$label"
  else
    report no "$label"
  fi
}

assert_tool_count() {
  local actual
  # tools/list は 1 行で返るため、行数ではなく出現数を数える。
  actual="$(grep -o '"inputSchema"' "$output_file" | wc -l)"
  if [ "$actual" -eq "$expected_tool_count" ]; then
    report yes "ツールが $expected_tool_count 個登録されている"
  else
    report no "ツール数が $expected_tool_count 個 (実際は $actual 個)"
  fi
}

echo "redash MCP スモークテスト: $server_path"
warm_up_dependencies
collect_responses

assert_contains "サーバーが initialize に応答する" '"serverInfo":{"name":"redash"'
assert_tool_count
# 1.x では任意の例外の本文が届いていたが、2.x は ToolError の派生でないと本文が伏せられる。
assert_contains "read-only ガードの理由がクライアントに届く" '読み取り専用 SQL のみ許可しています'

if has_redash_credentials; then
  # エラー応答も result で返るため、その呼び出しが成功扱いかどうかまで見る。
  if grep '"id":4' "$output_file" | grep -qF '"isError":false'; then
    report yes "Redash からデータソース一覧を取得できる"
  else
    report no "Redash からデータソース一覧を取得できる"
  fi
else
  echo "  SKIP Redash への実アクセス (接続情報が無いため)"
fi

if [ "$failures" -gt 0 ]; then
  echo "失敗 $failures 件。応答の全文:"
  cat "$output_file"
  exit 1
fi

echo "すべて成功しました。"
