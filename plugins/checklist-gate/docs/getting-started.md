# はじめに

## 前提条件

- Claude Code がインストール済みであること
- Python 3.x がインストール済みであること（プラグイン内部のスクリプトで使用）

## インストール

Claude Code 内で以下を実行する:

```
/plugin marketplace add hirokidoi/claude-plugins
/plugin install checklist-gate@doi
```

1 行目で GitHub リポジトリをマーケットプレイスとして登録し、2 行目でプラグインをインストールする。インストール後、Claude Code を再起動すると skills と hooks が有効になる。

## 初回セットアップ

インストール後、`$CLAUDE_PLUGIN_DATA/policy.json` を作成する必要があります。`config/policy.json.example` にサンプル設定が同梱されています。

初回セッション時にデフォルトの policy-source.md が自動的に `$CLAUDE_PLUGIN_DATA/` にコピーされます。

1. Claude に「設定ファイルを開いて」と依頼する（`checklist-gate:edit-config` skill が起動）
2. `$CLAUDE_PLUGIN_DATA/policy-source.md` を編集してゲートや ack 項目を定義する
3. Claude に「設定を反映して」と依頼する（`checklist-gate:apply-config` skill が `policy.json` を生成）

詳細は [運用ガイド](operations.md) を参照してください。

## セッション開始時の動作

Claude Code のセッションが開始されると、checklist-gate は自動的にコンテキスト情報を注入します。この注入により、Claude はプラグインが有効であることを認識し、ゲートの仕組みに従って動作するようになります。

注入される内容の概要:

- プラグインが有効である旨の通知
- ゲートでブロックされた場合の対処方法（`gate-ack` コマンドの使い方）
- reason（確認理由）の最低文字数と記載例
- ユーザーデータファイル（policy-source.md, policy.json）のパス情報

## デフォルトゲート

インストール直後は以下の3つのゲートが有効です。

- **task_start** -- ファイル編集を始める前に、プロジェクトのドキュメント（README.md / DEVELOPMENT.md）を確認済みであることを要求する。ドキュメント自体の読み取りはゲート前でも可能
- **git_commit_gate** -- git commit の前に、コミット準備が整っていることを要求する。コミットのたびに宣言が必要
- **git_push_gate** -- git push の前に、ユーザーからの明示的な push 指示があることを要求する。ユーザーの発話原文をフックが機械的に検証する

各ゲートの詳細な設定（patterns / except_patterns / ack タイプなど）は [設定リファレンス](configuration.md) を参照してください。

## 天の声（tenno_koe）

天の声はデフォルトで有効になっているビルトインの会話モニタリング機能です。Stop フック（Claude が各応答を終えるとき）に LLM がプロジェクトの MEMORY.md（Claude Code が自動管理するルール蓄積ファイル）に照らして違反を検出し、必要に応じて応答をブロックします。

### 動作条件

以下のいずれかのツールを使った応答の終了時に Haiku による評価が実行されます：`Edit`, `Write`, `Bash`, `NotebookEdit`, `MultiEdit`。Read / Glob / Grep のみの応答や会話のみの応答は評価をスキップします。

### ブロックされた場合

```
[天の声] <違反内容>

gate-ack tenno_koe_cleared --reason "<対応内容>" を実行してください。
hint: <policy.json の tenno_koe.hint に設定されたメッセージ>
```

`gate-ack tenno_koe_cleared` を宣言するまで応答を終了できません。

```
gate-ack tenno_koe_cleared --reason "指摘を確認。計画を列挙してからコマンドを実行するよう修正した"
```

### 一時的に無効にする

```
gate-toggle off tenno_koe
```

セッション終了時に自動的に元の状態に戻ります。

---

## gate-ack の基本的な使い方

ゲートでブロックされた場合、`gate-ack` コマンドで ack を宣言します。

```
gate-ack <項目名> --reason "<具体的な根拠>"
```

使用例:

```
gate-ack docs_checked --reason "README.md と DEVELOPMENT.md を Read で確認済み"
gate-ack commit_ready --reason "全テストが通過、変更差分を git diff で確認済み"
gate-ack user_authorized_push --prompt-id 42 --reason "ユーザー発話『push して』を受信（prompt_id=42）"
```

user-prompt-match 型の ack（`user_authorized_push`）では `--prompt-id` が必須です。ユーザー発話が「OK」などの肯定応答の場合は `--affirm` も使用します:

```
gate-ack user_authorized_push --prompt-id 43 --affirm "push しますか？" --reason "AIの質問に対しユーザーがOKと回答"
```

注意点:

- `--reason` には各 ack 項目で定められた最低文字数以上の具体的な根拠を記載する必要がある（デフォルトは20文字以上）
- `--session-id` は PreToolUse フックにより自動で付与されるため、手動での指定は不要
- **deny-first**: 全ての ack タイプで、対応するゲートの deny が発生した後にのみ ack を受け付けます。deny を受ける前に先回りで ack を宣言することはできません

### ゲートと ack 項目の一覧を確認する

```
gate-ack --help-gates
```

現在の policy.json に定義されている全ての ack 項目とゲートの情報が表示されます。

## ゲートの一時的な ON/OFF 切替

セッション中にゲートを一時的に無効化・有効化するには `gate-toggle` コマンドまたは `checklist-gate:gate-on` / `checklist-gate:gate-off` skill を使います。

```
gate-toggle off <ゲート名>    # ゲートを無効化
gate-toggle on <ゲート名>     # ゲートを有効化
gate-toggle list             # 全ゲートの現在の状態を表示
```

この切替はセッション中のみ有効です。詳細は [運用ガイド](operations.md) を参照してください。
