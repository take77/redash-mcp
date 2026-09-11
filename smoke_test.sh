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
expected_tool_count=9
asserted_response_ids="1 2 3 4 5 6 7 8 9"
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

# 指定した id の応答行を出力する。照合するのは行頭にある応答自身の id だけ。
# 単に "id":N で探すと、別の応答の structuredContent にあるデータソース ID や
# クエリ ID にも一致し、失敗した応答の代わりにそちらを見て成功と誤判定する。
print_response_line() {
  local response_id="$1"
  grep -E "^\{\"jsonrpc\":\"2\.0\",\"id\":$response_id," "$response_file"
}

# 最後に送ったリクエストが最初に返るとは限らない。ローカルで弾かれる SQL は
# Redash への往復より速く返るため、id をひとつだけ待って stdin を閉じると
# 処理中の応答が取り消される。表明対象の応答が揃うまで待つこと。
all_asserted_responses_received() {
  local response_id
  for response_id in $asserted_response_ids; do
    print_response_line "$response_id" >/dev/null || return 1
  done
}

wait_for_asserted_responses() {
  local waited=0
  while [ "$waited" -lt "$max_wait_sec" ]; do
    if all_asserted_responses_received; then
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
    printf '%s\n' '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"SELECT * INTO smoke_copy FROM smoke_source_that_must_not_exist","data_source_id":1}}}'
    printf '%s\n' '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"SELECT REPLACE(CHR(97), CHR(97), CHR(98)) AS replaced","data_source_id":1}}}'
    # 存在しない query_id に送る。ガードが壊れていても実在のクエリは書き換わらない。
    printf '%s\n' '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"update_query","arguments":{"query_id":999999999,"query":"CREATE TABLE smoke_must_not_exist (id int)","version":1}}}'
    printf '%s\n' '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"list_queries","arguments":{"search":"a","page_size":1}}}'
    # 存在しないテーブルを指す。ガードが壊れていても実データは書き換わらない。
    printf '%s\n' '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"SELECT 1; REPLACE smoke_must_not_exist (a) VALUES (1)","data_source_id":1}}}'
    wait_for_asserted_responses
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
  if print_response_line "$response_id" | grep -qF "$expected"; then
    report yes "$label"
  else
    report no "$label"
  fi
}

# 指定した id の応答が届いていて、かつ特定の文言を含まないことを確かめる。
# 応答そのものが無いときに「含まない」と誤って通さないよう、先に存在を見る。
assert_response_lacks() {
  local response_id="$1" label="$2" unexpected="$3"
  if ! print_response_line "$response_id" >/dev/null; then
    report no "$label (応答が届いていない)"
    return
  fi
  if print_response_line "$response_id" | grep -qF "$unexpected"; then
    report no "$label"
  else
    report yes "$label"
  fi
}

assert_tool_count() {
  local actual
  # tools/list (id 2) は 1 行で返るため、その行の中の出現数を数える。出力全体を
  # 数えると、別の応答にある "inputSchema" という名前 (データソース名など) も数えてしまう。
  # 起動に失敗していると 0 件になるので、そこでスクリプトを止めない。
  actual="$(print_response_line 2 | grep -o '"inputSchema"' | wc -l || true)"
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
# SELECT ... INTO はテーブルを作るので、SELECT 始まりでも弾けなければならない。
assert_response_contains 5 "SELECT ... INTO が read-only ガードに弾かれる" 'キーワード (INTO) を検出した'
# 文字列関数の REPLACE() は読み取り SQL なので、ガードで弾いてはいけない。
assert_response_lacks 6 "文字列関数 REPLACE() を含む SELECT はガードに弾かれない" 'キーワード (REPLACE)'
# 保存クエリの更新でも、書き込み系の SQL は保存させない。
assert_response_contains 7 "update_query も read-only ガードを通す" '読み取り専用 SQL のみ許可しています'
# MySQL の REPLACE 文は INTO を省略できるので、into ではなく replace で弾けなければならない。
assert_response_contains 9 "INTO を省いた REPLACE 文が read-only ガードに弾かれる" 'キーワード (REPLACE) を検出した'

if has_redash_credentials; then
  assert_response_contains 4 "Redash からデータソース一覧を取得できる" '"isError":false'
  assert_response_contains 6 "REPLACE() を含む SELECT を Redash で実行できる" '"isError":false'
  # 旧 /api/queries/search は 301 を返して失敗していた。
  assert_response_contains 8 "list_queries の検索が Redash から結果を返す" '"isError":false'
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
