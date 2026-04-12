---
name: checklist-gate:apply-config
description: This skill should be used when the user asks to "ゲート設定を反映して", "ゲート設定を適用", "checklist-gate の設定を反映", or wants to convert policy-source.md to policy.json.
---

# checklist-gate: apply-config

## やること

1. SessionStart で注入されたパス情報から `policy-source.md` のパスを確認し、Read で読み込む
2. `config/policy.json.example`（プラグインコードディレクトリ内）を参照スキーマとして読み込む
3. Markdown の内容を policy.json.example と同一のスキーマで JSON に変換する
4. スキーマ検証を行う（下記「必須スキーマ仕様」参照）
5. 既存の `policy.json` との diff を表示する
6. ユーザーの承認を得てから SessionStart で注入されたパスに書き込む
7. 次回 SessionStart から有効になる旨を伝える

**重要**: `policy-source.md` と `policy.json` のパスは SessionStart の additionalContext で注入される。ハードコードされたパスではなく、注入されたパスを使用すること。

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
- `trigger.check`: stop-time 用の組み込みチェッカー名（文字列）

スキーマ検証項目:
- `gates[].require` の ack 名が `ack_items` に存在するか
- `trigger.type` が有効な種別か（gate / stop-time）
- 種別ごとの必須キーが揃っているか
- `min_reason_length` が整数か、`type` が consumable / session か
- `affirm_keywords`（トップレベル）が文字列の配列であるか（省略可、存在する場合は空配列不可・最低1つ必要）

### user-prompt-match タイプの検証項目

user-prompt-match タイプの ack 項目には追加の必須フィールドがある。スキーマ検証で以下も確認すること:

- `match_keywords` が文字列の配列であるか（空配列は不可、最低1つ必要）
- `except_keywords` が文字列の配列であるか（省略時は空配列 `[]` として出力）
- `max_prompt_distance` が正の整数であるか（省略時はデフォルト `3` として出力）

### policy-source.md → policy.json の変換ルール（user-prompt-match）

| Markdown フィールド | policy.json フィールド | 変換ルール |
|---|---|---|
| 種類 | type | `user-prompt-match（ユーザー発話検証）` → `"user-prompt-match"` |
| 最低 reason 文字数 | min_reason_length | 数値に変換 |
| マッチキーワード | match_keywords | バッククォートで囲まれたカンマ区切り → 文字列の配列。例: `` `push`, `プッシュ` `` → `["push", "プッシュ"]` |
| 除外キーワード | except_keywords | 同上。省略時は `[]` |
| 有効発話数 | max_prompt_distance | 数値に変換。省略時は `3` |
| ヒント | hint | そのまま文字列として出力 |

**重要**: 不明な点がある場合は `config/policy.json.example`（プラグインコードディレクトリ内）を Read して正確なスキーマを確認すること。
