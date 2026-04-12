# シナリオ集と ack 書き方ガイド

## シナリオ A: ユーザーが明示的に push を依頼（user-prompt-match フロー）

**状況**: ユーザーが「push して」と明確に依頼した。`user_authorized_push` が `user-prompt-match` タイプで設定されている。

### フロー

1. ユーザーが「push して」と発話する
2. **UserPromptSubmit hook** が発話を DB に記録し、`[checklist-gate: prompt_id=42]` をエージェントに通知する
3. Claude が `git push` を実行しようとする
4. **git_push_gate が deny** を返す -- `user_authorized_push` の ack が必要（deny が state に記録される）
5. Claude が deny メッセージを読む（考え直す機会）
6. Claude が ack を宣言する:
   ```
   gate-ack user_authorized_push --prompt-id 42 --reason "ユーザー発話『push して』を受信（prompt_id=42）"
   ```
7. フックが検証する: deny-first チェック → 発話実在チェック → キーワードチェック（"push" が含まれる） → 全て OK
8. 再度 `git push` を実行する
9. ack が消費され、ゲートを通過する

### ポイント

- `--prompt-id` で根拠となるユーザー発話の ID を必ず指定する
- フックが発話原文を機械的に検証するため、エージェントが曖昧な発話を都合よく解釈して ack を通過することを防止できる
- `user_authorized_push` は user-prompt-match 型のため、push のたびに ack が必要（consumable と同じ消費型）

---

## シナリオ A-2: deny-first による先回り ack 防止

**状況**: ユーザーが「push して」と発話した直後、エージェントが deny を受ける前に先回りで gate-ack を実行しようとする。

### フロー

1. ユーザーが「push して」と発話する（prompt_id=42 が通知される）
2. Claude が deny を受ける前に先回りで ack を試みる:
   ```
   gate-ack user_authorized_push --prompt-id 42 --reason "ユーザー発話を受信"
   ```
3. **deny-first チェックで拒否される**: 対応するゲート（git_push_gate）の deny が記録されていないため、ack が受け付けられない
4. エラーメッセージ: `[gate-ack] Error: no prior deny for gate(s) 'git_push_gate'. ack is only accepted after a deny.`
5. Claude は実際に `git push` を試みて deny を受けてから、改めて ack を宣言する必要がある

### ポイント

- deny-first は全ての ack タイプ（session / consumable / user-prompt-match）に適用される
- エージェントが deny メッセージを読む機会が構造的に保証される

---

## シナリオ A-3: 肯定応答フロー（--affirm を使用するケース）

**状況**: AIが「push しますか？」と質問し、ユーザーが「OK」と答えた。発話原文にキーワード（"push"）が含まれないため、`--affirm` オプションを使用する。

### フロー

1. Claude がユーザーに「push しますか？」と質問する
2. ユーザーが「OK」と発話する
3. **UserPromptSubmit hook** が発話を DB に記録し、`[checklist-gate: prompt_id=43]` をエージェントに通知する
4. Claude が `git push` を実行しようとする
5. **git_push_gate が deny** を返す -- `user_authorized_push` の ack が必要（deny が state に記録される）
6. Claude が deny メッセージを読む（考え直す機会）
7. 発話原文「OK」にはキーワード（"push"）がないため、`--affirm` を使用して ack を宣言する:
   ```
   gate-ack user_authorized_push --prompt-id 43 --affirm "push しますか？" --reason "AIの質問『push しますか？』に対しユーザーがOKと回答（prompt_id=43）"
   ```
8. フックが検証する:
   - deny-first チェック → OK（deny が記録されている）
   - 発話実在チェック → OK（prompt_id=43 が存在する）
   - 肯定応答チェック → OK（「OK」が affirm_keywords に含まれる）
   - `--affirm` の値に対する match_keywords チェック → OK（「push しますか？」に "push" が含まれる）
9. 再度 `git push` を実行する
10. ack が消費され、ゲートを通過する

### ポイント

- `--affirm` にはAIがユーザーに発した質問のテキストを引用する
- フックは `--affirm` の値に対してキーワードチェックを行い、ユーザー発話に対しては affirm_keywords のチェックのみを行う
- ユーザー発話が affirm_keywords に該当しない場合（例: 「考えておく」）は `--affirm` の使用が拒否される
- `--affirm` を使わずに通常フローで ack しようとすると、発話原文にキーワードがないためキーワードチェックで拒否される

---

## シナリオ B: Claude が勝手に push しようとする

