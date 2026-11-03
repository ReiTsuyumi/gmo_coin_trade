import { afterEach, describe, expect, it } from "vitest";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { getDashboardSnapshot, parseCsv } from "./dashboard";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe("parseCsv", () => {
  it("BOM・引用符内カンマ・二重引用符を読む", () => {
    const rows = parseCsv('\uFEFFtimestamp_jst,event,reason,result\n2026-10-29T10:00:00+09:00,HOLD,"EMA, RSI","waiting ""spread"""\n');
    expect(rows).toEqual([{
      timestamp_jst: "2026-10-29T10:00:00+09:00",
      event: "HOLD",
      reason: "EMA, RSI",
      result: 'waiting "spread"',
    }]);
  });

  it("引用符内の改行を1レコードとして保持する", () => {
    const rows = parseCsv('event,reason\nERROR,"line1\nline2"\n');
    expect(rows[0].reason).toBe("line1\nline2");
  });
});

describe("dashboard snapshot", () => {
  it("ボットデータがない環境ではデモであることを明示する", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "gmo-dashboard-empty-"));
    temporaryDirectories.push(directory);
    process.env.BOT_DATA_DIR = path.join(directory, "data");
    process.env.BOT_LOG_DIR = path.join(directory, "logs");
    process.env.BOT_ENV_FILE = path.join(directory, ".env");

    const snapshot = await getDashboardSnapshot();
    expect(snapshot.source).toBe("demo");
    expect(snapshot.activities.length).toBeGreaterThan(0);
    expect(snapshot.equityHistory.length).toBeGreaterThan(24);
  });

  it("設定済みだが状態ファイルがない銘柄も選択でき、待機中と表示する", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "gmo-dashboard-waiting-"));
    temporaryDirectories.push(directory);
    process.env.BOT_DATA_DIR = path.join(directory, "data");
    process.env.BOT_LOG_DIR = path.join(directory, "logs");
    process.env.BOT_ENV_FILE = path.join(directory, ".env");
    await writeFile(process.env.BOT_ENV_FILE, "DRY_RUN=true\nSYMBOLS=BTC_JPY,ETH_JPY,XRP_JPY\n");

    const snapshot = await getDashboardSnapshot("ETH_JPY");
    expect(snapshot.source).toBe("bot");
    expect(snapshot.bot.symbol).toBe("ETH_JPY");
    expect(snapshot.bot.availableSymbols).toEqual(["BTC_JPY", "ETH_JPY", "XRP_JPY"]);
    expect(snapshot.bot.health).toBe("waiting");
    expect(snapshot.market).toBeNull();
    expect(snapshot.activities).toEqual([]);
  });

  it("実状態がない銘柄を選んでも別銘柄の状態へ置き換えない", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "gmo-dashboard-existing-"));
    temporaryDirectories.push(directory);
    const dataDirectory = path.join(directory, "data");
    process.env.BOT_DATA_DIR = dataDirectory;
    process.env.BOT_LOG_DIR = path.join(directory, "logs");
    process.env.BOT_ENV_FILE = path.join(directory, ".env");
    await writeFile(process.env.BOT_ENV_FILE, "DRY_RUN=true\nSYMBOLS=BTC_JPY,ETH_JPY\n");
    await import("node:fs/promises").then(({ mkdir }) => mkdir(dataDirectory));
    await writeFile(path.join(dataDirectory, "state_BTC_JPY_dry-run.json"), JSON.stringify({
      symbol: "BTC_JPY",
      mode: "dry-run",
      cash: "100000",
      account_equity: "100000",
      available_balance: "100000",
      updated_at: new Date().toISOString(),
    }));

    const snapshot = await getDashboardSnapshot("ETH_JPY");
    expect(snapshot.bot.symbol).toBe("ETH_JPY");
    expect(snapshot.bot.health).toBe("waiting");
    expect(snapshot.account.equity).toBe(0);
  });
});
