#!/usr/bin/env bash
# redash MCP サーバーのスモークテスト。
#
# 「起動して、クライアントに正しい応答を返せるか」を 1 コマンドで確かめる。
# mcp SDK のメジャーアップ (v1 -> v2) で起動不能になった事故と、
# 2.x でツールのエラー本文が伏せられる退行の両方を検知することが目的。
#
# 判定はサーバーの標準出力 (= MCP クライアントに届く応答) だけで行う。
# stderr のログを混ぜると、クライアントに何も届いていなくてもログ側の文言に
# マッチして通ってしまうため、両者は必ず分けておくこと。
#
# 使い方:
#   ./smoke_test.sh                                   # 隣の .env を読む
#   REDASH_ENV_FILE=profiles/lx.env ./smoke_test.sh   # プロファイルを指定
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
server_path="$script_dir/redash_mcp.py"
expected_tool_count=8
max_wait_sec=30

response_file="$(mktemp)"
log_file="$(mktemp)"
trap 'rm -f "$response_file" "$log_file"' EXIT
failures=0

# 接続情報が無い環境では Redash への実アクセスを伴う検査だけ飛ばす。
has_redash_credentials() {
  local env_file="${REDASH_ENV_FILE:-$script_dir/.env}"
  [ -f "$env_file" ] || [ -n "${REDASH_URL:-}" ]
}

# 依存の初回ダウンロードを本番セッションの外で済ませ、待ち時間を予測可能にする。
warm_up_dependencies() {
  uv run "$server_path" </dev/null >/dev/null 2>&1 || true
}

# 最後の応答が届くまで stdin を開いたまま待つ。届かなければ制限時間で打ち切る。
wait_for_last_response() {
  local waited=0
  while [ "$waited" -lt "$max_wait_sec" ]; do
    if grep -q '"id":4' "$response_file"; then
      return
    fi
    sleep 1
    waited=$((waited + 1))
  done
}

collect_responses() {
  {
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-test","version":"0"}}}'
    printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"PRAGMA smoke_test","data_source_id":1}}}'
    printf '%s\n' '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_data_sources","arguments":{}}}'
    wait_for_last_response
  } | uv run "$server_path" >"$response_file" 2>"$log_file" || true
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

# 指定した id の応答行だけを見る (サーバーログではなくクライアントが受け取る内容)。
assert_response_contains() {
  local response_id="$1" label="$2" expected="$3"
  if grep "\"id\":$response_id" "$response_file" | grep -qF "$expected"; then
    report yes "$label"
  else
    report no "$label"
  fi
}

assert_tool_count() {
  local actual
  # tools/list は 1 行で返るため、行数ではなく出現数を数える。
  # 起動に失敗していると 0 件になるので、そこでスクリプトを止めない。
  actual="$(grep -o '"inputSchema"' "$response_file" | wc -l || true)"
  if [ "$actual" -eq "$expected_tool_count" ]; then
    report yes "ツールが $expected_tool_count 個登録されている"
  else
    report no "ツール数が $expected_tool_count 個 (実際は $actual 個)"
  fi
}

echo "redash MCP スモークテスト: $server_path"
warm_up_dependencies
collect_responses

assert_response_contains 1 "サーバーが initialize に応答する" '"name":"redash"'
assert_tool_count
# 2.x は ToolError 派生でない例外の本文を伏せるため、理由が届くかどうかまで見る。
assert_response_contains 3 "read-only ガードの理由がクライアントに届く" '読み取り専用 SQL のみ許可しています'

if has_redash_credentials; then
  assert_response_contains 4 "Redash からデータソース一覧を取得できる" '"isError":false'
else
  echo "  SKIP Redash への実アクセス (接続情報が無いため)"
fi

if [ "$failures" -gt 0 ]; then
  echo
  echo "失敗 $failures 件。"
  echo "--- サーバーの応答 ---"
  cat "$response_file"
  echo "--- サーバーのログ ---"
  cat "$log_file"
  exit 1
fi

echo "すべて成功しました。"
