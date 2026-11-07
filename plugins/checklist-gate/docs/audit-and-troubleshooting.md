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
SELECT session_id, started_at, last_active_at, cwd
FROM sessions
ORDER BY COALESCE(last_active_at, started_at) DESC
LIMIT 20;
```

`last_active_at` はセッション開始（再開を含む）と発話の記録で更新される。古いデータの掃除はこの時刻を基準にする。

### 特定セッションの ack 履歴

`consumable` / `user-prompt-match` 型の ack は `acks` テーブルに記録される:

```sql
SELECT item, reason, created_at, consumed_at
FROM acks
WHERE session_id = '<セッションID>'
ORDER BY created_at;
```

`session` 型の ack（docs_checked など）は `session_checks` テーブルに記録される:

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

user-prompt-match ack で使用されるユーザー発話記録。UserPromptSubmit hook により自動記録される。システムの通知だけの発話は記録されない。IDE の情報や貼り付けたテキストは原文のまま残る（照合のときに除かれる）。

```sql
SELECT id AS prompt_id, prompt, created_at
FROM user_prompts
WHERE session_id = '<セッションID>'
ORDER BY created_at;
```

### 自動通過の記録

自動通過で記録された ack は、reason が `auto-authorized:` で始まる。

```sql
SELECT item, prompt_id, reason, created_at, consumed_at
FROM acks
WHERE session_id = '<セッションID>'
  AND reason LIKE 'auto-authorized:%'
ORDER BY created_at;
```

自動通過の前提ゲートを最後に通過したターンは `gate_passes` に記録される。

```sql
SELECT gate_name, prompt_id
FROM gate_passes
WHERE session_id = '<セッションID>';
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

`checklist-gate:gate-off` skill を使う（または `gate-toggle off <ゲート名> --prompt-id <id>`）。Claude に「<ゲート名> を外して」「push のゲートを OFF にして」のように頼むと、その発話を根拠に無効化される。

- セッション単位の一時的な無効化であり、`policy.json` は変更されない
- 絶対に止めるゲート（`block: true`）は外せない。policy-source.md を直して apply-config で反映する
- 同じセッションの再開では持ち越され、`/clear` や新しいセッションでは policy.json の状態から始まる
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

### ほとんどのツールが止まる（policy.json が不正）

policy.json が不正なとき、読み取り専用のツールと、apply-config による policy.json の書き込み以外はすべて止まる。deny のメッセージとセッション開始の知らせに不正の箇所が表示される。

1. **不正の箇所を読む** -- 未知のキー（綴り違い）、型の誤り、存在しない ack 項目の参照などが示される
2. **直す** -- Claude に「設定を反映して」と依頼する（checklist-gate:apply-config skill が policy-source.md から作り直す）。policy-source.md が原因なら先に直す
3. **手で直した場合は検証する** -- `python3 <プラグインのパス>/scripts/validate_policy.py $CLAUDE_PLUGIN_DATA/policy.json` が `OK` を返すことを確かめる

### 内部エラーで止まった

DB のロックやフックの内部エラーが起きたとき、ack を要求するゲートが関わる操作は止まり、「再試行して」というメッセージが出る。同じ操作を再試行すれば通るのが通常。繰り返し止まるときは次を確認する。

1. **stderr のログを確認する** -- フックのエラーメッセージが出力される
2. **DB が壊れていないか確認する** -- 壊れているときは上の「DB をリセットしたい」を行う
3. **Python のバージョンを確認する** -- フックは `python3` で起動される。3.9 以上であること

天の声ゲートだけが関わる操作と、どのゲートも関わらない操作は、内部エラーが起きても止まらない。

### policy.json が無いとき

ゲートは開いたままで、セッションの最初に未作成の知らせが出る。`checklist-gate:edit-config` → `checklist-gate:apply-config` skill で生成する。

### フックの失敗を止める側に倒す設定（onFailure）

PreToolUse のフックには、起動の失敗・時間切れ・想定外の終了を止める側に倒す設定（`onFailure: block`）を付けている。この設定は対応した版の Claude Code でだけ効き、古い版では無視されて通る。スクリプト内の異常を止める判断は版によらず効く。

### セッション再開時の挙動

- **session 型の ack**（例: `docs_checked`） -- セッション中は維持される。新しいセッションでは再度 ack が必要
- **consumable 型の ack**（例: `commit_ready`） -- 使用（消費）済みの ack は再利用できない。同じ操作を再度行う場合は、新たに ack を宣言する必要がある
- **user-prompt-match 型の ack**（例: `user_authorized_push`） -- consumable と同様に消費型。加えて、ユーザー発話の prompt_id によるキーワード検証が毎回行われる
- **ランタイムトグル** -- 同じセッションの再開では持ち越される。`/clear` や新しいセッションでは policy.json の状態から開始される
