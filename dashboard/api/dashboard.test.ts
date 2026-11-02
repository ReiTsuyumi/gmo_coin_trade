import { describe, expect, it } from "vitest";
import { getDashboardSnapshot, parseCsv } from "./dashboard";

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
    const snapshot = await getDashboardSnapshot();
    expect(snapshot.source).toBe("demo");
    expect(snapshot.activities.length).toBeGreaterThan(0);
    expect(snapshot.equityHistory.length).toBeGreaterThan(24);
  });
});
