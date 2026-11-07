---
name: checklist-gate:gate-off
description: This skill should be used when the user asks to "ゲートを OFF にして", "ゲートを無効にして", "gate-off", or wants to disable a checklist-gate at runtime.
---

# checklist-gate: gate-off

## やること

1. 現在のゲート一覧と ON/OFF 状態を表示する (`gate-toggle list`)
2. ユーザーが指定したゲート名で `gate-toggle off <name> --prompt-id <id>` を実行する。`<id>` はゲートを外すよう頼んだユーザー発話（gate-off の依頼や「<ゲート名> を外して」など）の prompt_id（`[checklist-gate: prompt_id=NN]` で通知されたもの）。ゲート名を聞き返した場合も、`<id>` には聞き返しの答えではなく、外すよう頼んだ発話を指定する
3. 結果を報告する。拒否された場合は、外すゲートと外してよいことを、ユーザーに改めて確認する
4. 同じセッション（再開を含む）の間有効で、`/clear` や新しいセッションでは policy.json の状態に戻る旨を伝える
