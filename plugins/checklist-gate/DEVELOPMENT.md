# DEVELOPMENT.md

checklist-gate プラグインの開発者向けアーキテクチャ説明書。

---

## 1. アーキテクチャ概要

### コンポーネント間のデータフロー

```
SessionStart hook
  |
  v
session_init.py  --->  State (SQLite)  --- セッション作成 / ハウスキーピング
  |
  v
[ユーザーが発話]
  |
  v
UserPromptSubmit hook
  |
  v
prompt_store.py  --->  State (SQLite)  --- ユーザー発話記録（prompt_id 発行）
  |                                        additionalContext で prompt_id をエージェントに通知
  |
  v
[Claude がツールを使用]
  |
  v
PreToolUse hook
  |
  v
gate_check.py  --->  State (SQLite)  --- ack 確認
  |                   policy.json    --- ゲート定義の読み込み
  |
  +--> gate-ack / gate-toggle コマンド検出時: hook 内部で ack を記録し、
  |    command を echo メッセージに書き換えて通過（外部 CLI は呼ばない）
  +--> ゲート条件未充足: deny レスポンス（ack を要求するメッセージを返却）
  |    deny 発生時に gate_denies テーブルに記録（deny-first 用）
  +--> ゲート条件充足: 通過（consumable / user-prompt-match ack は消費される）
  |
  v
[Claude が gate-ack コマンドを Bash で実行しようとする]
  |
  v
gate_check.py が検出 → deny-first チェック → (user-prompt-match の場合) 発話検証
  → ack を記録 → gate_denies を削除 → command を echo に書き換え
  |
  v
[再度ツール使用 -> PreToolUse -> gate_check.py -> 今度は通過]
  |
  v
Stop hook
  |
  v
stop_gate.py  --->  State (SQLite)  --- stop-time ゲート評価
              --->  git               --- 未コミット変更のチェック
  |
  +--> ブロック条件あり: block レスポンス
  +--> 条件なし: 通過
```

### 典型的なフロー

1. **SessionStart**: セッション開始時に session_init.py がセッションを作成し、古いデータを掃除する
2. **UserPromptSubmit**: ユーザーの発話ごとに prompt_store.py が発話を DB に記録し、prompt_id をエージェントに通知する
3. **PreToolUse**: ツール使用時に gate_check.py がポリシーに基づいてゲート判定を行う
4. **deny**: ゲート条件未充足の場合、deny レスポンスを返却し、gate_denies テーブルに deny を記録する
5. **gate-ack**: ゲートで拒否された場合、ack コマンドで条件を宣言する。deny-first により、deny が記録されていない場合は ack が拒否される
6. **PreToolUse (再試行)**: 再度ツールを使用すると、ack 済みのため通過する
7. **Stop**: セッション終了時に stop_gate.py が stop-time ゲートを評価する

### 責務分担

- **hooks (hooks.json)**: どのフックイベントでどのスクリプトを実行するかの宣言
- **scripts/prompt_store.py**: UserPromptSubmit hook スクリプト。ユーザーの発話を DB に記録し、additionalContext で prompt_id をエージェントに通知する
- **scripts/gate_check.py**: PreToolUse 時のゲート判定、deny/allow の決定、deny-first の enforce、user-prompt-match の発話検証・キーワードチェック、gate-ack / gate-toggle コマンドの内部処理
- **gate-ack / gate-toggle コマンド**: Claude が Bash で実行するコマンド。gate_check.py が PreToolUse フックで検出し、内部で処理する（外部バイナリは実行されない）
- **State (state.py)**: SQLite を用いたデータの永続化（DAO 層）
- **policy.json**: ゲートルール・ack 項目の宣言的定義

---

## 2. 主要モジュールの責務と依存関係

### ファイル一覧と責務

