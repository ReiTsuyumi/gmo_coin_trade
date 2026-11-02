import { useMemo, useState } from "react";
import { ArrowDownRight, ArrowUpRight, ChevronRight, Inbox } from "lucide-react";
import type { ActivityItem } from "@contracts/dashboard";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EVENT_LABELS, formatBtc, formatJpy, formatTime } from "@/lib/format";

type Filter = "all" | "orders" | "decisions";
const ORDER_EVENTS = new Set(["ORDER", "FILL", "ORDER_CLOSE", "ORDER_STATUS", "ORDER_UNKNOWN", "CANCEL_REQUEST"]);

function eventTone(event: string) {
  if (event === "FILL") return "fill";
  if (event === "ERROR" || event === "DAILY_HALT") return "danger";
  if (event === "SKIP") return "warning";
  return "neutral";
}

export function ActivityFeed({
  items,
  compact = false,
  onShowAll,
}: {
  items: ActivityItem[];
  compact?: boolean;
  onShowAll?: () => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<ActivityItem | null>(null);
  const filtered = useMemo(() => {
    const rows = filter === "all"
      ? items
      : filter === "orders"
        ? items.filter((item) => ORDER_EVENTS.has(item.event))
        : items.filter((item) => !ORDER_EVENTS.has(item.event) && item.event !== "MARKET");
    return compact ? rows.slice(0, 8) : rows.slice(0, 200);
  }, [compact, filter, items]);

  return (
    <section aria-labelledby={compact ? "recent-activity-title" : "activity-log-title"} className="panel activity-panel">
      <div className="panel-heading activity-heading">
        <div>
          <p className="section-label">AUDIT TRAIL</p>
          <h2 id={compact ? "recent-activity-title" : "activity-log-title"}>
            {compact ? "最近のアクティビティ" : "判断・注文ログ"}
          </h2>
        </div>
        {compact && onShowAll ? (
          <button type="button" className="text-button" onClick={onShowAll}>
            すべて見る <ChevronRight size={16} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {!compact ? (
        <div className="activity-filters" aria-label="ログの絞り込み">
          {([
            ["all", "すべて"],
            ["orders", "注文・約定"],
            ["decisions", "判断"],
          ] as [Filter, string][]).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={filter === value ? "filter-button active" : "filter-button"}
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
      ) : null}
      {filtered.length ? (
        <div className="activity-list">
          {filtered.map((item) => (
            <button
              type="button"
              className="activity-row"
              key={item.id}
              onClick={() => setSelected(item)}
              aria-label={`${formatTime(item.timestamp, true)} ${EVENT_LABELS[item.event] || item.event}の詳細`}
            >
              <span className="activity-time">{formatTime(item.timestamp)}</span>
              <span className={`event-label ${eventTone(item.event)}`}>
                {item.side === "BUY" ? <ArrowUpRight size={15} aria-hidden="true" /> : null}
                {item.side === "SELL" ? <ArrowDownRight size={15} aria-hidden="true" /> : null}
                {item.symbol ? `${item.symbol} · ` : ""}{EVENT_LABELS[item.event] || item.event}
              </span>
              <span className="activity-result">
                {item.price !== null ? formatJpy(item.price) : item.result || item.reason || "—"}
              </span>
              <ChevronRight className="activity-chevron" size={16} aria-hidden="true" />
            </button>
          ))}
        </div>
      ) : (
        <div className="empty-state">
          <Inbox size={24} aria-hidden="true" />
          <p>この条件のログはまだありません。</p>
        </div>
      )}

      <Dialog open={selected !== null} onOpenChange={(open) => { if (!open) setSelected(null); }}>
        <DialogContent className="activity-dialog">
          {selected ? (
            <>
              <DialogHeader>
                <DialogTitle>{EVENT_LABELS[selected.event] || selected.event}</DialogTitle>
                <DialogDescription>{formatTime(selected.timestamp, true)}</DialogDescription>
              </DialogHeader>
              <dl className="detail-list">
                {selected.symbol ? <div><dt>銘柄</dt><dd>{selected.symbol}/JPY</dd></div> : null}
                {selected.side ? <div><dt>売買</dt><dd>{selected.side === "BUY" ? "買い" : "売り"}</dd></div> : null}
                {selected.price !== null ? <div><dt>価格</dt><dd>{formatJpy(selected.price)}</dd></div> : null}
                {selected.size !== null ? <div><dt>数量</dt><dd>{formatBtc(selected.size)}</dd></div> : null}
                {selected.equity !== null ? <div><dt>評価資産</dt><dd>{formatJpy(selected.equity)}</dd></div> : null}
                {selected.orderId ? <div className="detail-wide"><dt>注文ID</dt><dd>{selected.orderId}</dd></div> : null}
                <div className="detail-wide"><dt>判断理由</dt><dd>{selected.reason || "記録なし"}</dd></div>
                <div className="detail-wide"><dt>結果</dt><dd>{selected.result || "記録なし"}</dd></div>
              </dl>
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </section>
  );
}
