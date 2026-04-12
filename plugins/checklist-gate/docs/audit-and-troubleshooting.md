# 監査クエリと FAQ

## データベースの場所

```
$CLAUDE_PLUGIN_DATA/checklist-gate.sqlite
```

SQLite（WAL モード）で動作する。通常の `sqlite3` コマンドで参照できる。`$CLAUDE_PLUGIN_DATA` は Claude Code がプラグインごとに設定する永続データディレクトリである。

---

## よく使う監査クエリ

### セッション一覧

```sql
SELECT session_id, started_at, cwd
FROM sessions
ORDER BY started_at DESC
LIMIT 20;
```

### 特定セッションの ack 履歴

```sql
SELECT item, reason, created_at, consumed_at
FROM acks
WHERE session_id = '<セッションID>'
ORDER BY created_at;
```

session 型の ack（docs_checked など）は `session_checks` テーブルに記録される:

```sql
SELECT item, reason, checked_at
FROM session_checks
WHERE session_id = '<セッションID>'
ORDER BY checked_at;
```

### 未消費 ack の確認

```sql
SELECT item, reason, created_at
FROM acks
WHERE session_id = '<セッションID>'
  AND consumed_at IS NULL
ORDER BY created_at;
```

### ゲートトグルの履歴

```sql
SELECT gate_name, enabled, updated_at
FROM gate_toggles
WHERE session_id = '<セッションID>'
ORDER BY updated_at;
```

### ユーザー発話の記録（user-prompt-match 用）

user-prompt-match ack で使用されるユーザー発話記録。UserPromptSubmit hook により自動記録される。

```sql
SELECT id AS prompt_id, prompt, created_at
FROM user_prompts
WHERE session_id = '<セッションID>'
ORDER BY created_at;
```

### deny 記録の確認（deny-first 用）

deny-first の判定に使用される deny 記録。同一ゲートにつき最新の1件のみ保持される。

```sql
SELECT gate_name, created_at
FROM gate_denies
WHERE session_id = '<セッションID>';
```

---

## FAQ

### ゲートを一時的に無効にしたい

`checklist-gate:gate-off` skill を使う（または `gate-toggle off <ゲート名>`）。

- セッション単位の一時的な無効化であり、`policy.json` は変更されない
- セッション終了時に自動的にデフォルト状態に戻る
- 詳細は [運用ガイド](operations.md) を参照

### deny が出たが理由が不明

1. **deny reason を読む** -- deny メッセージに、どの ack が不足しているか、どう ack すればよいかが記載されている
2. **`gate-ack --help-gates` を実行する** -- 全ゲートと ack 項目の一覧、各項目の hint が表示される
3. hint に従って、必要な確認を行い、ack を宣言する

### DB をリセットしたい

SQLite ファイルを削除する:

```
rm $CLAUDE_PLUGIN_DATA/checklist-gate.sqlite*
```

- `*` により WAL/SHM ファイル（`-wal`, `-shm`）も同時に削除される
- 次回セッション開始時に自動的に再作成される
- 全セッションの履歴・ack 記録が失われるため注意

### hook がエラーで動かない

以下を順に確認する:

1. **stderr のログを確認する** -- hook のエラーメッセージが stderr に出力される
2. **policy.json の書式をチェックする** -- JSON の構文エラーがないか確認する。checklist-gate:apply-config skill 経由で再反映するのが最も確実
3. **policy.json が存在するか確認する** -- `$CLAUDE_PLUGIN_DATA/policy.json` が正しい場所にあるか確認する。未作成の場合は `checklist-gate:edit-config` → `checklist-gate:apply-config` skill で生成する

### セッション再開時の挙動

- **session 型の ack**（例: `docs_checked`） -- セッション中は維持される。新しいセッションでは再度 ack が必要
- **consumable 型の ack**（例: `commit_ready`） -- 使用（消費）済みの ack は再利用できない。同じ操作を再度行う場合は、新たに ack を宣言する必要がある
- **user-prompt-match 型の ack**（例: `user_authorized_push`） -- consumable と同様に消費型。加えて、ユーザー発話の prompt_id によるキーワード検証が毎回行われる
- **ランタイムトグル** -- セッション終了時に消失する。新しいセッションでは policy.json のデフォルト状態から開始される
