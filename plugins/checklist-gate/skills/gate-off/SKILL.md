---
name: checklist-gate:gate-off
description: This skill should be used when the user asks to "ゲートを OFF にして", "ゲートを無効にして", "gate-off", or wants to disable a checklist-gate at runtime.
---

# checklist-gate: gate-off

## やること

1. 現在のゲート一覧と ON/OFF 状態を表示する (`gate-toggle list`)
2. ユーザーが指定したゲート名で `gate-toggle off <name>` を実行する
3. 結果を報告する
4. セッション終了時に自動的に元に戻る旨を伝える
