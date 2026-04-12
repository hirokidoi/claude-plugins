---
name: checklist-gate:edit-config
description: This skill should be used when the user asks to "ゲートの設定ファイルを開いて", "checklist-gate の設定編集", "ゲートの設定を変更", or wants to edit checklist-gate configuration.
---

# checklist-gate: edit-config

## やること

1. SessionStart で注入されたパス情報から `policy-source.md` のパスを確認し、Read で開く
2. SessionStart で注入されたパス情報から `policy.json` のパスを確認し、読み込んで ack 項目数とゲート数を表示する
3. ユーザーに編集内容を確認する

## 注意

- `policy.json` を直接編集しないこと
- 編集後は `checklist-gate:apply-config` skill で JSON に変換して反映する
- `policy-source.md` と `policy.json` のパスは SessionStart の additionalContext で注入される。ハードコードされたパスではなく、注入されたパスを使用すること
