# DEVELOPMENT.md

checklist-gate プラグインの開発者向けアーキテクチャ説明書。

---

## 1. アーキテクチャ概要

### コンポーネント間のデータフロー

```
SessionStart hook
  |
  v
session_init.py  --->  データディレクトリ作成・policy-source.md の複製
  |               --->  State (SQLite)  --- セッションの最終使用時刻の更新 / 古いデータの掃除
  |               --->  policy.json の状態（未作成・不正）を知らせに載せる
  |
  v
[ユーザーが発話]
  |
  v
UserPromptSubmit hook
  |
  v
prompt_store.py  --->  State (SQLite)  --- ユーザー発話記録（prompt_id 発行）・最終使用時刻の更新
  |                                        additionalContext で prompt_id をエージェントに通知
  |
  v
[Claude がツールを使用]
  |
  v
PreToolUse hook
  |
  v
gate_check.py（判定の順序は「純粋な計算 → DB」）
  |
  +-- 1. stdin を読む（JSON でなければ内部エラー）
  +-- 2. gate-ack / gate-toggle: hook 内部で処理し、command をメッセージを表示するだけのコマンドに書き換えて通過
  |      （外部 CLI は呼ばない。DB の異常はエラーを表示する書き換えになる）
  +-- 3. policy.json への Edit/Write: 直近の発話に「設定」「反映」「apply-config」などの語が否定なしで無ければ deny。Write は内容も検証する
  +-- 4. policy.json を読み込んで検証する（lib/policy.py）
  |      無い: 通過 / 不正: 読み取り専用のツールと許可済みの policy.json の書き込み以外は deny
  +-- 5. 有効で trigger が当たるゲートを、DB を開かずに列挙する（lib/shell_parse.py で単純コマンドに分けて照合）
  |      無ければ通過。require を持つゲートが 1 つでもあれば「止める必要あり」とする
  +-- 6. DB を開いてゲートを評価する
  |      - 不足した user-prompt-match ack: 自動通過の根拠になる発話を探す
  |      - どれかのゲートが未充足: deny レスポンス（ack を要求するメッセージ）。gate_denies に記録（deny-first 用）。何も消費しない
  |      - 全ゲート充足: 1 つの書き込みロックの中で ack を消費し、自動通過の ack を記録して通過。
  |        消費できなかった（並列の呼び出しが先に使った）ときは deny。自動通過の前提ゲートなら gate_passes に通過したターンを記録
  +-- 7. 例外: 「止める必要あり」または不明なら deny（内部エラーと再試行の案内）。天の声ゲートだけが当たる場合は通過
  +-- 8. deny の出力自体が失敗したら、stderr に理由を書いて終了コード 2
  |
  v
[Claude が gate-ack コマンドを Bash で実行しようとする]
  |
  v
gate_check.py が検出 → deny-first チェック → 未消費の ack が無いかのチェック
  → (user-prompt-match の場合) 発話検証 → ack を記録 → command を書き換え
  |
  v
[再度ツール使用 -> PreToolUse -> gate_check.py -> 今度は通過]
```

何も出力せず終了コード 0 で終わる経路は「通過」を意味する。通す経路だけがこの形で終わり、異常の経路は必ず deny の出力か終了コード 2 で終える。

### 典型的なフロー

1. **SessionStart**: セッション開始時（再開・`/clear`・compact を含む）に session_init.py がセッションの最終使用時刻を更新し、古いデータを掃除し、policy.json の状態を知らせる
2. **UserPromptSubmit**: ユーザーの発話ごとに prompt_store.py が発話を DB に記録し、prompt_id をエージェントに通知する
3. **PreToolUse**: ツール使用時に gate_check.py がポリシーに基づいてゲート判定を行う
4. **deny**: ゲート条件未充足の場合、deny レスポンスを返却し、gate_denies テーブルに deny を記録する
5. **gate-ack**: ゲートで拒否された場合、ack コマンドで条件を宣言する。deny-first により、deny が記録されていない場合は ack が拒否される
6. **PreToolUse (再試行)**: 再度ツールを使用すると、ack 済みのため通過する
7. **nudge gate**: `require: []` かつ `message` を持つゲートはそのターン最初の一致時に additionalContext を注入して通過（deny しない）

### 責務分担

