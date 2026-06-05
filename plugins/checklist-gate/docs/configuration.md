# 設定リファレンス

## policy.json の構造

policy.json は以下のセクションで構成されます。

- **affirm_keywords** -- 肯定応答の判定キーワード（トップレベル設定）
- **ack_items** -- 確認（ack）項目の定義
- **gates** -- ゲートの定義（どの操作の前に、どの ack を要求するか）
- **tenno_koe** -- 天の声（会話モニタリング）のビルトイン設定（省略可）

```json
{
  "affirm_keywords": ["OK", "はい", "よい", "いいよ", "お願い", "yes", "sure", "うん", "おけ", "いいね"],
  "ack_items": { ... },
  "gates": [ ... ],
  "tenno_koe": { ... }
}
```

---

## affirm_keywords（トップレベル設定）

`--affirm` オプション使用時に、ユーザー発話が肯定応答であるかを判定するためのキーワード一覧。プラグイン全体の共通設定。

- OR 条件: いずれか1つが発話に含まれれば肯定応答と判定
- 比較は空白除去・小文字変換後に行う
- `--affirm` を使用した gate-ack の際、ユーザー発話原文がこのキーワードのいずれかに該当しなければ `--affirm` の使用が拒否される

```json
"affirm_keywords": ["OK", "はい", "よい", "いいよ", "お願い", "yes", "sure", "うん", "おけ", "いいね"]
```

---

## ack 項目の設定

各 ack 項目は `ack_items` オブジェクトのキーとして定義します。

```json
"ack_items": {
  "項目名": {
    "type": "session / consumable / user-prompt-match",
    "min_reason_length": 20,
    "hint": "Claude に表示されるヒントメッセージ"
  }
}
```

### 共通フィールド

| フィールド | 説明 |
|---|---|
| type | `session`: セッション中1回の宣言で有効。`consumable`: 操作のたびに宣言が必要（使い切り型）。`user-prompt-match`: ユーザー発話を機械的に検証する ack（使い切り型） |
| min_reason_length | reason に必要な最低文字数（整数） |
| hint | ゲートでブロックされた際に Claude に表示されるガイドメッセージ |

### type の違い

- **session** -- 一度 ack を宣言すると、そのセッション中は再宣言不要。例: ドキュメント確認（`docs_checked`）
- **consumable** -- 操作のたびに ack が消費される。例: コミット準備（`commit_ready`）
- **user-prompt-match** -- エージェントの自己申告ではなく、ユーザーの発話原文を機械的に検証する ack。`--prompt-id` で根拠となる発話 ID を指定する。consumable と同様に操作のたびに消費される。例: push 許可（`user_authorized_push`）

### user-prompt-match 固有フィールド

user-prompt-match タイプの ack 項目には、共通フィールドに加えて以下のフィールドを指定します。

| フィールド | 説明 |
|---|---|
| match_keywords | 発話に含まれるべきキーワードの配列（OR 条件: いずれか1つが含まれれば match） |
| except_keywords | match 後に除外するキーワードの配列（OR 条件: いずれか1つが含まれれば除外）。省略時は空配列扱い |
| max_prompt_distance | gate-ack 時点から遡って直近 N 件の発話のみ有効。デフォルト: 3 |

`policy-source.md`（Markdown 形式）でゲートを定義する場合、上記フィールドはそれぞれ「マッチキーワード」「除外キーワード」「有効発話数」と記述します。apply-config skill が JSON フィールド名に自動変換します。

### --affirm オプション（肯定応答フロー）

ユーザー発話が「OK」「はい」などキーワードを含まない肯定応答の場合、`--affirm` オプションを使用します。

```
gate-ack <item> --prompt-id <id> --affirm "<AI の質問の引用>" --reason "<justification>"
```

- `--affirm` にはAIがユーザーに発した質問の引用テキストを指定する
- キーワードチェック（match_keywords / except_keywords）は `--affirm` の値に対して行われる
- `--affirm` 使用時は以下の追加検証が行われる:
  1. ユーザー発話原文が affirm_keywords のいずれかに該当するか（該当しなければ拒否）
  2. `--affirm` の値に match_keywords のいずれかが含まれるか（含まれなければ拒否）
  3. `--affirm` の値に except_keywords のいずれかが含まれていないか（含まれていれば拒否）

### deny-first（全 ack タイプ共通）

全ての ack タイプ（session / consumable / user-prompt-match）において、gate-ack は**対応するゲートの deny が発生した後にのみ受け付けます**。deny が発生する前に先回りで ack を宣言しても拒否されます。これにより、エージェントが deny メッセージを読んで考え直す機会が構造的に保証されます。

---

## ゲートの設定

各ゲートは `gates` 配列の要素として定義します。

```json
{
  "name": "ゲート名",
  "description": "説明",
  "trigger": { ... },
  "require": ["要求する ack 項目名"],
  "enabled": true
}
```

