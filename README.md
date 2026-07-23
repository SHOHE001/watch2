# watch2

**Discord から、tmux 上で動いている Claude Code の対話セッションを遠隔操作する bot。**

外出先のスマホや Apple Watch の Discord 通知から、自宅サーバーで走っている Claude Code セッションに返信・指示・承認ができます。「Claude に長い作業を任せて出かけたら、承認待ちで止まっていた」「外から一言だけ指示を足したい」を解決するためのツールです。

```
[外出先] Apple Watch / iPhone の Discord 通知
    │  「1」と返信（音声入力でもOK）
    ▼
[自宅サーバー] watch2 (systemd 常駐)
    │  tmux send-keys で数字キーを送信
    ▼
tmux 上の Claude Code 対話セッション
    │  選択肢が確定して作業続行
    ▼
応答・承認ダイアログはすべて Discord にミラーされる
```

## できること

| 機能 | 説明 |
|---|---|
| プロンプト送信 | Discord チャンネルに書いた文章がそのまま対話セッションに入力される。送達確認として元メッセージに ✅ リアクションが付く |
| 全ターンミラー | Claude の応答が**端末側で打ったターンも含めて**すべてチャンネルに投稿される。端末発のプロンプトは `🖥️` プレフィックス付き |
| 承認ダイアログの検知 | `AskUserQuestion`（選択肢質問）、プラン承認、permission プロンプトなど、**TUI に選択ダイアログが出たら自動で Discord に通知**される |
| 番号返信で回答 | ダイアログに対して「1」〜「9」の数字1文字を返信するだけで選択が確定する。Apple Watch の通知インライン返信（音声入力含む）で完結 |
| 端末回答の自動検知 | サーバーの前に戻って端末で直接回答した場合も自動で検知し、Discord 側の回答待ち状態を解除する |
| 誤爆防止 | チャンネル ↔ セッションの対応表にないチャンネルは完全無視。pane の作業ディレクトリが設定と一致しないときは送信しない |

### 実際のやりとりの例

```
（Claude が選択肢を出すと bot が投稿）
┌──────────────────────────────────────┐
│ ☐ テスト結果                          │
│ テストは成功？                        │
│ ❯ 1. はい                            │
│      テストは成功した                 │
│   2. いいえ                          │
│      テストは失敗した                 │
└──────────────────────────────────────┘
番号で返信できます

あなた: 1

bot: ✅ 回答: テストは成功？ → はい
bot: ✅ ダイアログ解決
bot: 検証完了！
```

## 仕組み

watch2 は3つの経路を組み合わせています。それぞれ「なぜこの方式か」に実機検証の裏付けがあります。

### 1. 入力: `tmux send-keys`

Discord のメッセージを `tmux send-keys -l -- <text>` でリテラル送信し、別コールで Enter を送ります（同一コールに混ぜると "Enter" が文字列として入力されてしまうため）。シェルを経由しない argv 直渡しなので、日本語・引用符・`$VAR` もエスケープ不要でそのまま届きます。

番号回答は**数字キー1発のみ**（Enter なし）。Claude Code の選択ダイアログは数字キーを押した瞬間に選択が確定することを実機で確認済みです。

### 2. 出力: セッション JSONL の常時 tail

Claude Code は対話内容を `~/.claude/projects/<cwd由来のディレクトリ名>/*.jsonl` に構造化データとして書き込みます。watch2 はこれをバイトオフセット管理で常時 tail し、`stop_reason == "end_turn"` でターン完了を検知して応答テキストを取り出します。

`tmux capture-pane`（画面キャプチャ）から応答を取らないのは、TUI の枠線・スピナー等の装飾が混ざり、差分検出も脆いためです。応答本文は常に JSONL を正とします。

### 3. ダイアログ検知: `capture-pane` の限定使用

承認ダイアログの検知**だけ**は capture-pane を使います。理由は実機検証で判明した次の事実です：

> **`AskUserQuestion` / `ExitPlanMode` の tool_use 行が JSONL に書かれるタイミングは不定**で、ダイアログが画面に表示されている間は何も書かれず、ユーザーが回答した後にまとめて書かれることがある。

つまり JSONL の tail だけでは「いま入力待ちである」ことを検知できません。そこで数秒おきに capture-pane を実行し、選択ダイアログのカーソル行（`❯ 1. ...` パターン）を検知したら Discord に通知します。この方式は AskUserQuestion に限らず、プラン承認・Bash 実行許可などの permission プロンプト全般を拾えるという利点もあります。