**状況**: ユーザーからの明示的な push 指示がないまま、Claude が git push を試みる。

### フロー

1. Claude が `git push` を実行しようとする
2. **git_push_gate が deny** を返す -- `user_authorized_push` の ack が必要（deny が state に記録される）
3. Claude は deny メッセージを読む
4. user-prompt-match の場合、ユーザー発話に "push" / "プッシュ" が含まれる prompt_id が必要だが、該当する発話がない
5. **ack を宣言できず、ユーザーに確認する**:「push してよろしいですか?」
6. ユーザーが「push して」と承認する（新しい prompt_id が発行される）
7. Claude が新しい prompt_id で ack を宣言して push する

### ポイント

- deny-first により、deny を受ける前に先回り ack はできない
- user-prompt-match により、ユーザー発話にキーワードが含まれていなければ ack が通らない
- この2つの仕組みの組み合わせにより、エージェントが勝手に push することを構造的に防止する

---

## シナリオ C: タスク開始時の docs 確認

task_start ゲートは gate トリガーで動作する。patterns に指定されたツール（`Edit(*)`, `Write(*)`, `Read(*)`, `Glob(*)`, `Grep(*)`）を `docs_checked` の ack がなければブロックする。ただし except_patterns に該当する操作（`Read(*README*)`, `Read(*DEVELOPMENT*)`, `Glob(*README*)`, `Glob(*DEVELOPMENT*)`）は ack 前でも通す。

### C-1: docs が存在する場合

**状況**: プロジェクトに README.md が存在する。

#### フロー

1. Claude がファイル編集（Edit / Write）を実行しようとする
2. **task_start ゲートが deny** を返す -- `docs_checked` の ack が必要
3. Claude が README.md を Read で読む -- `except_patterns` に該当するためブロックされない
4. ack を宣言する:
   ```
   gate-ack docs_checked --reason "README.md を Read で確認済み。開発ルールとセットアップ手順を把握した"
   ```
5. ファイル編集がゲートを通過する

#### ポイント

- `docs_checked` は session 型のため、セッション中に1回 ack すれば以降は不要
- DEVELOPMENT.md が存在する場合はそちらも確認すること
- README.md / DEVELOPMENT.md の Read・Glob は `except_patterns` により ack 不要で実行できる

### C-2: docs が存在しない場合

**状況**: プロジェクトに README.md も DEVELOPMENT.md も存在しない。

#### フロー

1. Claude がファイル編集を実行しようとする
2. **task_start ゲートが deny** を返す
3. Claude が Glob で README.md / DEVELOPMENT.md を検索し、不在を確認する -- `except_patterns` に該当するためブロックされない
4. 「不在」で ack を宣言する:
   ```
   gate-ack docs_checked --reason "Glob で README.md / DEVELOPMENT.md を検索したが不在"
   ```
5. ファイル編集がゲートを通過する

#### ポイント

- 「存在しない」も正当な根拠になる
- deny reason テンプレートにも「the file does not exist」の場合は事実を記載するよう案内がある

### C-3: 外部ドキュメント指示の場合

**状況**: ユーザーが「CONTRIBUTING.md を読んでから作業して」など、README.md / DEVELOPMENT.md 以外のドキュメントを参照するよう指示している。

#### フロー

1. Claude が外部ドキュメント（例: CONTRIBUTING.md）を Read しようとする
2. **task_start ゲートが deny** を返す -- `except_patterns` に該当しないためブロックされる
3. Claude が先に ack を宣言する:
   ```
   gate-ack docs_checked --reason "ユーザー指示で CONTRIBUTING.md を参照する。README.md / DEVELOPMENT.md はプロジェクトに不在"
   ```
4. ack 後、Read / Edit を含む全ツールがゲートを通過する

#### ポイント

- `except_patterns` は README.md / DEVELOPMENT.md に限定されているため、それ以外のファイルへのアクセスはブロック対象
- ack の reason には「なぜ通常の docs 確認ではなく別ドキュメントを参照するのか」を明記する

---

## シナリオ D: コミットせずに終了（stop-time ゲート）

**状況**: stop-time トリガーのゲートをユーザーが追加で設定している。

### フロー

1. セッション終了時に stop-time ゲートが発火する
2. 未コミットの変更がある場合、ゲートが警告を出す
3. Claude がユーザーにコミットの要否を確認する

### ポイント

- stop-time ゲートはデフォルトの設定には含まれていない
- 有効にするには `policy-source.md` にゲートを追加し、apply-config で反映する（詳細は [設定リファレンス](configuration.md) を参照）

