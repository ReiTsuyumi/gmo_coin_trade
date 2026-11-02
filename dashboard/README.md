# GMO Coin Trade Dashboard

Raspberry Pi上の取引ボットが出力する `data/state_*.json` と `logs/decisions_*.csv` を読み取る、監視専用Webアプリです。売買操作、APIキーの送信、データベース、ログイン機能はありません。

## 起動

リポジトリのルートでボット用 `.env` を作成した後、次を実行します。

```bash
cd dashboard
cp .env.example .env
npm install
npm run build
npm start
```

同じLAN内の端末から `http://ラズパイのIPアドレス:3000` を開きます。インターネットへ公開しないでください。

## 検証

```bash
npm run check
npm test
npm run build
```

詳細なRaspberry Pi・systemdの手順は `ラズパイでの実行手順.md` を参照してください。