| ファイル | 責務 |
|---|---|
| hooks/hooks.json | フックイベント（SessionStart / PreToolUse / Stop）と実行するスクリプトの対応定義。`$CLAUDE_PLUGIN_ROOT` 環境変数を使って scripts/ 配下の Python スクリプトを直接呼び出す |
| scripts/session_init.py | セッション作成、スキーマ初期化、古いデータのハウスキーピング、セッションコンテキストの出力 |
| scripts/prompt_store.py | UserPromptSubmit hook スクリプト。ユーザー発話を DB に記録し、additionalContext で prompt_id をエージェントに通知 |
| scripts/gate_check.py | PreToolUse 時のゲート判定、deny/allow の決定、deny-first の enforce、user-prompt-match の発話検証、gate-ack / gate-toggle コマンドの内部処理（ack 記録・トグル操作） |
| scripts/stop_gate.py | Stop フック発火時の stop-time ゲート評価（未コミット変更チェック、未消費 ack チェック） |
| lib/state.py | SQLite DAO 層。全テーブルの CRUD 操作、スキーマ初期化、トランザクション管理。DB は `$CLAUDE_PLUGIN_DATA/checklist-gate.sqlite` に配置 |
| config/policy.json.example | ゲートルールと ack 項目のスキーマリファレンス。apply-config skill がこのファイルを参照して `$CLAUDE_PLUGIN_DATA/policy.json` を生成する |
| templates/ | deny 理由やセッションコンテキストのテンプレートファイル。deny-reason.txt は `{gate_name}`, `{require_list}`, `{ack_commands}`, `{ack_hint}`, `{exit_commands}` の5変数を使用。session-context.txt は `{plugin_data_dir}` 変数を使用（session_init.py が置換） |
| config/policy-source.md | ゲートルールと ack 項目のデフォルトテンプレート（Markdown 形式）。初回セッション時に `$CLAUDE_PLUGIN_DATA` にコピーされ、ユーザーはコピー先を編集する。apply-config skill で policy.json に変換する |
| skills/ | Claude Code の skill 定義。apply-config（設定反映）、edit-config（設定編集）、gate-on / gate-off（ゲートの有効/無効切り替え）を提供 |
| docs/ | ユーザー向けドキュメント（導入手順、設定ガイド、運用ガイド、シナリオ例、監査・トラブルシューティング） |
| tests/ | unittest ベースの単体テスト（test_state.py） |

### 依存関係の方向