---

## シナリオ E: ToDoリスト的な使い方（1ゲートに複数 ack）

**状況**: デプロイコマンドの実行前に、「テスト完了」「ステージング確認」「ユーザー承認」の3つのタスク全てが完了していることを要求したい。

### 前提となる設定

ゲートの `require` に複数の ack 項目を指定する（設定方法は [設定リファレンス](configuration.md) の「デプロイ前の確認ゲート」の例を参照）。

```
要求 ack: tests_passed, staging_verified, user_authorized_deploy
```

### フロー

1. Claude が `deploy` コマンドを実行しようとする
2. **deploy_gate が deny** を返す -- `tests_passed`, `staging_verified`, `user_authorized_deploy` の3つの ack が必要
3. Claude がタスクを順に実施し、それぞれ ack を宣言する:
   ```
   gate-ack tests_passed --reason "npm test を実行し全 42 件パス確認済み"
   ```
   ```
   gate-ack staging_verified --reason "ステージング環境で主要画面の動作確認を完了。エラーなし"
   ```
   ```
   gate-ack user_authorized_deploy --reason "ユーザー発話『本番にデプロイして』を直接受信"
   ```
4. 3つ全ての ack が揃った状態で、再度 `deploy` コマンドを実行する
5. ゲートを通過する

### ポイント

- `require` 配列に複数の ack 項目を指定すると、**全ての ack が揃うまでゲートを通過できない**
- ack の宣言順序は自由。どの順番で宣言しても、全て揃えば通過する
- 各 ack 項目を consumable にすれば、デプロイのたびに毎回チェックリストを最初からやり直す運用になる
- 一部を session 型にすれば、セッション中は再宣言不要にできる（例: テスト結果はセッション中有効にするなど）
- 1つでも ack が不足していれば deny メッセージに**不足している項目の一覧**が表示されるため、残りの ToDo が一目でわかる

---

## ack の reason 書き方ガイド

### エラーメッセージと対処方法

gate-ack が拒否された場合、以下のメッセージが表示されることがあります。

**deny-first 違反（先回り ack）:**
```
[gate-ack] Rejected: no prior deny for gate(s) 'git_push_gate'.
This is an opportunity to reconsider.
Retry the operation and follow the deny message to reflect on your action.
```
→ まず操作を実行して deny を受けてから ack を宣言してください。

**キーワード検証失敗（user-prompt-match）:**
```
[gate-ack] Rejected: insufficient grounds for authorization.
Re-confirm with the user and gather the necessary context before retrying.
```
→ ユーザーに再度確認し、必要なコンテキスト（キーワードを含む発話）を収集してから再試行してください。具体的な失敗理由（どのキーワードが不足しているか等）は意図的に表示されません。

### 基本ルール

- **具体的な事実を書く** -- 何を確認したか、誰が指示したか、を明記する
- **日本語で書いてよい**
- **最低文字数がある** -- ack 項目ごとに `min_reason_length` が設定されている（デフォルト: 20文字）
  - 短すぎる reason はシステムに拒否される
  - 最低文字数は「具体的な根拠を書かせる」ための仕組み

### 良い例

| ack 項目 | reason |
|----------|--------|
| `docs_checked` | README.md を Read で確認済み。開発ルールとセットアップ手順を把握した |
| `docs_checked` | Glob で README.md / DEVELOPMENT.md を検索したが不在 |
| `commit_ready` | テスト全件パス確認済み。git diff で変更内容を確認し、不要な変更がないことを確認した |
| `user_authorized_push` | ユーザー発話『これを push して』を受信（prompt_id=42） |
| `user_authorized_push` | ユーザー発話『masterにpushしといて』を受信（prompt_id=55）。対象ブランチ: master |
| `user_authorized_push` | AIの質問『push しますか？』に対しユーザーがOKと回答（prompt_id=43） |

### 悪い例

| ack 項目 | reason | 問題点 |
|----------|--------|--------|
| `docs_checked` | 確認した | 何を確認したか不明。最低文字数にも満たない |
| `commit_ready` | OK | 根拠がない。最低文字数にも満たない |
| `user_authorized_push` | push する | ユーザーからの指示への言及がない |
| `user_authorized_push` | 多分 push していいと思う | 推測であり、明示的な指示の根拠がない |

### reason を書くときのチェックポイント

- 第三者が読んで「なぜ ack したか」を理解できるか?
- 事実に基づいているか?（推測や希望ではないか?）
- 最低文字数を満たしているか?
