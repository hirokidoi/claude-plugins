# checklist-gate 設定書

この Markdown ファイルを編集し、Claude に「設定を反映して」と依頼すると
`checklist-gate:apply-config` skill が JSON に変換して `$CLAUDE_PLUGIN_DATA/policy.json` を更新します。

---

## 肯定応答キーワード

`OK`, `はい`, `よい`, `いいよ`, `お願い`, `yes`, `sure`, `うん`, `おけ`, `いいね`

---

## ack 項目

### docs_checked
- **種類**: session（セッション中1回のみ）
- **最低 reason 文字数**: 20
- **ヒント**: README.md や DEVELOPMENT.md を Read で全文読んだ？存在しない場合は Glob で探したが無かったことを reason に記載して。

### commit_ready
- **種類**: consumable（毎回消費）
- **最低 reason 文字数**: 20
- **ヒント**: テスト実行した？変更内容を確認した？コミットOKな根拠を reason に記載して。

### user_authorized_push
- **種類**: user-prompt-match（ユーザー発話検証）
- **最低 reason 文字数**: 20
- **マッチキーワード**: `push`, `プッシュ`
- **除外キーワード**: `dry-run`, `ドライラン`, `pushはまだ`, `pushはしないで`
- **有効発話数**: 3
- **ヒント**: 直近のユーザー発話に明示的な push 指示があった？「pushして」のような直接の依頼が無ければユーザーに確認して。

---

## ゲート

### task_start
- **説明**: ファイル編集前に docs_checked を要求
- **トリガー種別**: gate
- **patterns**: `Edit(*)`, `Write(*)`, `Read(*)`, `Glob(*)`, `Grep(*)`
- **except_patterns**: `Read(*README*)`, `Read(*DEVELOPMENT*)`, `Glob(*README*)`, `Glob(*DEVELOPMENT*)`
- **要求 ack**: docs_checked
- **有効**: はい

### git_commit_gate
- **説明**: git commit 前に commit_ready を要求
- **トリガー種別**: gate
- **patterns**: `Bash(git commit *)`
- **except_patterns**: なし
- **要求 ack**: commit_ready
- **有効**: はい

### git_push_gate
- **説明**: git push 前に user_authorized_push を要求
- **トリガー種別**: gate
- **patterns**: `Bash(git push *)`
- **except_patterns**: `Bash(git push --dry-run *)`
- **要求 ack**: user_authorized_push
- **有効**: はい