- **hooks (hooks.json)**: どのフックイベントでどのスクリプトを実行するかの宣言
- **scripts/prompt_store.py**: UserPromptSubmit hook スクリプト。ユーザーの発話を DB に記録し、additionalContext で prompt_id をエージェントに通知する。システムの通知だけの発話は記録しない
- **scripts/gate_check.py**: PreToolUse 時のゲート判定、deny/allow の決定、異常時に止める判断、policy.json の書き込みの保護、deny-first の enforce、user-prompt-match の発話検証・キーワードチェック・自動通過、gate-ack / gate-toggle コマンドの内部処理、天の声ゲート（nudge gate）の per-turn additionalContext 注入
- **scripts/validate_policy.py**: policy.json の検証 CLI。apply-config が書き込む前に使う
- **lib/policy.py**: policy.json の読み込み・検証・既定値の補完。検証規則の正。gate_check.py・session_init.py・validate_policy.py が使う
- **lib/shell_parse.py**: Bash コマンド行を単純コマンドに分ける字句解析器（引用符・置換・ヒアドキュメント・包みの解釈）
- **lib/prompt_text.py**: 記録された発話から、IDE・貼り付け・システムの通知のブロックを取り除き、ユーザーが書いた部分を取り出す
- **gate-ack / gate-toggle コマンド**: Claude が Bash で実行するコマンド。gate_check.py が PreToolUse フックで検出し、内部で処理する（外部バイナリは実行されない）
- **State (state.py)**: SQLite を用いたデータの永続化（DAO 層）
- **policy.json**: ゲートルール・ack 項目の宣言的定義。`require: []` ＋ `message` フィールドで天の声ゲートを定義する。書式は `lib/policy.py` が検証する

---

## 2. 主要モジュールの責務と依存関係

### ファイル一覧と責務

| ファイル | 責務 |
|---|---|
| hooks/hooks.json | フックイベント（SessionStart / UserPromptSubmit / PreToolUse）と実行するスクリプトの対応定義。`$CLAUDE_PLUGIN_ROOT` 環境変数を使って scripts/ 配下の Python スクリプトを直接呼び出す |
| scripts/session_init.py | データディレクトリの用意、セッションの最終使用時刻の更新、スキーマ初期化、古いデータの掃除、policy.json の状態の知らせ、セッションコンテキストの出力。DB の各段は個別に失敗を受け止め、出力までは必ず進む |
| scripts/prompt_store.py | UserPromptSubmit hook スクリプト。ユーザー発話を DB に記録し、additionalContext で prompt_id をエージェントに通知 |
| scripts/gate_check.py | PreToolUse 時のゲート判定、deny/allow の決定、異常時に止める判断、policy.json の書き込みの保護、deny-first の enforce、user-prompt-match の発話検証、gate-ack / gate-toggle コマンドの内部処理（ack 記録・トグル操作）、天の声ゲートの per-turn additionalContext 注入 |
| scripts/validate_policy.py | policy.json の検証 CLI（`<path>` または `-` で stdin。終了コードで結果を返す） |
| lib/policy.py | policy.json の読み込み・検証・既定値の補完、trigger のパターン構文の解析、ack 項目を要求するゲートの引き当て |
| lib/shell_parse.py | `command_forms`: コマンド行に含まれるすべての単純コマンドを、原文と正規化した形の組で返す。読めない入力は `OPAQUE` の 1 組にする |
| lib/state.py | SQLite DAO 層。テーブルの操作、スキーマ初期化と列の追加、トランザクション管理、掃除。DB は `$CLAUDE_PLUGIN_DATA/checklist-gate.sqlite` に配置 |
| lib/prompt_text.py | 発話のうちユーザーが書いた部分の抽出と、システムの通知だけの発話の判定。prompt_store.py と gate_check.py が使う |
| config/policy.json.example | ゲートルールと ack 項目のスキーマリファレンス。apply-config skill がこのファイルを参照して `$CLAUDE_PLUGIN_DATA/policy.json` を生成する |
| templates/ | deny 理由やセッションコンテキストのテンプレートファイル。deny-reason.txt は `{gate_name}`, `{require_list}`, `{ack_commands}`, `{ack_hint}`, `{exit_section}`, `{prompt_match_section}` を使用。`{exit_section}` は deny-exit.txt（変数 `{exit_commands}`。user-prompt-match 以外の不足項目があるときだけ出す）、`{prompt_match_section}` は deny-prompt-match.txt（不足項目に user-prompt-match があるときだけ出す）。session-context.txt は `{plugin_data_dir}`, `{plugin_root}`, `{policy_notice}` を使用（session_init.py が置換） |
| config/policy-source.md | ゲートルールと ack 項目のデフォルトテンプレート（Markdown 形式）。初回セッション時に `$CLAUDE_PLUGIN_DATA` にコピーされ、ユーザーはコピー先を編集する。apply-config skill で policy.json に変換する |
| skills/ | Claude Code の skill 定義。apply-config（設定反映）、edit-config（設定編集）、gate-on / gate-off（ゲートの有効/無効切り替え）を提供 |
| docs/ | ユーザー向けドキュメント（導入手順、設定ガイド、運用ガイド、シナリオ例、監査・トラブルシューティング）と、この開発者向け文書 |
| tests/ | unittest ベースのテスト |

