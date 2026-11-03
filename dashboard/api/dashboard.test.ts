import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { getDashboardSnapshot, parseCsv } from "./dashboard";
import { sortActivitiesNewestFirst } from "../src/lib/format";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  vi.unstubAllGlobals();
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

describe("activity ordering", () => {
  it("画面のログを日時の新しい順に並べ、元の配列を変更しない", () => {
    const rows = [
      { id: "old", timestamp: "2026-11-03T09:00:00+09:00" },
      { id: "new", timestamp: "2026-11-03T12:00:00+09:00" },
      { id: "middle", timestamp: "2026-11-03T10:30:00+09:00" },
    ];

    const sorted = sortActivitiesNewestFirst(rows);

    expect(sorted.map((item) => item.id)).toEqual(["new", "middle", "old"]);
    expect(rows.map((item) => item.id)).toEqual(["old", "new", "middle"]);
  });
});

describe("dashboard snapshot", () => {
  const officialSymbols = ["BTC_JPY", "ETH_JPY", "XRP_JPY"];
  const mockOfficialSymbols = () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      status: 0,
      data: [{ symbol: "BTC" }, ...officialSymbols.map((symbol) => ({ symbol }))],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
  };
  it("ボットデータがなく公式一覧も取得できない環境ではデモであることを明示する", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("unavailable", { status: 503 })));
    const directory = await mkdtemp(path.join(tmpdir(), "gmo-dashboard-empty-"));
    temporaryDirectories.push(directory);
    process.env.BOT_DATA_DIR = path.join(directory, "data");
    process.env.BOT_LOG_DIR = path.join(directory, "logs");
    process.env.BOT_ENV_FILE = path.join(directory, ".env");

    const snapshot = await getDashboardSnapshot();
    expect(snapshot.source).toBe("demo");
    expect(snapshot.activities.length).toBeGreaterThan(0);
    expect(snapshot.activities.every((item) =>
      [item.reason, item.result].every((text) => /^[\x00-\x7F]*$/.test(text))
    )).toBe(true);
    expect(snapshot.equityHistory.length).toBeGreaterThan(24);
  });

  it("公式一覧と状態を突合し、未作成の銘柄も待機中として選べる", async () => {
    mockOfficialSymbols();
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
    mockOfficialSymbols();
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

  it("reads both English and legacy Japanese SMA fields", async () => {
    mockOfficialSymbols();
    const directory = await mkdtemp(path.join(tmpdir(), "gmo-dashboard-sma-"));
    temporaryDirectories.push(directory);
    const dataDirectory = path.join(directory, "data");
    const logDirectory = path.join(directory, "logs");
    process.env.BOT_DATA_DIR = dataDirectory;
    process.env.BOT_LOG_DIR = logDirectory;
    process.env.BOT_ENV_FILE = path.join(directory, ".env");
    await import("node:fs/promises").then(({ mkdir }) => Promise.all([
      mkdir(dataDirectory),
      mkdir(logDirectory),
    ]));
    await writeFile(process.env.BOT_ENV_FILE, "DRY_RUN=true\nSYMBOLS=BTC_JPY\n");
    await writeFile(path.join(dataDirectory, "state_BTC_JPY_dry-run.json"), JSON.stringify({
      symbol: "BTC_JPY",
      mode: "dry-run",
      updated_at: new Date().toISOString(),
    }));
    const logPath = path.join(logDirectory, "decisions_20261103.csv");
    const header = "timestamp_jst,event,symbol,reason,result,equity_jpy,mid_price,best_bid,best_ask,spread,spread_rate\n";
    const writeMarket = (reason: string) => writeFile(
      logPath,
      `${header}2026-11-03T10:00:00+09:00,MARKET,BTC_JPY,"${reason}",evaluated,100000,105,104,106,2,0.019047\n`,
    );

    await writeMarket("SMA_short=105.00, SMA_long=100.00");
    const english = await getDashboardSnapshot("BTC_JPY");
    expect(english.market?.smaShort).toBe(105);
    expect(english.market?.smaLong).toBe(100);

    await writeMarket("SMA短期=105.00, SMA長期=100.00");
    const legacy = await getDashboardSnapshot("BTC_JPY");
    expect(legacy.market?.smaShort).toBe(105);
    expect(legacy.market?.smaLong).toBe(100);
  });

  it("公式APIが失敗しても設定と状態の銘柄を一覧に残す", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("unavailable", { status: 503 })));
    const directory = await mkdtemp(path.join(tmpdir(), "gmo-dashboard-fallback-"));
    temporaryDirectories.push(directory);
    const dataDirectory = path.join(directory, "data");
    process.env.BOT_DATA_DIR = dataDirectory;
    process.env.BOT_LOG_DIR = path.join(directory, "logs");
    process.env.BOT_ENV_FILE = path.join(directory, ".env");
    await writeFile(process.env.BOT_ENV_FILE, "DRY_RUN=true\nSYMBOLS=BTC_JPY,ETH_JPY\n");
    await import("node:fs/promises").then(({ mkdir }) => mkdir(dataDirectory));
    await writeFile(path.join(dataDirectory, "state_XRP_JPY_dry-run.json"), JSON.stringify({
      symbol: "XRP_JPY",
      mode: "dry-run",
      updated_at: new Date().toISOString(),
    }));

    const snapshot = await getDashboardSnapshot();
    expect(snapshot.bot.availableSymbols).toEqual(["BTC_JPY", "ETH_JPY", "XRP_JPY"]);
    expect(snapshot.bot.symbol).toBe("BTC_JPY");
    expect(snapshot.bot.health).toBe("waiting");
  });
});
