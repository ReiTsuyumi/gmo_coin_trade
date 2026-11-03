# GMO Coin Trade

GMOコインの暗号資産レバレッジ取引向けPythonボットと、Raspberry Piで動かす監視専用ダッシュボードです。標準はdry-runで、実注文は明示的に切り替えた場合だけ送信します。

## 構成

```text
gmo_coin_trade/
├── gmo-trading-bot/
│   ├── gmo_trading_bot.py
│   ├── test_gmo_trading_bot.py
│   ├── requirements.txt
│   ├── env.example
│   └── セットアップと検証手順.md
└── dashboard/
```

## Raspberry Piでクローン

```bash
git clone https://github.com/ReiTsuyumi/gmo_coin_trade.git
cd gmo_coin_trade/gmo-trading-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp env.example .env
```

最初は `.env` の `DRY_RUN=true` のまま検証してください。

```bash
python -m unittest -v test_gmo_trading_bot.py
python gmo_trading_bot.py --once
```

継続実行は次のコマンドです。`.env`の5項目形式`TRADE_CRON`で指定した日本時間に判定し、既定の`0 * * * *`では毎時0分に全銘柄を順次処理します。`--once`はcron時刻を待たずに即時確認できます。

```bash
python gmo_trading_bot.py
```

## 監視ダッシュボード

別のターミナルで実行します。

```bash
cd /home/pi/gmo_coin_trade/dashboard
cp env.example .env
npm install
npm run build
npm start
```

同じLAN内のスマホやPCから `http://ラズパイのIPアドレス:3000` へ接続します。監視専用で、ブラウザから売買操作はできません。詳細は `dashboard/ラズパイでの実行手順.md` を参照してください。

## liveを使う場合

`.env` にAPIキーとシークレットを設定し、`DRY_RUN=false` と `LIVE_TRADING_ACK=I_UNDERSTAND_REAL_ORDERS` の両方を指定します。出金権限は付与せず、必要最小限の権限とIP制限を使ってください。

**`.env`、状態ファイル、ログはGit管理外です。実運用前にdry-runとPrivate API認証確認を行い、最小単位で検証してください。収益や損失上限の達成は保証されません。**
