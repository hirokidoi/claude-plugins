# 設定リファレンス

## policy.json の構造

policy.json は以下のセクションで構成されます。

- **affirm_keywords** -- 肯定応答の判定キーワード（トップレベル設定）
- **ack_items** -- 確認（ack）項目の定義
- **gates** -- ゲートの定義（どの操作の前に、どの ack を要求するか）

```json
{
  "affirm_keywords": ["OK", "はい", "よい", "いいよ", "お願い", "yes", "sure", "うん", "おけ", "いいね"],
  "ack_items": { ... },
  "gates": [ ... ]
}
```

---

## affirm_keywords（トップレベル設定）

`--affirm` オプション使用時に、ユーザー発話が肯定応答であるかを判定するためのキーワード一覧。プラグイン全体の共通設定。

- OR 条件: いずれか1つが発話に含まれれば肯定応答と判定
- match_keywords と同じく単語として照合する（`look` は `OK` に一致しない）
- 省略したときは `--affirm` が使えない（拒否される）。空の配列は不正（検証で弾かれる）
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
| min_reason_length | reason に必要な最低文字数（0 以上の整数） |
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
| auto_authorize_after_gate | 自動通過の前提となるゲート名（後述）。省略時は自動通過しない |

`policy-source.md`（Markdown 形式）でゲートを定義する場合、上記フィールドはそれぞれ「マッチキーワード」「除外キーワード」「有効発話数」「自動通過の前提ゲート」と記述します。apply-config skill が JSON フィールド名に自動変換します。

### 発話の照合ルール

- 照合するのはユーザーが自分で書いた部分だけ。IDE が付ける情報（開いているファイル・選択範囲）、貼り付けたテキスト、システムの通知は除いてから照合する
- システムの通知だけの発話（バックグラウンドタスクの完了通知など）は発話として記録しない。max_prompt_distance の数にも入らない
- 英数字で始まる・終わる match_keywords は単語として照合する。`push` は「push して」「pushして」「git push」には一致し、`web_push` や `push_subscription` には一致しない
- except_keywords は空白を除去・小文字変換したうえで、部分一致で照合する
- キーワードが否定されている発話は許可にならない。日本語はキーワードの直後（「push しないで」「push は待って」「push は不要」など）、英語は前後（"don't push"、"no need to push"、"push is not needed" など）にある否定の語を見る
- 1 つの発話を根拠にできるのは 1 回まで。同じ発話でもう一度 ack しようとすると拒否される
- 未消費の ack が残っている項目には、追加の ack を宣言できない（ack を積み増して、1 回の許可で何度も通すことを防ぐ）。発話が max_prompt_distance の外に出た未消費の ack は失効する

### 自動通過（auto_authorize_after_gate）

ユーザーの指示どおりに作業が流れている場面では、gate-ack なしでゲートを通過させます。例: 「commit → push して」と依頼され、commit のゲートを通過した後の push。

ゲートで不足していると判定された時点で、次の条件をすべて満たせば、ack を自動で記録して通過する。

- 直近 max_prompt_distance 件の発話のうち、match_keywords を含む最新の発話が、except_keywords を含まず、否定もされていない。新しい発話で「push はやめて」のように否定すると、それより前の許可も無効になる
- その発話のターン以降に、auto_authorize_after_gate のゲートを通過している（同じコマンドの中で通過する場合を含む）。別のコマンドで通過した場合は、その呼び出しが止められずにエラーなく終わったものだけを数える（許可ダイアログで拒否した commit の呼び出しや、エラーで終わった commit の呼び出しは数えない。`git commit …; echo done` のように、commit が失敗しても呼び出しがエラーなく終わる形は数える）
- その発話を根拠にした ack が、まだ記録されていない（1 つの発話で自動通過できるのは 1 回まで）

自動通過したときは、根拠にした prompt_id を Claude に通知する。条件を満たさないときは通常どおり deny になり、gate-ack で通過できる。`git commit … && git push` のように 1 回のコマンドで実行した場合は、gates 配列で前提ゲートが先に並んでいれば同じ判定になる。

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
  3. `--affirm` の値に except_keywords のいずれかが含まれていないか、キーワードが否定されていないか（どちらかに当たれば拒否）

### deny-first（全 ack タイプ共通）