### 共通フィールド

| フィールド | 説明 |
|---|---|
| name | ゲートの識別名 |
| description | ゲートの説明（英語推奨） |
| trigger | トリガー条件の定義（後述） |
| require | このゲートが要求する ack 項目名の配列。`ack_items` に存在する名前を指定。複数指定した場合、**全ての ack が揃わないとゲートを通過できない**（ToDo リスト的なチェックリストとして機能する）。session 型と consumable 型の混在も可能 |
| enabled | `true` で有効、`false` で無効 |

---

## trigger の設定

ツール呼び出しや Bash コマンド実行をブロックするパターンを指定します。

```json
"trigger": {
  "patterns": ["Edit(*)", "Write(*)", "Read(*)", "Glob(*)", "Grep(*)"],
  "except_patterns": ["Read(*README*)", "Read(*DEVELOPMENT*)", "Glob(*README*)", "Glob(*DEVELOPMENT*)"]
}
```

| フィールド | 説明 |
|---|---|
| patterns | ゲート対象の指定。`Tool(パターン)` 形式の文字列の配列 |
| except_patterns | 除外パターンの配列。一致した場合は ack 未宣言でもゲートを通過する |

#### patterns / except_patterns の記法

`Tool(パターン)` 形式で記述する。Claude Code の permissions の記法と同じ。

- `Edit(*)` -- Edit ツール全体
- `Read(*README*)` -- README を含むパスの Read
- `Bash(git push *)` -- git push コマンド
- `Bash(git push --dry-run *)` -- dry-run 付き git push

---

## 天の声（tenno_koe）

天の声は、各ターン終了時および Agent 起動前後に LLM がプロジェクトの MEMORY.md に照らして違反を検出し、必要に応じてブロックまたは通知するビルトイン機能です。

通常の `gates`（PreToolUse パターンマッチ）とは異なり、LLM による評価で動作し、`ack_items` や `gates` への記述は不要です。

### 動作するフック

| フック | タイミング | 動作 |
|---|---|---|
| Stop | 各応答終了時 | `watch_tools` を使ったターンを評価。違反あれば応答をブロックし `tenno_koe_cleared` ack を要求。ack が成立するまでブロックは継続 |
| PreToolUse（Agent） | Agent 起動前 | 手続き的違反があれば起動を deny |
| PostToolUse（Agent） | Agent 完了後 | 手続き的ルール準拠を確認し additionalContext に結果を注入 |

### policy.json の設定

```json
"tenno_koe": {
  "enabled": true,
  "watch_tools": ["Edit", "Write", "Bash", "NotebookEdit", "MultiEdit"],
  "model": "haiku",
  "timeout": 30,
  "min_reason_length": 20,
  "hint": "天の声の指摘に対してどう対応したかを reason に記載して。"
}
```

| フィールド | 説明 |
|---|---|
| enabled | `true` で有効、`false` で無効。省略時は `true`（デフォルト有効） |
| watch_tools | Stop フックの評価フィルタ。これらのツールを使ったターンのみ評価する |
| model | 評価に使用するモデル名（`claude --model` に渡す値） |
| timeout | 評価のタイムアウト秒数 |
| min_reason_length | `tenno_koe_cleared` ack の最低 reason 文字数 |
| hint | ブロック時に Claude に表示されるガイドメッセージ |

### ビルトイン ack 項目と gate

`tenno_koe` が有効な場合、以下の項目が自動的に追加されます。**policy.json や policy-source.md に明示的に記述する必要はありません**：

- **`tenno_koe_cleared`**（consumable ack）: Stop フックで違反を検出したとき、この ack を宣言するまで応答をブロックする
- **`tenno_koe` gate**: `gate-toggle list` で確認できる。`gate-toggle off tenno_koe` でセッション中の評価を一時無効化できる

### ブロックされたときの対応

Stop フックで違反を検出すると以下のメッセージが表示されます：

```
[天の声] <違反内容>

gate-ack tenno_koe_cleared --reason "<対応内容>" を実行してください。
```

`gate-ack tenno_koe_cleared` を実行するまで、応答を終了できません。deny-first が適用されるため、ブロックを受けた後にのみ ack が受け付けられます。

### policy-source.md での記述

```markdown
## 天の声（tenno_koe:会話モニタリング）

- **有効**: はい
- **監視ツール**: `Edit`, `Write`, `Bash`, `NotebookEdit`, `MultiEdit`
- **モデル**: `haiku`
- **タイムアウト（秒）**: 30
- **最低 reason 文字数**: 20
- **ヒント**: 天の声の指摘に対してどう対応したかを reason に記載して。
```

---

## 設定の編集と反映フロー

設定の編集・反映手順については [運用ガイド](operations.md) を参照してください。