### 依存関係の方向

- scripts/* は lib/state.py に依存する（State クラスを利用）。validate_policy.py は DB を使わない
- scripts/gate_check.py, scripts/session_init.py, scripts/validate_policy.py は lib/policy.py に依存する
- scripts/gate_check.py は lib/shell_parse.py に依存する
- scripts/prompt_store.py, scripts/gate_check.py は lib/prompt_text.py に依存する
- scripts/gate_check.py, scripts/session_init.py は `$CLAUDE_PLUGIN_DATA/policy.json` を読み込む
- scripts/prompt_store.py は policy.json を読まない（発話記録のみ）
- scripts/gate_check.py は templates/ のテンプレートを読み込む
- scripts/session_init.py は templates/ のテンプレートを読み込む
- lib/* は外部依存なし（Python 3.9 以上の標準ライブラリのみ）
- hooks/hooks.json は `$CLAUDE_PLUGIN_ROOT` 経由で scripts/* を直接起動する
- skills/* は Claude Code が skill として読み込み、ユーザーの指示に応じて起動する
- apply-config skill は config/policy-source.md を読み込み、config/policy.json.example をスキーマ参照とし、scripts/validate_policy.py で検証してから policy.json を生成する

### 設計判断の補足

#### gate-ack / gate-toggle を hook 内部で処理する理由

gate-ack / gate-toggle コマンドは gate_check.py が PreToolUse フックで検出し、内部で ack 記録 + command をメッセージを表示するだけのコマンドに書き換えて処理する。外部バイナリとして実行しない理由は以下の通り。

- **permissions 問題**: Claude Code は Bash コマンドの実行前にユーザー許可を求める。gate-ack は `--reason` 引数の内容が毎回変わるため Always allow が効かず、ack のたびにユーザーに許可ダイアログが表示される

書き換え後のコマンドはフックを再び通らない。そのため、メッセージはシェルで引用した形でだけ埋め込む（`_shell_message`）。エージェントの入力や policy の文字列に `$(…)` やバッククォートが含まれていても展開されない。

#### gate-toggle off の制限

gate-toggle off は、`--prompt-id` で示したユーザー発話が直近の数件（`_INSTRUCTION_PROMPT_DISTANCE`）以内にあり、外す指示の語（off・オフ・外して・無効など）を否定なしで含み、かつゲート名か「ゲート」「gate」という語を含むときだけ受け付ける（`_verify_toggle_off`）。skill 名の「gate-off」は両方の語として扱う。エージェントが自分の判断でゲートを外せないようにするため。on には条件を付けない。トグルは session_id ごとに DB の gate_toggles に残るため、同じセッションの再開では持ち越され、`/clear` や新しいセッションでは持ち越されない。

#### hooks.json のパス解決

Claude Code はプラグインの hook 実行時に `$CLAUDE_PLUGIN_ROOT` 環境変数をプラグインのインストール先ディレクトリに設定する。hooks.json ではこの変数を使って scripts/ 配下の Python スクリプトを直接呼び出す。

#### $CLAUDE_PLUGIN_DATA（永続データディレクトリ）

Claude Code はプラグインの hook 実行時に `$CLAUDE_PLUGIN_DATA` 環境変数をプラグイン固有の永続データディレクトリに設定する。`$CLAUDE_PLUGIN_ROOT`（コードディレクトリ）はプラグイン更新時に上書きされるため、ユーザーデータ（policy.json, policy-source.md, SQLite DB）は `$CLAUDE_PLUGIN_DATA` に配置する。

#### 異常時に止める側に倒す理由

policy.json の不正・DB のロック・フックの内部エラーで黙って通すと、ゲートが無効のまま作業が進む。そのため、ack を要求するゲートが関わる呼び出しでは異常を deny にする。

- 関わるかどうかは、policy の読み込みと照合（DB を使わない計算）で先に分かる。分からないうちに異常が起きたら止める
- policy.json が不正なときは、どのゲートが何に掛かるか分からないため、止める範囲を広く取る。読み取り専用のツール（`_READ_ONLY_TOOLS`）は止めず、復旧の経路（policy-source.md を読む）を残す。許可済みの policy.json の書き込みも通す
- policy.json が無いときは止めない（導入直後の状態）
- 天の声ゲートだけが関わる呼び出しは、異常が起きても通す
- PreToolUse のフック宣言には、起動の失敗・時間切れ・想定外の終了を止める側に倒す設定（`onFailure`）を付けて二重化する。タイムアウトは DB の待ち時間（`BUSY_TIMEOUT_SECONDS`）より十分長くする。SessionStart と UserPromptSubmit には付けない（失敗で発話やセッションの開始を止めないため）

#### policy.json の保護

policy.json への Edit/Write の保護は、policy の設定ではなくフックの組み込みの規則にしている。policy が壊れていても無くても効き、トグルでも外せない。

- 対象は Edit・Write・MultiEdit で、`file_path` と policy.json のパスを `realpath` で解いて小文字にしたものを比べる（データディレクトリがシンボリックリンク経由でも、大文字小文字が違っても対象になる）
- 許可は、直近の発話（`_INSTRUCTION_PROMPT_DISTANCE` 件）のうち、`_APPLY_CONFIG_KEYWORDS` を含む最新の発話が否定されていないとき。apply-config は複数の発話にまたがるため、最新の 1 件ではなく直近の複数件を見る
- Write は内容も `parse_policy` で検証し、不正なら不正の一覧つきで拒否する
- 対象は policy.json だけ。policy-source.md は対象外

#### Monitor・NotebookEdit の別名

`Bash(...)` のパターンは Monitor の `command` にも、`Edit(...)` のパターンは NotebookEdit の `notebook_path` にも照合する（`_TOOL_ALIASES`）。設定を変えずに、Bash・Edit を対象にしたゲートが同じ操作を行う別のツールにも掛かる。Edit の別名に Write は入れない。

#### policy.json の検証の厳しさ

未知のキーも不正にする（`except_pattern` のような綴り違いを黙って無視しないため）。独自のキーを書いている policy は不正になるが、不正の箇所が表示される。パターンは前後の空白と括弧の不正を弾く。照合は大文字小文字を区別せず、ワイルドカードは `*` だけ。

#### 掃除の基準

`sessions.last_active_at`（セッション開始・再開と発話の記録で更新）を基準に、表ごとの保持期間（`_RETENTION_DAYS`）より古いセッションの行を消す。再開した古いセッションの記録が消えない。sessions に行が無い孤立した行は、時刻の列を持つ表はその時刻で、持たない表は同じ session_id の発話が無いことで消す。

---

## 3. 拡張ポイント

### 新しいゲートの追加

- policy-source.md にゲート定義を追加し、apply-config skill で policy.json に反映する（直接 policy.json を編集しても可。検証スクリプトで確かめること）
- ゲートの対象は trigger の patterns / except_patterns だけで決まる。新しい種類の判定が要るときは、gate_check.py の照合（`_matches_pattern`・`_gate_applies`）と、lib/policy.py の検証の両方を拡張する

### 新しい ack 項目の追加

- policy-source.md の ack 項目セクションに定義を追加し、apply-config skill で policy.json に反映する（type, min_reason_length, hint を指定。省略したものは既定値が補われる）
- type は session（セッション中1回で永続）、consumable（使い捨て）、または user-prompt-match（ユーザー発話検証付き使い捨て）を選択
- user-prompt-match タイプの場合は match_keywords（必須）, except_keywords, max_prompt_distance, auto_authorize_after_gate を指定する
- 対応するゲートの require 配列に項目名を追加する
- 項目のキーを増やすときは、lib/policy.py の検証と既定値の補完にも足す（未知のキーは不正になる）

### skill の追加

- skills/ 配下にディレクトリを作成し、SKILL.md を配置する
- SKILL.md の YAML frontmatter に name と description を記載する
- description にはトリガーとなるユーザーの発話例を含める
- 同梱の skill（apply-config, edit-config, gate-on, gate-off）を参考にする

### trigger の記法

patterns / except_patterns に `Tool(パターン)` 形式で対象を指定する（Claude Code の permissions と同じ記法）。

記法の例:
- `Edit(*)` -- Edit ツール全体
- `Read(*README*)` -- README を含むパスの Read
- `Bash(git push *)` -- git push コマンド
- `Bash(git push --dry-run *)` -- dry-run 付き git push

Bash（と Monitor）のコマンド行は、lib/shell_parse.py の `command_forms` が単純コマンドに分け、それぞれ原文（`literal`）と正規化した形（`canonical`。環境変数の代入・コマンドのパス・大文字小文字・git のグローバルオプションを除いたもの）の組で返す。パターンはどちらかに一致すればよい。コマンド名から始まるパターンは引数の中の文字列には反応しない（`*` で始まるパターンは反応する）。

`command_forms` の処理は 3 段:

1. 字句解析: 引用符の種類・`$(…)`・バッククォート・`<(…)`・ヒアドキュメント・コメント・行継続を 1 回の走査で扱う
2. 単純コマンドへの分割: 制御の演算子で区切り、リダイレクトを語から外し、予約語を除く
3. 正規化と包みの剥がし: `bash -c`・`eval`・`env`・`xargs` などの包みを再帰的に開く（深さに上限がある）。git の `-c alias.…` は展開する

解析器が扱えない入力（引用符の閉じ忘れなどの構文エラーのほか、置換の中の `case` のようにシェルは実行できるが解析器が読み切れない形を含む）と、置換・包みの入れ子が深すぎる入力は、正規形を `OPAQUE` にした 1 つの組として返し、`_gate_applies` はそれをそのツールのすべてのパターンに当たり除外には当たらないものとして扱う。区切り文字での分割などで推測して読む方法は、引用符の組がずれた入力でコマンドを隠すため採らない。解釈は bash の文法を基準にし、zsh などと読み方が分かれる箇所は隠さない側（ゲートがかかる側）に倒す。

ゲートをかけるかは、シェルのツールでは単純コマンドごとに「patterns に一致し、except_patterns に一致しない」かで判定し、1 つでも当てはまればかける（`_gate_applies`）。除外に当たるコマンドを連結に混ぜても、他のコマンドは除外されない。パイプの後ろや置換の中も単純コマンドとして判定される。シェル以外のツールは、呼び出し全体で判定する（パスは `normpath` で正規化する）。

照合は `_glob_regex` が行う。`*` だけがワイルドカードで、大文字小文字を区別しない。ツール名にも `*` を書ける。

---

## 4. deny-first の仕組み

deny-first は全ての ack タイプ（session / consumable / user-prompt-match）に共通する仕組み。gate-ack は対応するゲートの deny が発生した後にのみ受け付ける。

### データ構造

gate_denies テーブルで `(session_id, gate_name)` を UNIQUE 制約で管理する。同一ゲートにつき最新の deny のみ保持する。

### ライフサイクル

1. **deny 発生時**: gate_check.py がゲート条件未充足を検出すると、deny レスポンスを返却すると同時に gate_denies テーブルに UPSERT（INSERT OR REPLACE）で記録する
2. **ack 成立時**: deny は消さない。require に複数の項目を持つゲートで、1 回の deny の後に全項目を ack できるようにするため
3. **ゲート通過時**: 対応する `(session_id, gate_name)` のレコードを削除する。次回は再度 deny を経る必要がある
4. **再 deny 時**: 同一ゲートで再度 deny が発生すると、UPSERT により created_at が更新される

この仕組みにより:
- エージェントが deny メッセージを読む前に先回り ack することを構造的に防止する
- 古い deny 記録の使い回しが防止される

deny が残っている間に同じ項目を何度も ack して積み増すことは、deny-first ではなく「未消費の ack が残っている項目には追加の ack を受け付けない」規則で防ぐ。consumable・user-prompt-match の未消費の ack は、項目ごとに常に 1 つまでになる。

自動通過（5 章）はフックが ack を記録するもので、gate-ack を経由しないため deny-first の対象外。

---

## 5. user-prompt-match ack タイプ

エージェントの自己申告ではなく、ユーザーの発話原文を機械的に検証する ack タイプ。

### UserPromptSubmit hook による発話記録

UserPromptSubmit hook（prompt_store.py）がユーザーの発話を user_prompts テーブルに記録し、prompt_id を発行する。フックの additionalContext 出力でエージェントに通知する。

通知フォーマット: `[checklist-gate: prompt_id=N]`

Claude Code は、バックグラウンドタスクの完了通知や Stop フックの連絡も UserPromptSubmit として渡してくる。これらのシステムの通知のブロックだけからなる発話は記録しない。記録しないので、max_prompt_distance の数え方や天の声のターンの区切りにも影響しない。

### 検証フロー

gate-ack 実行時に gate_check.py が以下の順序で検証する:

1. **deny-first チェック**: 対応するゲートの deny が gate_denies に記録されているか
2. **積み増しチェック**: 同じ項目に未消費の ack が残っていないか（範囲外に出た発話を根拠にした ack は先に失効させる）
3. **発話実在チェック**: 指定された prompt_id が当該セッションの user_prompts に存在するか
4. **max_prompt_distance チェック**: prompt_id が直近 N 件の発話以内に含まれるか（古い発話の持ち出し防止）
5. **使用済みチェック**: その prompt_id を根拠にした ack が、同じ項目にすでにないか（1 つの発話で 1 回まで）
6. **キーワード検証**（`--affirm` の有無で対象が異なる）

いずれかの検証に失敗した場合、ack を拒否してエラーメッセージを返却する。

### キーワード評価の流れ

発話は、IDE の情報・貼り付けたテキスト・システムの通知のブロックを取り除き、ユーザーが書いた部分だけを対象にする（lib/prompt_text.py の `authored_text`）。

- match 判定・肯定応答チェック: 英字の大小を区別しない。英数字で始まる・終わるキーワードは単語として照合し、前後に英数字・`_`・`-` が続く場合は一致としない（`web_push` は `push` に、`look` は `ok` に一致しない）
- except 判定: 空白を除去し英字を小文字に変換したうえで、部分一致で照合する
- 否定判定: match_keywords の一致箇所の直後（日本語の否定の語）か前後（英語の否定の語）の数文字に否定の語があれば NG（`_negated_mention`）。語のリストと見る範囲は gate_check.py の定数

match・except・否定の 3 つの判定は `_authorizes` にまとめてあり、gate-ack・自動通過・gate-toggle off が共通に使う。

**`--affirm` なしの場合（通常フロー）:**

1. 指定された prompt_id の発話から、ユーザーが書いた部分を取り出す
2. **match 判定**: match_keywords のいずれかが発話に含まれるか → 含まれなければ NG
3. **except 判定・否定判定**: except_keywords を含むか、キーワードが否定されていれば NG
4. すべて通過すれば OK

**`--affirm` ありの場合（肯定応答フロー）:**

ユーザー発話が「OK」「はい」など肯定応答のみで、キーワードを含まない場合に使用する。`--affirm` の値にはAIがユーザーに発した質問の引用テキストを指定する。

1. 指定された prompt_id の発話から、ユーザーが書いた部分を取り出す
2. **肯定応答チェック**: 発話が affirm_keywords（policy.json トップレベル）のいずれかに該当するか → 該当しなければ拒否。affirm_keywords を省略したとき（既定値は空）は `--affirm` 自体を拒否する。policy.json に空の配列を書くことは検証で不正になる
3. **`--affirm` の値に対して match 判定**: match_keywords のいずれかが含まれるか → 含まれなければ NG
4. **`--affirm` の値に対して except 判定・否定判定**: except_keywords を含むか、キーワードが否定されていれば NG
5. すべて通過すれば OK

例:
- ユーザー発話「push して」→ `--affirm` 不要。発話原文に "push" が含まれるので通常フローで通過
- AI質問「push しますか？」→ ユーザー応答「OK」→ `--affirm "push しますか？"` を指定。affirm の値に "push" が含まれるので通過

### affirm_keywords

policy.json のトップレベルに定義する、肯定応答判定用のキーワード一覧。`--affirm` を使用する際に、ユーザー発話がこれらのキーワードのいずれかに該当するかを事前チェックする。

```json
"affirm_keywords": ["OK", "はい", "よい", "いいよ", "お願い", "yes", "sure", "うん", "おけ", "いいね"]
```

- OR 条件: いずれか1つが発話に含まれれば肯定応答と判定
- 単語として照合する（match 判定と同じ）

### 自動通過

`auto_authorize_after_gate` を持つ user-prompt-match 項目がゲートで不足していると判定されたとき、gate_check.py は gate-ack を待たずに根拠になる発話を探す（`_find_auto_authorization`。DB には書かない）。

1. 直近 max_prompt_distance 件の発話を新しい順に見て、match_keywords を含む最新の発話を選ぶ
2. その発話が `_authorizes` を通らない（except・否定）、その発話を根拠にした ack がすでにある、または前提ゲートの最後の通過がその発話のターンより前なら、自動通過しない。前提ゲートを同じ呼び出しの中で通過する場合は、今のターンで通過したものとして扱う
3. 見つかった発話は、全ゲートが充足した後、消費と同じ書き込みロックの中で「消費済みの ack」として記録する（`State.consume_acks`）。そこで同じ発話の ack がすでにあれば（並列の呼び出しが先に使った場合）、何も変えずに deny する。通過したときは additionalContext でエージェントに通知する

前提ゲートの通過は gate_passes テーブルに、通過した時点の最新の prompt_id として記録する。記録するのは、いずれかの項目の `auto_authorize_after_gate` に名前があるゲートだけ。dry-run のように実際には commit しないコマンドは、前提ゲートの except_patterns に入れておく（同梱の例は `Bash(git commit --dry-run *)` を除外している）。except に当たったコマンドではゲートが評価されないので、通過も記録されない。

### 消費の原子性

ack の消費は、全ゲートの判定が終わってから、1 つの書き込みロック（`BEGIN IMMEDIATE`）の中でまとめて行う。

- 連結したコマンドで後ろのゲートが deny しても、前のゲートの ack は消費されない
- 並列に走った 2 つの呼び出しが同じ ack を確認しても、消費できるのは片方だけで、もう片方は deny になる
- 天の声の発火の記録も、全ゲートの通過後に行う。deny された呼び出しで発火済みにはならない

自動通過は「ユーザーの指示どおりに作業が流れている」ことをフックが確認できる場面に限る。commit 済みの状態での「push して」や、`--affirm` が必要な肯定応答は、エージェントの判断を挟むため通常の deny → gate-ack とする。

### JSON 設定例

```json
{
  "user_authorized_push": {
    "type": "user-prompt-match",
    "min_reason_length": 20,
    "hint": "ユーザー発話に明示的な push 指示があった？",
    "match_keywords": ["push", "プッシュ"],
    "except_keywords": ["dry-run", "ドライラン"],
    "max_prompt_distance": 3,
    "auto_authorize_after_gate": "git_commit_gate"
  }
}
```

---

## 6. テスト戦略

### 方針

- unittest を使用する。プラグイン直下で `python3 -m unittest discover -s tests -t .` を実行し、Python 3.9 でも全件通すこと
- テストの期待値は要件の不変条件で書く（「push が止まる」「止めた理由に不正の場所が含まれる」など）。調整される値（保持日数・距離・待ち時間）の実値は固定しない
- フックのテストは、実際の gate_check.py などを subprocess で起動し、一時ディレクトリの実物の policy.json と SQLite を使う（モックを使わない）。共通の道具は tests/hook_harness.py（起動と、deny・無言の通過・additionalContext の判定ヘルパー）。「通過」の確認では stderr が空であることも見て、落ちて通過した場合と区別する
- policy の雛形は config/policy.json.example を読んで作り、必要な箇所だけ変える
- State のテストは :memory: SQLite を使い、各テストで State を新規作成する

### テストファイル

| ファイル | 対象 |
|---|---|
| test_state.py | State の操作（セッション・ack・発話・deny・トグル・掃除・トランザクション）。掃除は表ごと |
| test_shell_parse.py | `command_forms` を、包み・置換・引用符・ヒアドキュメント・コメント・行継続・解析の失敗の表で判定する純粋関数のテスト |
| test_matching.py | 大文字小文字、パスの正規化、`[ ]` `?` が文字どおりであること、ツール名のワイルドカード、Monitor・NotebookEdit の別名、末尾 ` *` の扱い、除外が単純コマンドごとであること |
| test_policy.py | 検証規則（同梱の例が通ること、不正の各種）と既定値の補完 |
| test_validate_policy_cli.py | 検証スクリプトの終了コードと出力 |
| test_fail_closed.py | policy.json の不正・未作成、DB の破損、stdin の不正で、止める・通すが設計どおりであること。Python 3.9 互換（構文と注釈の確認） |
| test_policy_protection.py | policy.json への Edit/Write が、発話の条件と内容の検証で通る・止まること |
| test_deny_message.py | deny のメッセージの節が、不足項目の種類で出し分けられること |
| test_session_init.py | データディレクトリの作成、policy.json の状態の知らせ、掃除（最終使用時刻と孤立した行） |
| test_prompt_store.py | システムの通知だけの発話を記録しないこと、閉じていないタグを含むユーザーの発話が記録されること、最終使用時刻の更新 |
| test_gate_commands.py | gate-ack・gate-toggle の拒否と受理（deny-first、最低文字数、発話の範囲、未知のオプション、`--affirm`）をフックを通して確かめる |
| test_gate_check_affirm.py | `--affirm` のロジックと、キーワードの照合（単語・正規化・IDE や貼り付けのブロックの除外） |
| test_auto_authorize.py | 自動通過の各条件（否定を含む）と、フックを通した自動通過 |
| test_hook_safety.py | 書き換え後のコマンドが入力や policy の文字列を展開しないこと（実際に bash で実行して確かめる）、gate-toggle off の制限、ack の積み増しと失効、連結したコマンドの消費、並列の呼び出しで 1 つの ack が 1 回しか使えないこと、deny された呼び出しで天の声が発火済みにならないこと |
| test_prompt_text.py | 発話からのブロックの除去と、システムの通知だけの発話の判定 |

### 手動シナリオテスト

- 実際の Claude Code セッションで以下のシナリオを確認する:
  - ドキュメント未確認の状態でファイル編集が拒否されること
  - gate-ack 後にファイル編集が許可されること
  - git commit が commit_ready ack なしで拒否されること
  - git push が user_authorized_push ack なしで拒否されること
  - deny を受ける前に先回り ack が拒否されること（deny-first）
  - user-prompt-match で正しい prompt_id を指定して ack が通ること
  - user-prompt-match でキーワード不一致の prompt_id を指定して ack が拒否されること
  - 「commit → push して」の後、commit を通過した push が gate-ack なしで通ること
  - gate-toggle でゲートの有効/無効が切り替わること

---

## 7. リリース手順

- 変更内容をコミットする
- config/policy.json.example と利用者の policy.json が、検証スクリプトで `OK` になることを確かめる
- 検証規則（lib/policy.py）を変えた場合は、利用者の policy.json が不正にならないか確認する
- plugin.json の version を、利用者の設定が止まる挙動を含めて影響の大きさに合わせて更新する
- テンプレートファイルを変更した場合は、deny メッセージの表示を確認する
- hooks.json の変更がある場合は、Claude Code のフック再読み込みが必要

---

## 8. 限界

- **シェルの解釈**: 変数の展開結果・git の設定ファイルのエイリアス・別プロセスや別言語やファイルを経由する実行・シェル関数などは解釈できない。一覧は設定リファレンスの「シェルの解釈の限界」を参照。解釈は bash の文法が基準で、zsh などと分かれる箇所はゲートがかかる側に倒している
- **Bash 経由の書き換え**: policy.json の保護は Edit/Write だけが対象で、Bash のリダイレクトや `sed -i` による書き換え、DB（SQLite）の直接の書き換えは防げない
- **onFailure**: フック宣言の `onFailure` は対応した版の Claude Code でだけ効く。古い版では無視されて通る。スクリプト内で異常を deny にする本体の対策は版によらず効く
- **SessionStart の systemMessage**: 利用者向けの表示が出るかは実行環境に依る。表示されなくても、同じ知らせが additionalContext でエージェントに届く
- **自動通過・発話の判定**: push という語を含まない取り消し（「やっぱりやめて」など）は読み取れない。照合は語の一致で、意図を理解するものではない