- scripts/* は lib/state.py に依存する（State クラスを利用）
- scripts/gate_check.py, scripts/session_init.py は `$CLAUDE_PLUGIN_DATA/policy.json` を読み込む
- scripts/prompt_store.py は policy.json を読まない（発話記録のみ）
- scripts/gate_check.py は templates/ のテンプレートを読み込む
- scripts/session_init.py は templates/ のテンプレートを読み込む
- lib/state.py は外部依存なし（Python 標準ライブラリのみ）
- hooks/hooks.json は `$CLAUDE_PLUGIN_ROOT` 経由で scripts/* を直接起動する
- skills/* は Claude Code が skill として読み込み、ユーザーの指示に応じて起動する
- apply-config skill は config/policy-source.md を読み込み、config/policy.json.example をスキーマ参照として policy.json を生成する

### 設計判断の補足

#### gate-ack / gate-toggle を hook 内部で処理する理由

gate-ack / gate-toggle コマンドは gate_check.py が PreToolUse フックで検出し、内部で ack 記録 + command を echo メッセージに書き換えて処理する。外部バイナリとして実行しない理由は以下の通り。

- **permissions 問題**: Claude Code は Bash コマンドの実行前にユーザー許可を求める。gate-ack は `--reason` 引数の内容が毎回変わるため Always allow が効かず、ack のたびにユーザーに許可ダイアログが表示される

#### hooks.json のパス解決

Claude Code はプラグインの hook 実行時に `$CLAUDE_PLUGIN_ROOT` 環境変数をプラグインのインストール先ディレクトリに設定する。hooks.json ではこの変数を使って scripts/ 配下の Python スクリプトを直接呼び出す。

#### $CLAUDE_PLUGIN_DATA（永続データディレクトリ）

Claude Code はプラグインの hook 実行時に `$CLAUDE_PLUGIN_DATA` 環境変数をプラグイン固有の永続データディレクトリに設定する。`$CLAUDE_PLUGIN_ROOT`（コードディレクトリ）はプラグイン更新時に上書きされるため、ユーザーデータ（policy.json, policy-source.md, SQLite DB）は `$CLAUDE_PLUGIN_DATA` に配置する。

#### Stop フックの発火タイミング

Stop フックは「セッション終了時」ではなく「Claude が応答を終えるたび」に発火する。そのため sessions テーブルにはセッション終了を表すカラムを持たない。

---

## 3. 拡張ポイント

### 新しいゲート種別の追加

- policy-source.md にゲート定義を追加し、apply-config skill で policy.json に反映する（直接 policy.json を編集しても可）
- trigger.type に応じた判定ロジックが gate_check.py に必要
- 現在対応している trigger.type: gate, stop-time

### 新しい stop-time チェックの追加

- policy.json のゲート定義で trigger.type を stop-time、trigger.check に新しいチェック名を指定する
- stop_gate.py にチェック名に対応する判定処理を追加する

### 新しい ack 項目の追加

- policy-source.md の ack 項目セクションに定義を追加し、apply-config skill で policy.json に反映する（type, min_reason_length, hint を指定）
- type は session（セッション中1回で永続）、consumable（使い捨て）、または user-prompt-match（ユーザー発話検証付き使い捨て）を選択
- user-prompt-match タイプの場合は match_keywords, except_keywords, max_prompt_distance を追加で指定する
- 対応するゲートの require 配列に項目名を追加する

### skill の追加

- skills/ 配下にディレクトリを作成し、SKILL.md を配置する
- SKILL.md の YAML frontmatter に name と description を記載する
- description にはトリガーとなるユーザーの発話例を含める
- 既存の skill（apply-config, edit-config, gate-on, gate-off）を参考にする

### trigger.type ごとの動作

#### gate

ツール呼び出しや Bash コマンド実行をゲート対象とする。patterns / except_patterns に `Tool(パターン)` 形式で対象を指定する（Claude Code の permissions と同じ記法）。

記法の例:
- `Edit(*)` -- Edit ツール全体
- `Read(*README*)` -- README を含むパスの Read
- `Bash(git push *)` -- git push コマンド
- `Bash(git push --dry-run *)` -- dry-run 付き git push

#### stop-time

Stop フック発火時に評価されるゲート。trigger.check に組み込みチェッカー名を指定する。

---

## 4. deny-first の仕組み

deny-first は全ての ack タイプ（session / consumable / user-prompt-match）に共通する仕組み。gate-ack は対応するゲートの deny が発生した後にのみ受け付ける。

### データ構造

gate_denies テーブルで `(session_id, gate_name)` を UNIQUE 制約で管理する。同一ゲートにつき最新の deny のみ保持する。

### ライフサイクル

1. **deny 発生時**: gate_check.py がゲート条件未充足を検出すると、deny レスポンスを返却すると同時に gate_denies テーブルに UPSERT（INSERT OR REPLACE）で記録する
2. **ack 成立時**: gate-ack が成功すると、対応する `(session_id, gate_name)` のレコードを削除する。次回は再度 deny を経る必要がある
3. **再 deny 時**: 同一ゲートで再度 deny が発生すると、UPSERT により created_at が更新される

この仕組みにより:
- エージェントが deny メッセージを読む前に先回り ack することを構造的に防止する
- 1 deny → 1 ack の対応が保証される
- 古い deny 記録の使い回しが防止される

---

## 5. user-prompt-match ack タイプ

エージェントの自己申告ではなく、ユーザーの発話原文を機械的に検証する ack タイプ。

### UserPromptSubmit hook による発話記録

UserPromptSubmit hook（prompt_store.py）がユーザーの発話を user_prompts テーブルに記録し、prompt_id を発行する。フックの additionalContext 出力でエージェントに通知する。

通知フォーマット: `[checklist-gate: prompt_id=N]`

### 検証フロー

gate-ack 実行時に gate_check.py が以下の順序で検証する:

1. **deny-first チェック**: 対応するゲートの deny が gate_denies に記録されているか
2. **発話実在チェック**: 指定された prompt_id が当該セッションの user_prompts に存在するか
3. **max_prompt_distance チェック**: prompt_id が直近 N 件の発話以内に含まれるか（古い発話の持ち出し防止）
4. **キーワード検証**（`--affirm` の有無で対象が異なる）

いずれかの検証に失敗した場合、ack を拒否してエラーメッセージを返却する。

### キーワード評価の流れ

キーワードの比較は、空白を除去し英字を小文字に変換した上で行う。

**`--affirm` なしの場合（通常フロー）:**

1. 指定された prompt_id の発話原文を取得
2. **match 判定**: match_keywords のいずれかが発話に含まれるか → 含まれなければ NG
3. **except 判定**: except_keywords のいずれかが発話に含まれるか → 含まれれば NG
4. 両方通過すれば OK

**`--affirm` ありの場合（肯定応答フロー）:**

ユーザー発話が「OK」「はい」など肯定応答のみで、キーワードを含まない場合に使用する。`--affirm` の値にはAIがユーザーに発した質問の引用テキストを指定する。

1. 指定された prompt_id の発話原文を取得
2. **肯定応答チェック**: 発話原文が affirm_keywords（policy.json トップレベル）のいずれかに該当するか → 該当しなければ拒否
3. **`--affirm` の値に対して match 判定**: match_keywords のいずれかが含まれるか → 含まれなければ NG
4. **`--affirm` の値に対して except 判定**: except_keywords のいずれかが含まれるか → 含まれれば NG
5. 両方通過すれば OK

例:
- ユーザー発話「push して」→ `--affirm` 不要。発話原文に "push" が含まれるので通常フローで通過
- AI質問「push しますか？」→ ユーザー応答「OK」→ `--affirm "push しますか？"` を指定。affirm の値に "push" が含まれるので通過

### affirm_keywords

policy.json のトップレベルに定義する、肯定応答判定用のキーワード一覧。`--affirm` を使用する際に、ユーザー発話がこれらのキーワードのいずれかに該当するかを事前チェックする。

```json
"affirm_keywords": ["OK", "はい", "よい", "いいよ", "お願い", "yes", "sure", "うん", "おけ", "いいね"]
```

- OR 条件: いずれか1つが発話に含まれれば肯定応答と判定
- 比較は空白除去・小文字変換後に行う

### JSON 設定例

```json
{
  "user_authorized_push": {
    "type": "user-prompt-match",
    "min_reason_length": 20,
    "hint": "ユーザー発話に明示的な push 指示があった？",
    "match_keywords": ["push", "プッシュ"],
    "except_keywords": ["dry-run", "ドライラン"],
    "max_prompt_distance": 3
  }
}
```

---

## 6. テスト戦略

### 単体テスト

- unittest を使用する
- State クラスのテストでは :memory: SQLite を使用し、ファイルシステムに依存しない
- 各テストケースで State インスタンスを新規作成し、テスト間の状態干渉を避ける

#### test_state.py

State クラスの全 CRUD 操作をカバーする:
- セッション操作、SessionCheck 操作、Ack 操作、GateToggle 操作
- UserPrompt 操作（add / get / is_prompt_within_distance / get_oldest_valid_prompt_id）
- GateDeny 操作（record_deny / has_deny / clear_deny の UPSERT 動作含む）
- クリーンアップ（全テーブルの古いデータ削除）
- トランザクション（ロールバック・コミット）

#### test_gate_check_affirm.py

gate_check.py の --affirm ロジックをカバーする:
- 肯定応答フロー（--affirm あり）の成功・拒否パターン
- 通常フロー（--affirm なし）のキーワード検証（unified reject メッセージ）
- affirm_keywords との照合、match_keywords / except_keywords の --affirm 値への適用
- 空白・大文字小文字の正規化

#### test_gate_check.py（将来追加予定）

gate_check.py のゲート判定ロジックをカバーする:
- deny-first の enforce ロジック
- user-prompt-match の発話検証・キーワードチェック
- gate-ack / gate-toggle コマンドのパースと処理

#### test_prompt_store.py（将来追加予定）

prompt_store.py の UserPromptSubmit hook 処理をカバーする:
- hook 入力 JSON のパースと発話記録
- additionalContext 出力フォーマットの検証

### 手動シナリオテスト

- 実際の Claude Code セッションで以下のシナリオを確認する:
  - ドキュメント未確認の状態でファイル編集が拒否されること
  - gate-ack 後にファイル編集が許可されること
  - git commit が commit_ready ack なしで拒否されること
  - git push が user_authorized_push ack なしで拒否されること
  - deny を受ける前に先回り ack が拒否されること（deny-first）
  - user-prompt-match で正しい prompt_id を指定して ack が通ること
  - user-prompt-match でキーワード不一致の prompt_id を指定して ack が拒否されること
  - gate-toggle でゲートの有効/無効が切り替わること
  - セッション終了時に stop-time ゲートが正しく動作すること

---

## 7. リリース手順

- 変更内容をコミットする
- config/policy.json.example に新しい設定項目がある場合は、既存ユーザーへの影響を確認する
- テンプレートファイルを変更した場合は、deny メッセージの表示を確認する
- hooks.json の変更がある場合は、Claude Code のフック再読み込みが必要

