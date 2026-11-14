# checklist-gate

Claude Code の操作に「確認ゲート」を設けるプラグイン。git commit / git push / ファイル編集などの重要な操作の前に、明示的な確認（ack）を要求することで、意図しない操作を防止します。

## 特徴

- **操作前の確認を強制** -- git commit、git push、ファイル編集など、指定した操作の前に ack（確認宣言）を必須化
- **柔軟なゲート設定** -- gate トリガーで対象操作を定義可能。patterns / except_patterns は `Tool(パターン)` 形式で記述（Claude Code の permissions と同じ記法）
- **Markdown ベースの設定管理** -- 設定書（policy-source.md）を編集し、skill で JSON に変換・反映
- **セッション単位の状態管理** -- ack の有効範囲をセッション単位（1回限り）または消費型（毎回必要）で制御
- **ランタイムでのゲート切替** -- `gate-on` / `gate-off` skill でセッション中にゲートの ON/OFF を切替可能
- **絶対に止めるゲート** -- sudo のように、ack でもトグルでも外せないゲートを定義できる
- **止められた操作では緩まない** -- 許可ダイアログで拒否した gate-ack や gate-toggle off は効かない。ack の記録などは、操作が実行された後に PostToolUse フックが確定する（プラグインの更新後は Claude Code の再起動が必要）
- **異常時は止める側に倒す** -- policy.json が不正、またはフックの内部でエラーが起きたとき、ack を要求するゲートが関わる操作は止まる
- **シェルの解釈** -- `bash -c`・`eval`・`$(…)` などの中のコマンドも照合し、引用符の中の文字列には反応しない
- **設定ファイルの保護** -- policy.json への書き込みは、直近のユーザー発話に「設定」「反映」などの語があるときだけ通る（apply-config の依頼を想定した語の照合）

## インストール

Claude Code 内で以下を実行:

```
/plugin marketplace add hirokidoi/claude-plugins
/plugin install checklist-gate@doi
```

インストール後、Claude Code を再起動してください。再起動後に skills と hooks が有効になります。

初回セットアップとして `checklist-gate:edit-config` → `checklist-gate:apply-config` skill で `policy.json` を生成してください。詳細は [はじめに](docs/getting-started.md) を参照。

## ドキュメント

- [はじめに（インストールとセットアップ）](docs/getting-started.md)
- [設定リファレンス](docs/configuration.md)
- [運用ガイド（設定編集・ランタイムトグル）](docs/operations.md)
- [シナリオ集・ack の書き方ガイド](docs/scenarios.md)
- [監査クエリと FAQ](docs/audit-and-troubleshooting.md)

## ライセンス

MIT
