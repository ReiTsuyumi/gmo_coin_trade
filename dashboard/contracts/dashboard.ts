export type BotHealth = "running" | "stale" | "halted";
export type DataSource = "bot" | "demo";

export type EquityPoint = {
  time: string;
  equity: number;
};

export type ActivityItem = {
  id: string;
  symbol: string;
  timestamp: string;
  event: string;
  side: string;
  price: number | null;
  size: number | null;
  orderId: string;
  reason: string;
  result: string;
  equity: number | null;
};

export type PendingOrderView = {
  orderId: string;
  side: string;
  settleType: string;
  price: number;
  size: number;
  placedAt: string;
  reason: string;
};

export type DashboardSnapshot = {
  source: DataSource;
  generatedAt: string;
  bot: {
    symbol: string;
    availableSymbols: string[];
    mode: string;
    health: BotHealth;
    updatedAt: string;
    loopSeconds: number;
    dailyHalted: boolean;
  };
  account: {
    equity: number;
    cash: number;
    positionSize: number;
    positionSide: string;
    positionId: string;
    positionEntryPrice: number;
    positionMargin: number;
    availableBalance: number;
    positionCost: number;
    positionValue: number;
    unrealizedPnl: number;
    dailyRealizedPnl: number;
    totalRealizedPnl: number;
    dayStartEquity: number;
    dailyLoss: number;
    dailyLossLimit: number;
  };
  market: {
    mid: number;
    bestBid: number;
    bestAsk: number;
    spread: number;
    spreadRate: number;
    emaShort: number | null;
    emaLong: number | null;
    rsi: number | null;
    smaShort: number | null;
    smaLong: number | null;
    reason: string;
  } | null;
  settings: {
    maxSpreadRate: number;
    riskPerTradeRate: number;
    dailyLossLimitRate: number;
  };
  pendingOrder: PendingOrderView | null;
  equityHistory: EquityPoint[];
  activities: ActivityItem[];
  system: {
    memoryUsedPercent: number;
    cpuTemperature: number | null;
    uptimeSeconds: number;
  } | null;
};
