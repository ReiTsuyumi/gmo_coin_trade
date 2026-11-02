import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { parse as parseEnv } from "dotenv";
import type {
  ActivityItem,
  DashboardSnapshot,
  EquityPoint,
} from "@contracts/dashboard";

const DATA_DIR = path.resolve(process.env.BOT_DATA_DIR || "../data");
const LOG_DIR = path.resolve(process.env.BOT_LOG_DIR || "../logs");
const ENV_FILE = path.resolve(process.env.BOT_ENV_FILE || "../.env");
const MAX_LOG_BYTES = 4 * 1024 * 1024;

type CsvRow = Record<string, string>;
type BotState = Record<string, unknown> & {
  symbol: string;
  mode: string;
  updated_at?: string;
};

function numberValue(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableNumber(value: unknown): number | null {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** RFC4180対応。引用符内のカンマ・改行・二重引用符を処理する。 */
export function parseCsv(text: string): CsvRow[] {
  const source = text.replace(/^\uFEFF/, "");
  const records: string[][] = [];
  let record: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"' && field === "") {
      quoted = true;
    } else if (char === ",") {
      record.push(field);
      field = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && source[index + 1] === "\n") index += 1;
      record.push(field);
      field = "";
      if (record.some((cell) => cell !== "")) records.push(record);
      record = [];
    } else {
      field += char;
    }
  }
  if (field !== "" || record.length > 0) {
    record.push(field);
    records.push(record);
  }
  const headers = records.shift() || [];
  return records.map((cells) =>
    Object.fromEntries(headers.map((header, index) => [header, cells[index] || ""])),
  );
}

async function readTail(filePath: string): Promise<string> {
  const stat = await fs.stat(filePath);
  const length = Math.min(stat.size, MAX_LOG_BYTES);
  const handle = await fs.open(filePath, "r");
  try {
    const buffer = Buffer.alloc(length);
    await handle.read(buffer, 0, length, stat.size - length);
    let text = buffer.toString("utf8");
    if (stat.size > length) {
      const firstNewline = text.indexOf("\n");
      text = firstNewline >= 0 ? text.slice(firstNewline + 1) : "";
      // 末尾から読んだ場合はヘッダーを補うため、先頭だけ別途取得する。
      const head = Buffer.alloc(Math.min(4096, stat.size));
      await handle.read(head, 0, head.length, 0);
      const header = head.toString("utf8").split(/\r?\n/, 1)[0];
      text = `${header}\n${text}`;
    }
    return text;
  } finally {
    await handle.close();
  }
}

async function listFiles(directory: string, pattern: RegExp) {
  try {
    return (await fs.readdir(directory)).filter((name) => pattern.test(name));
  } catch {
    return [];
  }
}

async function readStates(): Promise<{ state: BotState; filePath: string; mtime: number }[]> {
  const names = await listFiles(DATA_DIR, /^state_[A-Za-z0-9_]+_(dry-run|live)\.json$/);
  const candidates = await Promise.all(
    names.map(async (name) => {
      const filePath = path.join(DATA_DIR, name);
      try {
        const stat = await fs.stat(filePath);
        return { filePath, mtime: stat.mtimeMs };
      } catch {
        return null;
      }
    }),
  );
  const readable = await Promise.all(
    candidates
      .filter((item): item is { filePath: string; mtime: number } => item !== null)
      .map(async (item) => {
        try {
          const state = JSON.parse(await fs.readFile(item.filePath, "utf8")) as BotState;
          return { ...item, state };
        } catch {
          return null;
        }
      }),
  );
  return readable
    .filter((item): item is { state: BotState; filePath: string; mtime: number } => item !== null)
    .sort((a, b) => b.mtime - a.mtime);
}

async function readActivities(symbol?: string): Promise<ActivityItem[]> {
  const names = (await listFiles(LOG_DIR, /^decisions_\d{8}\.csv$/)).sort().reverse().slice(0, 8);
  const batches = await Promise.all(
    names.map(async (name) => {
      try {
        const rows = parseCsv(await readTail(path.join(LOG_DIR, name)));
        return rows.map((row, index): ActivityItem => ({
          id: `${name}-${row.symbol}-${row.timestamp_jst}-${index}`,
          symbol: row.symbol || "",
          timestamp: row.timestamp_jst || "",
          event: row.event || "UNKNOWN",
          side: row.side || "",
          price: nullableNumber(row.order_price),
          size: nullableNumber(row.order_size),
          orderId: row.order_id || "",
          reason: row.reason || "",
          result: row.result || "",
          equity: nullableNumber(row.equity_jpy),
        }));
      } catch {
        return [];
      }
    }),
  );
  return batches
    .flat()
    .filter((row) => row.timestamp && (!symbol || row.symbol === symbol))
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
    .slice(0, 500);
}