全ての ack タイプ（session / consumable / user-prompt-match）において、gate-ack は**対応するゲートの deny が発生した後にのみ受け付けます**。deny が発生する前に先回りで ack を宣言しても拒否されます。これにより、エージェントが deny メッセージを読んで考え直す機会が構造的に保証されます。自動通過はエージェントの宣言ではなくフックが記録する ack なので、この制約の対象外です。

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
| name | ゲートの識別名（必須。ゲート間で重複不可） |
| description | ゲートの説明（英語推奨） |
| trigger | トリガー条件の定義（後述） |
| require | このゲートが要求する ack 項目名の配列。複数指定した場合、**全ての ack が揃わないとゲートを通過できない**。block でないゲートで `[]`（空配列）にすると天の声ゲートになる（後述） |
| message | 天の声ゲートでは、トリガーに一致したターン最初のツール使用時に Claude へ additionalContext として注入する文字列。絶対に止めるゲートでは deny の理由。ack を要求するゲートでは使わない |
| block | `true` にすると絶対に止めるゲートになる（後述）。省略時は `false` |
| enabled | `true` で有効、`false` で無効 |

### 天の声ゲート（nudge gate）

`require: []` かつ `message` を指定し、block でないゲートは**天の声ゲート**として動作します。deny せず、そのターン最初のトリガー一致時に `message` を additionalContext として Claude に注入します。

```json
{
  "name": "agent_nudge",
  "description": "Agent 起動前のルールリマインダー",
  "trigger": { "patterns": ["Agent(*)"], "except_patterns": [] },
  "require": [],
  "message": "[天の声] CLAUDE.md のルール確認した？",
  "enabled": true
}
```

- LLM 呼び出しなし・ブロックなし・ack 不要で摩擦ゼロ
- 「そのターン最初の1回だけ」注入するため、同一ターン内に Agent を複数回起動しても重複しない

### 絶対に止めるゲート（block gate）

`"block": true` と `message` を指定したゲートは、トリガーに一致した操作を**無条件に止めます**。`message` が deny の理由として Claude に表示されます。

```json
{
  "name": "sudo_block",
  "description": "sudo を使うコマンドを絶対に止める",
  "trigger": { "patterns": ["Bash(sudo *)"], "except_patterns": [] },
  "block": true,
  "message": "sudo は使えません。実行が必要なら、コマンドをユーザーに示して実行を頼んでください。",
  "enabled": true
}
```

- gate-ack では通らない。`require` は持てず、`message` は必須（どちらも検証で弾かれる）
- gate-toggle off では外せない（拒否される）。止めるのをやめるには、policy-source.md で無効にするか消して、apply-config で反映する
- DB を使わずに判定するので、DB に異常があっても止まる
- 照合は他のゲートと同じ。解析できないコマンドはすべてのパターンに当たるので、このゲートにも止められる。「シェルの解釈の限界」に挙げた形は、このゲートでも止められない
- policy-source.md では「**種類**: 絶対に止める」と書き、「要求 ack」は書かない（同梱の設定書の sudo_block を参照）

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
| patterns | ゲート対象の指定（必須・空不可）。`Tool(パターン)` 形式の文字列の配列 |
| except_patterns | 除外パターンの配列（省略可）。一致した場合は ack 未宣言でもゲートを通過する。Bash では単純コマンドごとに判定し、除外に当たらないコマンドが 1 つでも patterns に一致すればゲートがかかる |

#### patterns / except_patterns の記法

`Tool(パターン)` 形式で記述する。Claude Code の permissions の記法と同じ。

- `Edit(*)` -- Edit ツール全体
- `Read(*README*)` -- README を含むパスの Read
- `Bash(git push *)` -- git push コマンド
- `Bash(git push --dry-run *)` -- dry-run 付き git push

**照合の規則（全ツール共通）**

- 大文字小文字を区別しない（除外パターンも同じ）
- ワイルドカードは `*` だけ。`[ ]` や `?` は文字そのものとして扱う（`Edit(app/[id]/x.ts)` は実在のパスに一致する）
- パスは `..` などを正規化してから照合する（`Edit(*/tmp/*)` の除外は `/repo/tmp/../src/a.py` に当たらない）
- ツール名にも `*` を書ける（`mcp__*(*)` は MCP のツール全体）。`Tool(*)` や括弧なしの `Tool` は値を見ずに一致する
- 末尾が ` *` のパターンは、引数なしにも一致する（`Bash(git push *)` は `git push` にも一致する）
- Monitor は Bash のパターンで、NotebookEdit は Edit のパターン（対象は `notebook_path`）でも照合する。`Monitor(...)`・`NotebookEdit(...)` と直接書くこともできる。Edit のパターンは Write には効かない
- パターンの前後に空白があるもの、括弧の対応が取れないものは不正（検証で弾かれる）

