# 運用ガイド -- 設定編集とランタイムトグル

## 設定ファイルの編集フロー

checklist-gate の設定は **Markdown 形式の設定書**（`$CLAUDE_PLUGIN_DATA/policy-source.md`）で管理し、skill を使って JSON に変換・反映する。`policy.json` の直接編集は推奨しない（`policy-source.md` との整合性が崩れるため）。

### 手順

1. **checklist-gate:edit-config skill を起動する**
   - 「設定ファイルを開いて」「checklist-gate の設定編集」などと伝える
   - skill が `$CLAUDE_PLUGIN_DATA/policy-source.md` を開き、現在の ack 項目数・ゲート数を表示する
2. **policy-source.md を編集する**
   - ack 項目の追加・変更、ゲートの追加・変更・削除などを Markdown 上で行う
   - `policy-source.md` は直接 Edit で編集してよい
3. **checklist-gate:apply-config skill で JSON に変換・反映する**
   - 「設定を反映して」「変換して反映して」などと伝える
   - skill が以下を自動で実行する:
     - Markdown から JSON 構造への変換
     - スキーマ検証（ack 名の整合性、トリガー種別の妥当性、必須キーの確認など）
     - 既存 `policy.json` との diff 表示
     - ユーザー承認後に `$CLAUDE_PLUGIN_DATA/policy.json` を上書き
4. **次回の SessionStart から新しい設定が有効になる**

### 注意事項

- `policy.json` を直接編集した場合、`policy-source.md` との整合性が崩れる
- JSON の書式エラーがあると hook が動作しなくなるため、必ず checklist-gate:apply-config skill 経由で反映する

---

## ランタイムトグル

セッション中に特定のゲートを一時的に ON/OFF できる。

### コマンド一覧

| 操作 | skill / コマンド | 説明 |
|------|-----------------|------|
| ゲートを無効にする | `checklist-gate:gate-off` skill または `gate-toggle off <ゲート名>` | 指定ゲートを OFF にする |
| ゲートを有効にする | `checklist-gate:gate-on` skill または `gate-toggle on <ゲート名>` | 指定ゲートを ON に戻す |
| 現在の状態を確認する | `gate-toggle list` | 全ゲートの ON/OFF 状態を一覧表示する |

### スコープ

- トグルの有効範囲は **セッション単位**
- セッションが終了すると、トグルの変更は自動的に消失し、次回セッションでは `policy.json` のデフォルト状態に戻る
- トグルは `policy.json` のファイル自体を変更しない（ランタイムのみの一時的な上書き）

### gate-toggle list の出力例

```
[gate-toggle] Gate states for session abc123:
  task_start: ON (default)
  git_commit_gate: OFF (toggled)
  git_push_gate: ON (default)
```

- `(default)` -- policy.json の設定どおり
- `(toggled)` -- セッション中に手動で切り替えた状態

---

## よくある操作例

### ゲートを一時的に無効にして作業する

1. 「gate-off」と伝える（または「ゲートを OFF にして」）
2. 無効にしたいゲート名を指定する
3. 作業を行う
4. 作業後、必要に応じて「gate-on」で戻す（戻さなくてもセッション終了時に自動復帰する）

### 新しい ack 項目を追加する

1. 「設定ファイルを開いて」（checklist-gate:edit-config skill）
2. `policy-source.md` に新しい ack 項目を記述する
3. 「設定を反映して」（checklist-gate:apply-config skill）
4. diff を確認して承認する
5. 新しいセッションを開始して反映を確認する

### 現在の設定を確認する

- `gate-ack --help-gates` を実行すると、ack 項目とゲートの一覧が表示される
- `gate-toggle list` を実行すると、セッション中のトグル状態が表示される
