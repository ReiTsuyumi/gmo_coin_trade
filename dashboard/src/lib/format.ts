import type { ActivityItem } from "@contracts/dashboard";

export function sortActivitiesNewestFirst<T extends Pick<ActivityItem, "timestamp">>(items: T[]): T[] {
  return [...items].sort((a, b) => {
    const aTime = Date.parse(a.timestamp);
    const bTime = Date.parse(b.timestamp);
    if (!Number.isFinite(aTime)) return Number.isFinite(bTime) ? 1 : 0;
    if (!Number.isFinite(bTime)) return -1;
    return bTime - aTime;
  });
}

export function formatJpy(value: number, maximumFractionDigits = 0) {
  return new Intl.NumberFormat("ja-JP", {
    style: "currency",
    currency: "JPY",
    maximumFractionDigits,
  }).format(value);
}

export function formatNumber(value: number, maximumFractionDigits = 2) {
  return new Intl.NumberFormat("ja-JP", { maximumFractionDigits }).format(value);
}

export function formatBtc(value: number) {
  return `${new Intl.NumberFormat("ja-JP", {
    minimumFractionDigits: 4,
    maximumFractionDigits: 8,
  }).format(value)} BTC`;
}

export function formatPercent(value: number, digits = 2) {
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatTime(value: string, withDate = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "時刻不明";
  return new Intl.DateTimeFormat("ja-JP", {
    month: withDate ? "numeric" : undefined,
    day: withDate ? "numeric" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: withDate ? undefined : "2-digit",
  }).format(date);
}

export function relativeTime(value: string) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}時間前`;
  return `${Math.floor(seconds / 86400)}日前`;
}

export const EVENT_LABELS: Record<string, string> = {
  MARKET: "市況更新",
  HOLD: "様子見",
  SKIP: "スキップ",
  ORDER: "指値注文",
  FILL: "約定",
  ORDER_CLOSE: "注文完了",
  ORDER_STATUS: "注文照会",
  ORDER_UNKNOWN: "注文確認中",
  CANCEL_REQUEST: "取消要求",
  DAILY_HALT: "日次停止",
  ERROR: "エラー",
};
