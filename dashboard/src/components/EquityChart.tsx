import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { EquityPoint } from "@contracts/dashboard";
import { formatJpy, formatTime } from "@/lib/format";

type Range = "24H" | "7D";

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const [range, setRange] = useState<Range>("24H");
  const filtered = useMemo(() => {
    const cutoff = Date.now() - (range === "24H" ? 24 : 24 * 7) * 60 * 60 * 1000;
    const selected = points.filter((point) => new Date(point.time).getTime() >= cutoff);
    return selected.length >= 2 ? selected : points.slice(-Math.min(points.length, range === "24H" ? 24 : 168));
  }, [points, range]);
  const change = filtered.length > 1 ? filtered.at(-1)!.equity - filtered[0].equity : 0;

  return (
    <section aria-labelledby="equity-chart-title" className="panel chart-panel">
      <div className="panel-heading chart-heading">
        <div>
          <p className="section-label">EQUITY CURVE</p>
          <h2 id="equity-chart-title">評価資産の推移</h2>
        </div>
        <div className="range-switch" aria-label="チャート期間">
          {(["24H", "7D"] as Range[]).map((item) => (
            <button
              key={item}
              type="button"
              className={range === item ? "range-button active" : "range-button"}
              aria-pressed={range === item}
              onClick={() => setRange(item)}
            >
              {item}
            </button>
          ))}
        </div>
      </div>
      <div className="chart-summary" aria-live="polite">
        <span>{range === "24H" ? "過去24時間" : "過去7日間"}</span>
        <strong className={change >= 0 ? "positive" : "negative"}>
          {change >= 0 ? "+" : ""}{formatJpy(change)}
        </strong>
      </div>
      {filtered.length >= 2 ? (
        <div className="chart-canvas" aria-label={`評価資産チャート。期間中の変化は${formatJpy(change)}です`}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={filtered} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="oklch(0.36 0.018 165)" vertical={false} strokeDasharray="2 6" />
              <XAxis
                dataKey="time"
                tickFormatter={(value: string) => formatTime(value, range === "7D")}
                tick={{ fill: "oklch(0.76 0.015 130)", fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                minTickGap={36}
              />
              <YAxis
                domain={["auto", "auto"]}
                tickFormatter={(value: number) => `${Math.round(value / 10000).toLocaleString()}万`}
                tick={{ fill: "oklch(0.76 0.015 130)", fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={48}
              />
              <Tooltip
                cursor={{ stroke: "oklch(0.68 0.04 155)", strokeWidth: 1 }}
                contentStyle={{
                  background: "oklch(0.215 0.016 165)",
                  border: "1px solid oklch(0.38 0.025 165)",
                  borderRadius: "6px",
                  color: "oklch(0.94 0.008 105)",
                  fontSize: "12px",
                }}
                labelFormatter={(value) => formatTime(String(value), true)}
                formatter={(value) => [formatJpy(Number(value)), "評価資産"]}
              />
              <Area
                type="monotone"
                dataKey="equity"
                stroke="oklch(0.82 0.105 155)"
                strokeWidth={2}
                fill="oklch(0.82 0.105 155)"
                fillOpacity={0.09}
                activeDot={{ r: 4, fill: "oklch(0.82 0.105 155)", stroke: "oklch(0.155 0.014 165)" }}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="chart-empty">
          <p>まだ十分な資産データがありません。</p>
          <span>ボットのMARKETログが2件以上記録されると表示されます。</span>
        </div>
      )}
    </section>
  );
}