**Bash（と Monitor）の照合**

コマンド行を単純コマンドに分け、それぞれを patterns・except_patterns と照合する。

- `&&`・`||`・`;`・`|`・`&`・改行で区切った各コマンド、`(…)`・`{ …; }` の中、`$(…)`・バッククォート・`<(…)` の中のコマンド、`if` や `for` の本体を対象にする。パイプの後ろも照合する
- `bash -c`・`sh -c`・`eval`・`env`・`command`・`time`・`nohup`・`sudo`・`timeout`・`xargs` などの包みは開き、中のコマンドも照合する。包みのオプションが値を取るか分からないときは、取る場合と取らない場合の両方で照合する
- 包みや置換が深く重なって読み切れないコマンドは、そのツールのすべてのパターンに当たるものとして扱う（除外にも当たらない）
- 先頭の環境変数の代入（`FOO=1`）は読み飛ばし、コマンドのパス（`/usr/bin/git`）と大文字小文字（`Git`）はコマンド名として正規化する
- git のグローバルオプション（`-C <dir>`・`-c <key=value>`・`--no-pager` など）は読み飛ばす。`git -C repo push` は `Bash(git push *)` に一致する。`git -c alias.名前=…` で定義したエイリアスは展開する
- 引用符の中の文字列には反応しない。`echo "x; git push y"` や `grep "git push" README.md` は `Bash(git push *)` に一致しない。二重引用符の中の `$(git push)` は実行されるものとして照合する
- ヒアドキュメントの本文は、区切り語に引用符が無いときの `$(…)`・バッククォートの中だけを照合し、本文の地の文は照合しない
- コマンド名から始まるパターンは、引数の中の文字列には反応しない。`git stash push` や `git diff docs/web_push.md` は `Bash(git push *)` に一致しない。`*` で始まるパターン（`Bash(*README*)` など）は引数にも一致する
- 引用符の閉じ忘れや、置換の中の `case` のように解析できない入力は、読み切れないコマンドと同じく、そのツールのすべてのパターンに当たるものとして扱う（除外にも当たらない）。通常のコマンドやコミットメッセージは解析できるので、この扱いにはならない

除外の判定は単純コマンドごとに行う。`cat README.md | head` では `cat README.md` が除外に当たっても、`head` が除外に当たらなければゲートがかかる。

### シェルの解釈の限界

次のものは解釈できず、ゲートを回避できる。ゲートは誤操作を防ぐためのもので、悪意ある回避を防ぐものではない。

- 変数やコマンド置換の展開結果で組み立てたコマンド（`git $a`）
- git の設定ファイルで定義したエイリアス
- 別プロセス・別言語・ファイルを経由する実行（`python3 -c`・`ssh`・`sh script.sh`・`source`）
- 包みとして扱っていないコマンドの引数として渡す実行（`find -exec` など）
- シェルの展開で組み立てたコマンド名（ブレース展開 `{git,push}`、コマンド名のグロブ `gi?`）
- シェル関数・シェルのエイリアスの呼び出し
- Bash を経由した policy.json や DB の書き換え

解釈は bash の文法を基準にしている。実行するシェルが zsh などで bash と読み方が分かれる箇所は、ゲートがかかる側に倒している。

---

## 設定の編集と反映フロー

設定の編集・反映手順については [運用ガイド](operations.md) を参照してください。

### 検証

policy.json の検証規則は `lib/policy.py` の `validate` が正で、次の 3 か所が同じ検証を使う。

- apply-config が書き込む前に実行する検証スクリプト（`scripts/validate_policy.py <path|->`。正しければ `OK`、不正なら箇所を 1 行ずつ表示する）
- フックの読み込み時（PreToolUse のたびと、セッション開始の知らせ）
- フックによる policy.json への Write の内容確認

未知のキーも不正になる。不正な policy.json では、読み取り専用のツール以外が止まる（運用ガイドの「policy.json の保護と不正時の挙動」を参照）。省略されたフィールドの既定値も `lib/policy.py` が埋める。

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

