import { lazy, Suspense, useState, type ReactNode } from "react";
import {
  Activity,
  Bot,
  CircleAlert,
  CircleCheck,
  Clock3,
  FlaskConical,
  LayoutDashboard,
  Pause,
  Play,
  RefreshCw,
  ScrollText,
  Server,
  ShieldCheck,
  WifiOff,
} from "lucide-react";
import { trpc } from "@/providers/trpc";
import { ActivityFeed } from "@/components/ActivityFeed";
import type { BotHealth, DashboardSnapshot } from "@contracts/dashboard";
import {
  formatBtc,
  formatJpy,
  formatNumber,
  formatPercent,
  formatTime,
  relativeTime,
} from "@/lib/format";

const EquityChart = lazy(() => import("@/components/EquityChart").then((module) => ({ default: module.EquityChart })));

const HEALTH: Record<BotHealth, { label: string; detail: string }> = {
  running: { label: "稼働中", detail: "状態ファイルは定期更新されています" },
  stale: { label: "更新停止", detail: "最終更新から時間が経過しています" },
  halted: { label: "取引停止", detail: "日次損失上限により停止しています" },
};

function Metric({
  label,
  value,
  detail,
  accent,
  large = false,
}: {
  label: string;
  value: ReactNode;
  detail: ReactNode;
  accent?: "positive" | "negative";
  large?: boolean;
}) {
  return (
    <section className={large ? "metric-card metric-primary" : "metric-card"}>
      <p className="metric-label">{label}</p>
      <p className={large ? "metric-value large" : "metric-value"}>{value}</p>
      <div className={accent ? `metric-detail ${accent}` : "metric-detail"}>{detail}</div>
    </section>
  );
}

function Navigation({ view, onChange }: { view: "overview" | "activity"; onChange: (view: "overview" | "activity") => void }) {
  return (
    <>
      <button
        type="button"
        className={view === "overview" ? "nav-item active" : "nav-item"}
        aria-current={view === "overview" ? "page" : undefined}
        onClick={() => onChange("overview")}
      >
        <LayoutDashboard size={19} aria-hidden="true" /><span>概要</span>
      </button>
      <button
        type="button"
        className={view === "activity" ? "nav-item active" : "nav-item"}
        aria-current={view === "activity" ? "page" : undefined}
        onClick={() => onChange("activity")}
      >
        <ScrollText size={19} aria-hidden="true" /><span>判断・注文ログ</span>
      </button>
    </>
  );
}

function RiskPanel({ data }: { data: DashboardSnapshot }) {
  const lossRatio = data.account.dailyLossLimit > 0
    ? Math.min(100, (data.account.dailyLoss / data.account.dailyLossLimit) * 100)
    : 0;
  const spreadRatio = Math.min(100, (data.market?.spreadRate || 0) / data.settings.maxSpreadRate * 100);
  return (
    <section className="panel risk-panel" aria-labelledby="risk-panel-title">
      <div className="panel-heading">
        <div>
          <p className="section-label">RISK & MARKET</p>
          <h2 id="risk-panel-title">リスク監視</h2>
        </div>
        <ShieldCheck size={20} aria-hidden="true" />
      </div>
      <div className="risk-block">
        <div className="risk-line">
          <span>本日の評価損失</span>
          <strong>{formatJpy(data.account.dailyLoss)}</strong>
        </div>
        <div className="meter" role="meter" aria-label="日次損失上限の使用率" aria-valuenow={Math.round(lossRatio)} aria-valuemin={0} aria-valuemax={100}>
          <span className={lossRatio >= 80 ? "meter-fill danger" : "meter-fill"} style={{ width: `${lossRatio}%` }} />
        </div>
        <div className="risk-caption">
          <span>上限 {formatJpy(data.account.dailyLossLimit)}</span>
          <span>{lossRatio.toFixed(1)}%</span>
        </div>
      </div>
      <div className="risk-block">
        <div className="risk-line">
          <span>スプレッド</span>
          <strong>{data.market ? formatPercent(data.market.spreadRate, 4) : "—"}</strong>
        </div>
        <div className="meter" role="meter" aria-label="スプレッド上限の使用率" aria-valuenow={Math.round(spreadRatio)} aria-valuemin={0} aria-valuemax={100}>
          <span className={spreadRatio >= 100 ? "meter-fill danger" : "meter-fill"} style={{ width: `${spreadRatio}%` }} />
        </div>
        <div className="risk-caption">
          <span>発注上限 {formatPercent(data.settings.maxSpreadRate, 2)}</span>
          <span>{data.market ? formatJpy(data.market.spread) : "—"}</span>
        </div>
      </div>
      <dl className="market-facts">
        {data.market?.smaShort !== null && data.market?.smaShort !== undefined ? (
          <>
            <div><dt>平均足</dt><dd>方向確認</dd></div>
            <div><dt>短期 SMA</dt><dd>{formatJpy(data.market.smaShort)}</dd></div>
            <div><dt>長期 SMA</dt><dd>{data.market.smaLong !== null ? formatJpy(data.market.smaLong) : "—"}</dd></div>
          </>
        ) : (
          <>
            <div><dt>RSI (14)</dt><dd>{data.market?.rsi !== null && data.market?.rsi !== undefined ? data.market.rsi.toFixed(2) : "—"}</dd></div>
            <div><dt>短期 EMA</dt><dd>{data.market?.emaShort !== null && data.market?.emaShort !== undefined ? formatJpy(data.market.emaShort) : "—"}</dd></div>
            <div><dt>長期 EMA</dt><dd>{data.market?.emaLong !== null && data.market?.emaLong !== undefined ? formatJpy(data.market.emaLong) : "—"}</dd></div>
          </>
        )}
        <div><dt>1取引リスク</dt><dd>{formatPercent(data.settings.riskPerTradeRate)}</dd></div>
      </dl>
      {data.system ? (
        <div className="system-line">
          <Server size={16} aria-hidden="true" />
          <span>メモリ {data.system.memoryUsedPercent.toFixed(0)}%</span>
          {data.system.cpuTemperature !== null ? <span>CPU {data.system.cpuTemperature.toFixed(1)}°C</span> : null}
        </div>
      ) : null}
    </section>
  );
}