async function readPublicSettings() {
  const defaults = { maxSpreadRate: 0.001, riskPerTradeRate: 0.01, dailyLossLimitRate: 0.05, loopSeconds: 3600 };
  try {
    // .env全体ではなく、画面に必要な非秘密の4項目だけを利用する。
    const parsed = parseEnv(await fs.readFile(ENV_FILE));
    return {
      maxSpreadRate: numberValue(parsed.MAX_SPREAD_RATE, defaults.maxSpreadRate),
      riskPerTradeRate: numberValue(parsed.RISK_PER_TRADE_RATE, defaults.riskPerTradeRate),
      dailyLossLimitRate: numberValue(parsed.DAILY_LOSS_LIMIT_RATE, defaults.dailyLossLimitRate),
      loopSeconds: numberValue(parsed.LOOP_INTERVAL_SECONDS, defaults.loopSeconds),
    };
  } catch {
    return defaults;
  }
}

async function systemStatus(): Promise<DashboardSnapshot["system"]> {
  let cpuTemperature: number | null = null;
  try {
    const thermal = await fs.readFile("/sys/class/thermal/thermal_zone0/temp", "utf8");
    cpuTemperature = numberValue(thermal.trim()) / 1000;
  } catch {
    // Raspberry Pi以外や権限がない環境では温度を表示しない。
  }
  return {
    memoryUsedPercent: ((os.totalmem() - os.freemem()) / os.totalmem()) * 100,
    cpuTemperature,
    uptimeSeconds: os.uptime(),
  };
}

function equitySeries(activities: ActivityItem[]): EquityPoint[] {
  return activities
    .filter((row) => row.event === "MARKET" && row.equity !== null)
    .map((row) => ({ time: row.timestamp, equity: row.equity as number }))
    .reverse();
}


function demoSnapshot(): DashboardSnapshot {
  const now = Date.now();
  const equityHistory: EquityPoint[] = Array.from({ length: 168 }, (_, index) => {
    const wave = Math.sin(index / 8) * 28000 + Math.sin(index / 19) * 13000;
    return {
      time: new Date(now - (167 - index) * 60 * 60 * 1000).toISOString(),
      equity: Math.round(1_000_000 + index * 380 + wave),
    };
  });
  const templates = [
    ["HOLD", "EMA短期=12,649,154.55, EMA長期=12,656,701.11, RSI=25.75", "position=0"],
    ["MARKET", "1分足・板・指標を更新", "evaluated"],
    ["SKIP", "スプレッド上限", "0.1042% >= 0.1000%"],
    ["ORDER", "ENTRY: EMAゴールデンクロス; 往復コスト考慮", "accepted (simulated)"],
    ["FILL", "ENTRY: EMAゴールデンクロス", "fee=632.17, realized=0"],
    ["ORDER_STATUS", "次サイクルの注文状態を確認", "status=EXECUTED"],
    ["MARKET", "1分足・板・指標を更新", "evaluated"],
  ] as const;
  const activities: ActivityItem[] = Array.from({ length: 42 }, (_, index) => {
    const template = templates[index % templates.length];
    const isOrder = template[0] === "ORDER" || template[0] === "FILL";
    return {
      id: `demo-${index}`,
      symbol: "BTC_JPY",
      timestamp: new Date(now - index * 60 * 60 * 1000).toISOString(),
      event: template[0],
      side: isOrder ? "BUY" : "",
      price: isOrder ? 12_643_439 : null,
      size: isOrder ? 0.0078 : null,
      orderId: isOrder ? `dry-demo-${String(index).padStart(3, "0")}` : "",
      reason: template[1],
      result: template[2],
      equity: 1_102_400 - index * 190,
    };
  });
  return {
    source: "demo",
    generatedAt: new Date(now).toISOString(),
    bot: {
      symbol: "BTC_JPY",
      availableSymbols: ["BTC_JPY"],
      mode: "dry-run",
      health: "running",
      updatedAt: new Date(now - 90_000).toISOString(),
      loopSeconds: 3600,
      dailyHalted: false,
    },
    account: {
      equity: 1_102_400,
      cash: 998_140.22,
      positionSize: 0.0082,
      positionSide: "BUY",
      positionId: "demo-position",
      positionEntryPrice: 12_643_439,
      positionMargin: 51_851.31,
      availableBalance: 946_288.91,
      positionCost: 103_702.61,
      positionValue: 103_677.02,
      unrealizedPnl: -25.59,
      dailyRealizedPnl: 4_286.4,
      totalRealizedPnl: 68_912.45,
      dayStartEquity: 1_098_113.6,
      dailyLoss: 0,
      dailyLossLimit: 54_905.68,
    },
    market: {
      mid: 12_643_438.5,
      bestBid: 12_643_438,
      bestAsk: 12_643_439,
      spread: 1,
      spreadRate: 0.0000000791,
      emaShort: 12_649_154.55,
      emaLong: 12_656_701.11,
      rsi: 25.75,
      smaShort: null,
      smaLong: null,
      reason: "EMA短期 < EMA長期。RSI売られすぎ水準ですが、上昇トレンド条件を待機中",
    },
    settings: { maxSpreadRate: 0.001, riskPerTradeRate: 0.01, dailyLossLimitRate: 0.05 },
    pendingOrder: null,
    equityHistory,
    activities,
    system: null,
  };
}

