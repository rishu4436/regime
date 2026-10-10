import { useEffect, useRef } from "react";
import {
  ColorType, CrosshairMode, createChart, type IChartApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, EquityPoint } from "../contracts";

export interface TradeMarker { time: string; type: "BUY" | "SELL"; text?: string }

const ts = (iso: string) => Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;

function baseChart(el: HTMLElement, height: number): IChartApi {
  return createChart(el, {
    height,
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#8b93a7", fontFamily: "Inter Variable, system-ui, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
    timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
    crosshair: { mode: CrosshairMode.Normal },
    handleScroll: true, handleScale: true,
  });
}

/** Candlestick chart (TradingView lightweight-charts) with BUY/SELL markers snapped to bars. */
export function PriceChart({ candles, markers = [], height = 320 }: { candles: Candle[]; markers?: TradeMarker[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ReturnType<IChartApi["addCandlestickSeries"]> | null>(null);
  const fitted = useRef(0);

  useEffect(() => {
    if (!ref.current) return;
    chart.current = baseChart(ref.current, height);
    series.current = chart.current.addCandlestickSeries({
      upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e88", wickDownColor: "#ef444488",
    });
    return () => { chart.current?.remove(); chart.current = null; series.current = null; fitted.current = 0; };
  }, [height]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    s.setData(candles.map((c) => ({ time: ts(c.timestamp), open: c.open, high: c.high, low: c.low, close: c.close })));
    if (!candles.length) { s.setMarkers([]); return; }
    const times = candles.map((c) => ts(c.timestamp));
    const snap = (t: number) => { // latest bar at or before t
      let lo = 0, hi = times.length - 1;
      if (t < times[0]) return null;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (times[m] <= t) lo = m; else hi = m - 1; }
      return times[lo];
    };
    const ms: SeriesMarker<Time>[] = [];
    for (const m of markers) {
      const t = snap(ts(m.time));
      if (t == null) continue;
      ms.push(m.type === "BUY"
        ? { time: t, position: "belowBar", color: "#22c55e", shape: "arrowUp", text: m.text ?? "B" }
        : { time: t, position: "aboveBar", color: "#f87171", shape: "arrowDown", text: m.text ?? "S" });
    }
    ms.sort((a, b) => (a.time as number) - (b.time as number));
    s.setMarkers(ms);
    if (fitted.current !== candles.length && Math.abs(fitted.current - candles.length) > 5) { chart.current?.timeScale().fitContent(); }
    fitted.current = candles.length;
  }, [candles, markers]);

  return <div ref={ref} className="chart-box" style={{ height }} />;
}

/** Equity curve as a gradient area, with the starting capital as a dashed baseline. */
export function EquityChart({ points, baseline, height = 200 }: { points: EquityPoint[]; baseline?: number; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const c = baseChart(ref.current, height);
    const last = points[points.length - 1]?.equity ?? 0, up = baseline == null || last >= baseline;
    const color = up ? "#22c55e" : "#ef4444";
    const s = c.addAreaSeries({ lineColor: color, topColor: color + "55", bottomColor: color + "05", lineWidth: 2, priceLineVisible: false });
    const seen = new Set<number>();
    s.setData(points.map((p) => ({ time: ts(p.timestamp), value: p.equity })).filter((p) => !seen.has(p.time) && seen.add(p.time)));
    if (baseline != null) s.createPriceLine({ price: baseline, color: "#6b7280", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "start" });
    c.timeScale().fitContent();
    return () => c.remove();
  }, [points, baseline, height]);
  return <div ref={ref} className="chart-box" style={{ height }} />;
}