function Overview({ data, onShowActivity }: { data: DashboardSnapshot; onShowActivity: () => void }) {
  const dailyPositive = data.account.dailyRealizedPnl >= 0;
  return (
    <div className="dashboard-grid">
      <div className="metrics-grid">
        <Metric
          large
          label="評価資産"
          value={formatJpy(data.account.equity)}
          detail={<span>累計実現損益 <strong>{data.account.totalRealizedPnl >= 0 ? "+" : ""}{formatJpy(data.account.totalRealizedPnl)}</strong></span>}
        />
        <Metric
          label="本日の実現損益"
          value={<span className={dailyPositive ? "positive" : "negative"}>{dailyPositive ? "+" : ""}{formatJpy(data.account.dailyRealizedPnl)}</span>}
          detail={`日次開始資産 ${formatJpy(data.account.dayStartEquity)}`}
        />
        <Metric
          label="現在値 / MID"
          value={data.market ? formatJpy(data.market.mid) : "—"}
          detail={data.market ? `BID ${formatNumber(data.market.bestBid)} / ASK ${formatNumber(data.market.bestAsk)}` : "市況ログを待っています"}
        />
        <Metric
          label={data.account.positionSize > 0 ? `${data.account.positionSide === "SELL" ? "売り" : "買い"}建玉` : "建玉"}
          value={data.account.positionSize > 0 ? formatBtc(data.account.positionSize) : "なし"}
          detail={data.account.positionSize > 0 ? (
            <span>建玉単価 {formatJpy(data.account.positionEntryPrice)} / 含み損益 <strong className={data.account.unrealizedPnl >= 0 ? "positive" : "negative"}>{data.account.unrealizedPnl >= 0 ? "+" : ""}{formatJpy(data.account.unrealizedPnl, 2)}</strong></span>
          ) : `取引余力 ${formatJpy(data.account.availableBalance)}`}
        />
      </div>

      {data.pendingOrder ? (
        <section className="pending-order" aria-label="未完了の注文">
          <Activity size={20} aria-hidden="true" />
          <div>
            <strong>未完了の{data.pendingOrder.settleType === "CLOSE" ? "決済" : "新規"}{data.pendingOrder.side === "BUY" ? "買い" : "売り"}指値</strong>
            <span>{formatJpy(data.pendingOrder.price)} × {formatBtc(data.pendingOrder.size)}</span>
          </div>
          <span className="pending-id">{data.pendingOrder.orderId || "注文IDを照会中"}</span>
        </section>
      ) : null}

      <div className="analysis-grid">
        <Suspense fallback={<div className="panel chart-empty">チャートを読み込んでいます…</div>}>
          <EquityChart points={data.equityHistory} />
        </Suspense>
        <RiskPanel data={data} />
      </div>

      <ActivityFeed items={data.activities} compact onShowAll={onShowActivity} />
    </div>
  );
}