export async function getDashboardSnapshot(symbol?: string): Promise<DashboardSnapshot> {
  const states = await readStates();
  if (!states.length) return demoSnapshot();
  const latest = states.find((item) => item.state.symbol === symbol) || states[0];

  const { state } = latest;
  const [activities, settings, system] = await Promise.all([
    readActivities(state.symbol),
    readPublicSettings(),
    systemStatus(),
  ]);
  const latestMarket = activities.find((row) => row.event === "MARKET" && row.symbol === state.symbol);
  const latestRows = await (async () => {
    // 市況の数値はAPI応答用に最新MARKET行だけを再取得せず、活動履歴とは別に保持する。
    const names = (await listFiles(LOG_DIR, /^decisions_\d{8}\.csv$/)).sort().reverse().slice(0, 2);
    for (const name of names) {
      try {
        const rows = parseCsv(await readTail(path.join(LOG_DIR, name)));
        const market = rows.reverse().find((row) => row.event === "MARKET" && row.symbol === state.symbol);
        if (market) return market;
      } catch {
        // 次の日別ファイルを確認する。
      }
    }
    return null;
  })();

  const mid = numberValue(latestRows?.mid_price);
  const cash = numberValue(state.cash);
  const positionSize = numberValue(state.position_size);
  const positionCost = numberValue(state.position_cost);
  const positionSide = stringValue(state.position_side);
  const positionEntryPrice = numberValue(state.position_entry_price);
  const positionValue = positionSize * mid;
  const direction = positionSide === "SELL" ? -1 : 1;
  const unrealizedPnl = positionSize > 0
    ? (mid - positionEntryPrice) * positionSize * direction
    : 0;
  const equity = nullableNumber(latestMarket?.equity) ?? numberValue(state.account_equity, cash + unrealizedPnl);
  const dayStartEquity = numberValue(state.day_start_equity, equity);
  const dailyLoss = Math.max(0, dayStartEquity - equity);
  const updatedAt = stringValue(state.updated_at, latestMarket?.timestamp || new Date().toISOString());
  const ageSeconds = Math.max(0, (Date.now() - new Date(updatedAt).getTime()) / 1000);
  const dailyHalted = Boolean(state.daily_halted);
  const health = dailyHalted ? "halted" : ageSeconds <= Math.max(900, settings.loopSeconds * 1.5) ? "running" : "stale";
  const pending = state.pending_order && typeof state.pending_order === "object"
    ? state.pending_order as Record<string, unknown>
    : null;

  return {
    source: "bot",
    generatedAt: new Date().toISOString(),
    bot: {
      symbol: state.symbol,
      availableSymbols: [...new Set(states.map((item) => item.state.symbol))],
      mode: state.mode,
      health,
      updatedAt,
      loopSeconds: settings.loopSeconds,
      dailyHalted,
    },
    account: {
      equity,
      cash,
      positionSize,
      positionSide,
      positionId: stringValue(state.position_id),
      positionEntryPrice,
      positionMargin: numberValue(state.position_margin),
      availableBalance: numberValue(state.available_balance, cash),
      positionCost,
      positionValue,
      unrealizedPnl,
      dailyRealizedPnl: numberValue(state.daily_realized_pnl),
      totalRealizedPnl: numberValue(state.total_realized_pnl),
      dayStartEquity,
      dailyLoss,
      dailyLossLimit: dayStartEquity * settings.dailyLossLimitRate,
    },
    market: latestRows ? {
      mid,
      bestBid: numberValue(latestRows.best_bid),
      bestAsk: numberValue(latestRows.best_ask),
      spread: numberValue(latestRows.spread),
      spreadRate: numberValue(latestRows.spread_rate),
      emaShort: nullableNumber(latestRows.ema_short),
      emaLong: nullableNumber(latestRows.ema_long),
      rsi: nullableNumber(latestRows.rsi),
      smaShort: nullableNumber(latestRows.reason?.match(/SMA短期=([\d.]+)/)?.[1]),
      smaLong: nullableNumber(latestRows.reason?.match(/SMA長期=([\d.]+)/)?.[1]),
      reason: latestRows.reason || "",
    } : null,
    settings: {
      maxSpreadRate: settings.maxSpreadRate,
      riskPerTradeRate: settings.riskPerTradeRate,
      dailyLossLimitRate: settings.dailyLossLimitRate,
    },
    pendingOrder: pending ? {
      orderId: stringValue(pending.order_id, stringValue(pending.client_order_id, "照会中")),
      side: stringValue(pending.side),
      settleType: stringValue(pending.settle_type, "OPEN"),
      price: numberValue(pending.price),
      size: numberValue(pending.size),
      placedAt: stringValue(pending.placed_at),
      reason: stringValue(pending.reason),
    } : null,
    equityHistory: equitySeries(activities),
    activities,
    system,
  };
}