回答内容の取得（`toolUseResult.answers`）と回答済み判定は JSONL 側が担当し、両者は重複しないよう調停されます（JSONL 検知が先に立った場合は capture-pane 通知を抑止、回答済みで遅れて届いた JSONL 上の質問は投稿しない）。

## 必要要件

- Linux サーバー（tmux 上で Claude Code を対話モードで使っている環境）
- Python **3.11 以上**（`tomllib` を使用）
- tmux（Claude Code セッションと同一ユーザーの同一 tmux サーバー）
- Discord Bot（作成手順は下記）
- 依存パッケージ: `discord.py`, `python-dotenv`（`pip install -e .` で入ります）

## セットアップ

### 1. Discord Bot を作る

1. <https://discord.com/developers/applications> → **New Application**
2. 左メニュー **Bot** →
   - **Reset Token** で bot token を発行して控える（後で `.env` に入れる）
   - **Privileged Gateway Intents** → **MESSAGE CONTENT INTENT** を **ON**（⚠️ これを忘れると本文が読めず bot が完全に無反応になります。無反応トラブルの最頻出原因）
3. 左メニュー **OAuth2 → URL Generator** →
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Read Message History`, `Add Reactions`
   - 生成された URL を開いて自分の Discord サーバーに招待
4. Discord クライアントで **User Settings → Advanced → Developer Mode** を ON
5. bot 専用のチャンネルを作り（例: `#claude`）、チャンネルを右クリック → **ID をコピー**

### 2. クローンと仮想環境

```bash
git clone https://github.com/SHOHE001/watch2.git
cd watch2
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### 3. `.env` を作る

```bash
cp .env.example .env
# DISCORD_BOT_TOKEN に手順1のトークンを設定
```

`.env` に入るのはトークンだけです。チャンネルとセッションの対応は次の TOML で管理します。

### 4. `claude-watch.toml` を作る（チャンネル ↔ セッション対応表）

```bash
cp claude-watch.toml.example claude-watch.toml
```

```toml
[[projects]]
channel_id = 123456789012345678        # 手順1-5で控えたチャンネルID（整数）
tmux_target = "main:0.0"               # 対象 pane（session:window.pane）
cwd = "/home/you/projects/myapp"       # そのセッションの作業ディレクトリ（絶対パス）
```

- `tmux_target` は対象 pane で `tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}'` を実行すると確認できます
- `cwd` は Claude Code をそのセッションで**起動したときのディレクトリ**を指定します（セッション JSONL の場所の特定と、誤爆防止の照合に使います）
- 複数プロジェクトを使い分けるときは `[[projects]]` ブロックを追加します（チャンネル1つにつきセッション1つ）
- 設定に不備がある場合（channel_id 重複、必須キー欠落、相対パスなど）は**起動時に即エラー**で落ちます（fail-fast）

### 5. 操作対象のセッションを用意して動作確認

watch2 は**既に動いている対話セッションに割り込む**ツールです。セッションの新規起動はしません。先に tmux 上で Claude Code を対話モードで起動しておいてください。

```bash
# フォアグラウンドで起動
.venv/bin/python -m watch2
# 「watch2 ready as <bot名>」が出たら、対象チャンネルに何か書いて応答が返るか確認
```

### 6. systemd で常駐化

```bash
sudo cp deploy/watch2.service /etc/systemd/system/
# ⚠️ unit 内のパス（WorkingDirectory / EnvironmentFile / ExecStart）を自分の環境に書き換えること
sudo systemctl daemon-reload
sudo systemctl enable --now watch2
journalctl -u watch2 -f   # ログ追跡
```

unit を自作・修正する場合の**重要な注意**（どちらも実際に踏んだ罠です）：

- **`PrivateTmp=true` を設定しないこと。** tmux のサーバーソケットは `/tmp/tmux-<uid>/` にあるため、PrivateTmp を有効にすると list-panes / send-keys / capture-pane が全滅します
- パスにスペースを含む場合、`ExecStart` は単語分割されるので実行ファイルパスを `"..."` で囲むこと（`WorkingDirectory` / `EnvironmentFile` は行全体が1つの値なので引用符不要）

## 使い方

### 通常のプロンプト

対応表に登録したチャンネルに普通に書き込むだけです。送信に成功すると元メッセージに ✅ リアクションが付き、Claude の応答はターン完了時にチャンネルへ投稿されます。

