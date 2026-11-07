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
     - 検証スクリプト（`scripts/validate_policy.py`）による検証。不正があれば直して再検証する
     - 現在の `policy.json` との diff 表示
     - ユーザー承認後に `$CLAUDE_PLUGIN_DATA/policy.json` を上書き
4. **書き込んだ直後から新しい設定が有効になる**（PreToolUse のたびに読み直される）

### policy.json の保護と不正時の挙動

- policy.json への Edit / Write は、フックが監視する。直近のユーザー発話に「設定」「反映」「apply-config」などの語が否定なしで含まれるときだけ通り、それ以外は止まる。語の照合なので、「設定を確認して」のような発話の後でも通る。Write は内容も検証され、不正な内容は書けない。トグルでは外せない
- policy.json が不正なとき、読み取り専用のツールと、上の条件を満たす policy.json の書き込み以外は止まる。deny のメッセージとセッション開始の知らせに、不正の箇所が表示される。apply-config で直すか、policy-source.md を直して反映する
- policy.json が無いときはゲートが開いたままで、セッション開始時に未作成の知らせが出る

### 注意事項

- `policy.json` を直接編集した場合、`policy-source.md` との整合性が崩れる
- policy.json は必ず checklist-gate:apply-config skill 経由で反映する。ユーザーが手で編集する場合は、検証スクリプトで確かめてから置く
- Bash 経由の書き換え（リダイレクトや `sed -i` など）はフックが検知できない。範囲外として扱う

---

## ランタイムトグル

セッション中に特定のゲートを一時的に ON/OFF できる。

### コマンド一覧

| 操作 | skill / コマンド | 説明 |
|------|-----------------|------|
| ゲートを無効にする | `checklist-gate:gate-off` skill または `gate-toggle off <ゲート名> --prompt-id <id>` | 指定ゲートを OFF にする |
| ゲートを有効にする | `checklist-gate:gate-on` skill または `gate-toggle on <ゲート名>` | 指定ゲートを ON に戻す |
| 現在の状態を確認する | `gate-toggle list` | 全ゲートの ON/OFF 状態を一覧表示する |

### OFF にできる条件

OFF は、ユーザーが外すよう頼んだときだけ受け付ける。Claude が自分の判断でゲートを外すことはできない。

- `--prompt-id` で、外すよう頼んだユーザー発話を指定する
- その発話が直近の数件以内にあり、「off」「オフ」「外して」「無効」などの語と、ゲート名か「ゲート」「gate」という語の両方を含み、否定されていない（「off にしないで」など）ときだけ OFF になる。例: 「git_push_gate を外して」「push のゲートを OFF にして」「gate-off」（skill 名は両方の語として扱う）
- ON に戻すのに条件はない。ただし policy.json で `enabled: false` のゲートは、トグルでは ON にできない（拒否される）。有効にするには policy-source.md を直して apply-config で反映する

### スコープ

- トグルは session_id ごとに DB に残る。同じセッションの再開では持ち越される
- `/clear` や新しいセッションでは持ち越されず、`policy.json` の状態から始まる
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
4. 作業後、必要に応じて「gate-on」で戻す（戻さなくても、`/clear` や新しいセッションでは policy.json の状態から始まる）

### 新しい ack 項目を追加する

1. 「設定ファイルを開いて」（checklist-gate:edit-config skill）
2. `policy-source.md` に新しい ack 項目を記述する
3. 「設定を反映して」（checklist-gate:apply-config skill）
4. diff を確認して承認する
5. `gate-ack --help-gates` で反映を確認する

### 現在の設定を確認する

- `gate-ack --help-gates` を実行すると、ack 項目とゲートの一覧が表示される
- `gate-toggle list` を実行すると、セッション中のトグル状態が表示される