export default function Home() {
  const [view, setView] = useState<"overview" | "activity">("overview");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selectedSymbol, setSelectedSymbol] = useState<string>();
  const query = trpc.dashboard.snapshot.useQuery({ symbol: selectedSymbol }, {
    refetchInterval: autoRefresh ? 10_000 : false,
    refetchOnWindowFocus: true,
    retry: 1,
  });
  const data = query.data;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <Bot size={25} strokeWidth={1.7} aria-hidden="true" />
          <div><strong>PI / TRADE</strong><span>MONITORING CONSOLE</span></div>
        </div>
        <nav className="side-navigation" aria-label="メインメニュー">
          <Navigation view={view} onChange={setView} />
        </nav>
        <div className="sidebar-foot">
          <ShieldCheck size={18} aria-hidden="true" />
          <p><strong>監視専用</strong><span>売買操作はできません</span></p>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div>
            <div className="title-line">
              <h1>{view === "overview" ? "運用ダッシュボード" : "判断・注文ログ"}</h1>
              {data ? <span className={`mode-badge ${data.bot.mode === "live" ? "live" : ""}`}>{data.bot.mode === "live" ? "LIVE" : "テストモード"}</span> : null}
            </div>
            {data ? (
              <div className="symbol-selector">
                <select
                  aria-label="表示する銘柄"
                  value={data.bot.symbol}
                  onChange={(event) => setSelectedSymbol(event.target.value)}
                >
                  {data.bot.availableSymbols.map((symbol) => (
                    <option key={symbol} value={symbol}>{symbol}</option>
                  ))}
                </select>
                <span>GMOコイン・レバレッジ / 余力 {formatJpy(data.account.availableBalance)}</span>
              </div>
            ) : <p>ボットの状態を読み込んでいます</p>}
          </div>
          <div className="header-actions">
            <button
              type="button"
              className={autoRefresh ? "icon-text-button selected" : "icon-text-button"}
              aria-pressed={autoRefresh}
              aria-label={autoRefresh ? "自動更新中" : "自動更新OFF"}
              onClick={() => setAutoRefresh((current) => !current)}
            >
              {autoRefresh ? <Pause size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}
              <span>{autoRefresh ? "自動更新中" : "自動更新OFF"}</span>
            </button>
            <button
              type="button"
              className="refresh-button"
              onClick={() => void query.refetch()}
              disabled={query.isFetching}
              aria-label="今すぐ更新"
            >
              <RefreshCw size={18} className={query.isFetching ? "spin" : ""} aria-hidden="true" />
              <span>更新</span>
            </button>
          </div>
        </header>

        {data?.source === "demo" ? (
          <div className="demo-banner" role="status">
            <FlaskConical size={18} aria-hidden="true" />
            <p><strong>デモデータを表示中</strong><span>ラズパイでボットのdata/とlogs/に接続すると実データへ切り替わります。</span></p>
          </div>
        ) : null}

        {data ? (
          <div className={`connection-bar ${data.bot.health}`}>
            <span className="status-dot" aria-hidden="true" />
            <strong>{HEALTH[data.bot.health].label}</strong>
            <span>{HEALTH[data.bot.health].detail}</span>
            <span className="last-update"><Clock3 size={14} aria-hidden="true" />{relativeTime(data.bot.updatedAt)}に更新</span>
          </div>
        ) : null}

        {query.isLoading ? (
          <div className="loading-grid" aria-label="読み込み中" aria-busy="true">
            <div className="skeleton skeleton-metrics" /><div className="skeleton skeleton-chart" /><div className="skeleton skeleton-list" />
          </div>
        ) : query.isError ? (
          <section className="error-state" role="alert">
            <WifiOff size={28} aria-hidden="true" />
            <h2>監視サーバーに接続できません</h2>
            <p>ラズパイ上でダッシュボードが起動しているか、同じネットワークに接続しているか確認してください。</p>
            <button type="button" className="primary-button" onClick={() => void query.refetch()}>再接続</button>
          </section>
        ) : data ? (
          view === "overview"
            ? <Overview data={data} onShowActivity={() => setView("activity")} />
            : <ActivityFeed items={data.activities} />
        ) : null}

        <footer className="dashboard-footer">
          <span><CircleCheck size={14} aria-hidden="true" />読み取り専用API</span>
          <span>最終画面更新 {data ? formatTime(data.generatedAt) : "—"}</span>
          <span><CircleAlert size={14} aria-hidden="true" />稼働推定は状態ファイルの更新時刻に基づきます</span>
        </footer>
      </main>

      <nav className="mobile-navigation" aria-label="モバイルメニュー">
        <Navigation view={view} onChange={setView} />
      </nav>
    </div>
  );
}