### 承認ダイアログへの回答

ダイアログが検知されるとチャンネルに選択肢が投稿され、そのチャンネルは**回答待ちモード**になります。

- **「1」〜「9」の数字1文字だけ**を返信すると、その数字キーが TUI に送られて選択が確定します
- 回答待ち中に数字以外を送ると `⚠️ 承認の回答待ちです（番号で返信）` と返り、**セッションには流れません**（開いているダイアログに文章を誤爆させないための安全弁）
- 端末側の表示には `Type something.`（自由記述）や `Chat about this` などの追加選択肢が自動で付くことがあります。投稿された選択肢より大きい番号もそのまま TUI に送られるので、画面抜粋を見て選んでください
- **`!reset`** と送ると回答待ちモードを手動解除できます（TUI 側のダイアログはそのまま。bot の状態だけリセット）

### メッセージの読み方

| 表示 | 意味 |
|---|---|
| `🖥️ <text>` | 端末側で直接入力されたプロンプトのミラー |
| ` ``` 画面抜粋 ``` 番号で返信できます` | 選択ダイアログを検知。回答待ちモードに入った |
| `✅ 回答: <質問> → <選択>` | ダイアログが回答された（Discord 発・端末発どちらも） |
| `✅ ダイアログ解決` | 画面からダイアログが消えたことを確認し、回答待ちを解除した |
| `⚠️ <理由>` | エラー。pane 不在・作業ディレクトリ不一致など。**無言で失敗することはありません** |

### Apple Watch から使う

特別な設定は不要です。対象チャンネルの通知を Watch に出るようにしておけば、通知のインライン返信（音声入力・手書き・定型文）でプロンプトも番号回答も送れます。「1」「2」のような定型返信を登録しておくと承認が最速になります。

## トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| bot が完全に無反応 | ①Bot の **MESSAGE CONTENT INTENT** が OFF（最頻出）②チャンネルが対応表にない（未登録チャンネルは仕様として無視）③`journalctl -u watch2` でログ確認 |
| `⚠️ tmux pane が見つかりません` | `tmux_target` の pane が実在するか `tmux list-panes -a` で確認。pane を閉じて作り直した場合は対応表の更新が必要 |
| `⚠️ pane cwd が設定と一致しません` | 対象 pane のカレントディレクトリと toml の `cwd` の不一致。別セッションへの誤爆を防ぐ安全弁なので、`cwd` を実際の起動ディレクトリに合わせる |
| 応答がミラーされない | 対象セッションの JSONL がまだ無い可能性（起動直後で1ターンも会話していない）。1ターンやり取りすると作られる |
| systemd 起動後だけ tmux 操作が失敗する | unit に `PrivateTmp=true` が入っていないか確認（上記の注意参照）。また pane 検証には `tmux display-message` ではなく `list-panes` を使う必要がある（制御端末なしの環境では display-message が rc=0 のまま空文字を返すため。watch2 は対応済みだが、fork する場合の参考に） |
| ⚠️ が二重に届く・様子がおかしい | 同じ bot トークンで別プロセス（旧バージョンや多重起動）が同居していないか確認: `pgrep -fa "watch2"` と `systemctl list-units | grep -i watch` |

## 制限事項（設計上のスコープ外）

- **セッションの新規起動はしない。** 操作対象の対話セッションは人間が事前に tmux で立てておく前提（bot は割り込むだけ）
- `AskUserQuestion` の複数選択（multiSelect）と自由記述（Type something）は番号返信では完結しません。投稿に注意書きが付くので端末で回答してください
- Discord のボタン UI は不使用。Watch の通知インライン返信で完結させるため、あえてテキスト番号返信に統一しています
- Webhook・push 通知（hook 連携）は現バージョンには含まれていません

## テスト

```bash
.venv/bin/pytest -v
```

Discord・実 tmux・ネットワークは不要です（tmux 呼び出しは runner の依存注入、JSONL は一時ファイル、Discord クライアントはモックでテストします）。

## 背景

前身の [applewatch (v1)](https://github.com/SHOHE001/applewatch) の設計思想（`-p` ヘッドレス実行ではなく対話セッションへの割り込み、エラーを silent drop しない、決めつけずに実機 PoC で確認する）を引き継いだ作り直しです。経緯と設計判断の詳細は [`設計思想.md`](./設計思想.md) を参照してください。

## ライセンス

未定（個人プロジェクト）。

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)
