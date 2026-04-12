---
name: checklist-gate:gate-on
description: This skill should be used when the user asks to "ゲートを ON にして", "ゲートを有効にして", "gate-on", or wants to enable a checklist-gate at runtime.
---

# checklist-gate: gate-on

## やること

1. 現在 OFF になっているゲートの一覧を表示する (`gate-toggle list`)
2. ユーザーが指定したゲート名で `gate-toggle on <name>` を実行する
3. 結果を報告する