### apply-config が行う検証

変換時に以下のスキーマ検証が自動的に行われます。

- `gates[].require` で指定された ack 名が `ack_items` に存在するか
- `trigger.patterns` が配列であるか
- `min_reason_length` が整数か
- `type` が有効な種別か（session / consumable / user-prompt-match）

---

## カスタムゲートの追加例

### 例: 特定ファイルの編集をブロックするゲートを追加する

policy-source.md に以下を追記します。

**ack 項目の追加:**

```
### config_edit_ready
- **種類**: consumable（毎回消費）
- **最低 reason 文字数**: 30
- **ヒント**: 設定ファイルの変更理由と影響範囲を reason に記載してください。
```

**ゲートの追加:**

```
### config_edit_gate
- **説明**: 設定ファイルの編集前に config_edit_ready を要求
- **patterns**: `Edit(config/*.json)`, `Write(config/*.json)`
- **except_patterns**: なし
- **要求 ack**: config_edit_ready
- **有効**: はい
```

追記後、Claude に「設定を反映して」と依頼すると、checklist-gate:apply-config skill が JSON に変換して policy.json を更新します。

### 例: デプロイ前の確認ゲートを追加する（複数 ack によるチェックリスト）

1つのゲートに複数の ack 項目を要求することで、ToDoリスト的なチェックリストとして機能させることができます。以下の例では、デプロイ前に「テスト完了」「ステージング確認」「ユーザー承認」の3つ全てを要求します。

policy-source.md に以下を追記します。

**ack 項目の追加:**

```
### tests_passed
- **種類**: consumable（毎回消費）
- **最低 reason 文字数**: 20
- **ヒント**: テストを実行して全件パスした？結果を reason に記載して。

### staging_verified
- **種類**: consumable（毎回消費）
- **最低 reason 文字数**: 30
- **ヒント**: ステージング環境での動作確認は完了した？確認内容を reason に記載して。

### user_authorized_deploy
- **種類**: consumable（毎回消費）
- **最低 reason 文字数**: 30
- **ヒント**: ユーザーから明示的なデプロイ指示があった？発話内容を reason に記載して。
```

**ゲートの追加:**

```
### deploy_gate
- **説明**: デプロイコマンド実行前に全チェック項目の完了を要求
- **patterns**: `Bash(deploy *)`
- **except_patterns**: `Bash(deploy --dry-run *)`
- **要求 ack**: tests_passed, staging_verified, user_authorized_deploy
- **有効**: はい
```

この設定では、`deploy` コマンドを実行しようとすると、3つの ack が全て揃うまでブロックされます。ack は任意の順序で宣言でき、全て揃った時点でゲートを通過します。

追記後、Claude に「設定を反映して」と依頼すると、checklist-gate:apply-config skill が JSON に変換して policy.json を更新します。

### 例: git push を user-prompt-match で保護するゲートを追加する

エージェントの自己申告ではなく、ユーザーの発話原文を機械的に検証して push を許可するゲートです。

**policy-source.md の記述:**

```
### user_authorized_push
- **種類**: user-prompt-match（ユーザー発話検証）
- **最低 reason 文字数**: 20
- **マッチキーワード**: `push`, `プッシュ`
- **除外キーワード**: `dry-run`, `ドライラン`
- **有効発話数**: 3
- **ヒント**: ユーザー発話に明示的な push 指示があった？「pushして」のような直接の依頼が無ければユーザーに確認して。
```

**対応する policy.json:**

```json
{
  "affirm_keywords": ["OK", "はい", "よい", "いいよ", "お願い", "yes", "sure", "うん", "おけ", "いいね"],
  "ack_items": {
    "user_authorized_push": {
      "type": "user-prompt-match",
      "min_reason_length": 20,
      "hint": "ユーザー発話に明示的な push 指示があった？「pushして」のような直接の依頼が無ければユーザーに確認して。",
      "match_keywords": ["push", "プッシュ"],
      "except_keywords": ["dry-run", "ドライラン"],
      "max_prompt_distance": 3
    }
  },
  "gates": [
    {
      "name": "git_push_gate",
      "description": "Require user_authorized_push before git push",
      "trigger": {
        "patterns": ["Bash(git push *)"],
        "except_patterns": ["Bash(git push --dry-run *)"]
      },
      "require": ["user_authorized_push"],
      "enabled": true
    }
  ]
}
```

この設定により:
- ユーザーが「push して」→ match_keywords に "push" が含まれる → match
- ユーザーが「dry-run で push して」→ match するが except_keywords に "dry-run" が含まれる → 除外（ack 不可）
- ユーザーが「テスト通ったのでマージしよう」→ match_keywords に該当なし → ack 不可
- AI が「push しますか？」→ ユーザーが「OK」→ affirm_keywords に該当 → `--affirm "push しますか？"` で通過

