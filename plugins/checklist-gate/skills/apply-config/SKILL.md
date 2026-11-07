---
name: checklist-gate:apply-config
description: This skill should be used when the user asks to "ゲート設定を反映して", "ゲート設定を適用", "checklist-gate の設定を反映", or wants to convert policy-source.md to policy.json.
---

# checklist-gate: apply-config

## やること

1. SessionStart で注入されたパス情報から `policy-source.md` のパスを確認し、Read で読み込む
2. `config/policy.json.example`（プラグインコードディレクトリ内）を参照スキーマとして読み込む
3. Markdown の内容を policy.json.example と同一のスキーマで JSON に変換する
4. 検証スクリプトに変換結果を stdin で渡して検証する。`<plugin_root>` は SessionStart で注入されたパス（`python3 "<plugin_root>/scripts/validate_policy.py" - <<'EOF'` の後に JSON を置き、`EOF` で閉じる）。`OK` が出るまで、表示された不正の箇所を直して再検証する
5. 現在の `policy.json` との diff を表示する
6. ユーザーの承認を得てから、Write で SessionStart で注入されたパスに書き込む（フックが、直近の依頼と内容の検証を確かめる）
7. 書き込んだ直後から有効になる旨を伝える

**重要**: `policy-source.md` と `policy.json` のパスは SessionStart の additionalContext で注入される。ハードコードされたパスではなく、注入されたパスを使用すること。

**policy.json が不正で検証の Bash が止められたとき**: そのまま手順 5 以降に進んでよい。Write のときにフックが同じ検証を行い、不正な内容は拒否される。

### policy-source.md → policy.json の変換ルール（affirm_keywords）

policy-source.md に「肯定応答キーワード」セクションがある場合、トップレベルの `affirm_keywords` に変換する。

| Markdown フィールド | policy.json フィールド | 変換ルール |
|---|---|---|
| 肯定応答キーワード | affirm_keywords | バッククォートで囲まれたカンマ区切り → 文字列の配列。例: `` `OK`, `はい`, `yes` `` → `["OK", "はい", "yes"]` |

## 必須スキーマ仕様

生成する JSON は以下のスキーマに**厳密に**従うこと。`config/policy.json.example`（プラグインコードディレクトリ内）が正規のリファレンスである。

- `gates` は**配列**（オブジェクトではない）。各要素に `name` フィールドを含む
- `trigger.patterns`（`pattern` ではない）: `Tool(パターン)` 形式の文字列の配列。Claude Code の permissions と同じ記法
- `trigger.except_patterns`（`exclude_pattern` ではない）: `Tool(パターン)` 形式の文字列の配列。除外対象を指定

検証の規則は検証スクリプトが持つ。この文書には列挙しない。

### policy-source.md → policy.json の変換ルール（user-prompt-match）

| Markdown フィールド | policy.json フィールド | 変換ルール |
|---|---|---|
| 種類 | type | `user-prompt-match（ユーザー発話検証）` → `"user-prompt-match"` |
| 最低 reason 文字数 | min_reason_length | 数値に変換 |
| マッチキーワード | match_keywords | バッククォートで囲まれたカンマ区切り → 文字列の配列。例: `` `push`, `プッシュ` `` → `["push", "プッシュ"]` |
| 除外キーワード | except_keywords | 同上。省略時はキー自体を出力しない（既定値が補われる） |
| 有効発話数 | max_prompt_distance | 数値に変換。省略時はキー自体を出力しない（既定値が補われる） |
| 自動通過の前提ゲート | auto_authorize_after_gate | ゲート名の文字列。省略時はキー自体を出力しない |
| ヒント | hint | そのまま文字列として出力 |

### policy-source.md → policy.json の変換ルール（天の声ゲート）

ゲート定義に「要求 ack: なし」と「メッセージ」が含まれる場合、`require: []` ＋ `message` フィールドを持つ天の声ゲートとして変換する。

| Markdown フィールド | policy.json フィールド | 変換ルール |
|---|---|---|
| 要求 ack | require | `なし` → `[]` |
| メッセージ | message | そのまま文字列として出力（省略時は `message` キー自体を出力しない） |

`require: []` かつ `message` がある場合、そのゲートは天の声ゲート（nudge gate）として動作する。

**重要**: 不明な点がある場合は `config/policy.json.example`（プラグインコードディレクトリ内）を Read して正確なスキーマを確認すること。
