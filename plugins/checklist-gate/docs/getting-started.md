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

### task_start

- **目的**: Edit, Write, Read, Glob, Grep のツール呼び出しを、`docs_checked` の ack 宣言までブロックする。ただし README.md / DEVELOPMENT.md に対する Read / Glob は例外として通す
- **トリガー**: gate（patterns: `Edit(*)`, `Write(*)`, `Read(*)`, `Glob(*)`, `Grep(*)`）
- **要求する ack**: `docs_checked`
- **動作**: プロジェクトのドキュメント（README.md / DEVELOPMENT.md）を確認した旨を宣言するまで、対象ツールの呼び出しがブロックされる。ドキュメント自体の読み取りは except_patterns により ack 前でも実行可能

### git_commit_gate

- **目的**: git commit の前に、コミット準備が整っていることを要求する
- **トリガー**: gate（patterns: `Bash(git commit *)`）
- **要求する ack**: `commit_ready`
- **動作**: テスト実行や変更内容確認などの根拠を宣言するまで、git commit がブロックされる。consumable 型のため、コミットのたびに ack が必要

### git_push_gate

- **目的**: git push の前に、ユーザーからの明示的な push 指示があることを要求する
- **トリガー**: gate（patterns: `Bash(git push *)`、except_patterns: `Bash(git push --dry-run *)`）
- **要求する ack**: `user_authorized_push`（user-prompt-match 型）
- **動作**: ユーザーの発話原文をフックが機械的に検証する。push のたびに ack が必要
  - ユーザーが発話すると `[checklist-gate: prompt_id=NN]` という通知がエージェントに送られる
  - ack 時には `--prompt-id` でこの ID を指定する必要がある
  - フックが発話原文にキーワード（`push`, `プッシュ`）が含まれるかを自動検証する
  - ユーザー発話が「OK」「はい」などの肯定応答の場合は `--affirm` オプションを使用する

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

セッション中にゲートを一時的に無効化・有効化するには `gate-toggle` を使用します。

```
gate-toggle off <ゲート名>    # ゲートを無効化
gate-toggle on <ゲート名>     # ゲートを有効化
gate-toggle list             # 全ゲートの現在の状態を表示
```

この切替はセッション単位で有効であり、次回セッションでは policy.json の設定に戻ります。
