/**
 * Single-series line chart drawn in SVG, so no charting library and no iframe is involved.
 * Missing samples are gaps: the line breaks and a grey band says "no data" (never drawn as 0).
 * Hover or the arrow keys show a crosshair and the value at that time.
 */
import { useId, useMemo, useState } from "react";
import { t } from "@/lib/i18n";

export interface Point {
  /** ISO time. */
  t: string;
  v: number;
}

const W = 440;
const H = 170;
const PAD = { l: 44, r: 10, t: 10, b: 22 };

/** Round a maximum up to 1, 2 or 5 times a power of ten, so four gridlines get clean numbers. */
export function niceMax(max: number): number {
  if (!(max > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(max));
  const f = max / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/** Runs of consecutive points; a jump of more than 1.5 steps starts a new run (a gap). */
export function segments(points: Point[], stepS: number): Point[][] {
  const out: Point[][] = [];
  let run: Point[] = [];
  let prev = 0;
  for (const p of points) {
    const at = Date.parse(p.t);
    if (run.length > 0 && at - prev > stepS * 1500) {
      out.push(run);
      run = [];
    }
    run.push(p);
    prev = at;
  }
  if (run.length > 0) out.push(run);
  return out;
}

export function TimeSeries({
  points,
  stepS,
  windowS,
  label,
  format,
  unit,
}: {
  points: Point[];
  stepS: number;
  windowS: number;
  /** Accessible name, for example "Requests per second". */
  label: string;
  format: (v: number) => string;
  unit: string;
}) {
  const id = useId();
  const [hover, setHover] = useState<number | null>(null);
  const geo = useMemo(() => {
    const end = points.length ? Date.parse(points[points.length - 1]!.t) : windowS * 1000;
    const start = end - windowS * 1000;
    const top = niceMax(Math.max(0, ...points.map((p) => p.v)));
    const x = (at: number) => PAD.l + ((at - start) / (end - start || 1)) * (W - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + (1 - v / top) * (H - PAD.t - PAD.b);
    return { start, end, top, x, y };
  }, [points, windowS]);
  const runs = useMemo(() => segments(points, stepS), [points, stepS]);
  const gaps = runs.slice(1).map((r, i) => ({
    from: Date.parse(runs[i]![runs[i]!.length - 1]!.t),
    to: Date.parse(r[0]!.t),
  }));
  const wide = windowS > 86400;
  const tick = (at: number) =>
    new Date(at).toLocaleString(
      [],
      wide
        ? { month: "short", day: "numeric" }
        : { hour: "2-digit", minute: "2-digit", hour12: false },
    );
  const active = hover === null ? null : (points[hover] ?? null);

  function nearest(clientX: number, rect: DOMRect) {
    const at = geo.start + ((clientX - rect.left) / rect.width) * (geo.end - geo.start);
    let best = 0;
    points.forEach((p, i) => {
      if (Math.abs(Date.parse(p.t) - at) < Math.abs(Date.parse(points[best]!.t) - at)) best = i;
    });
    return best;
  }

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={label}
        aria-describedby={`${id}-v`}
        tabIndex={0}
        className="h-auto w-full touch-none outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onMouseMove={(e) => setHover(nearest(e.clientX, e.currentTarget.getBoundingClientRect()))}
        onMouseLeave={() => setHover(null)}
        onBlur={() => setHover(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") setHover(Math.min((hover ?? -1) + 1, points.length - 1));
          else if (e.key === "ArrowLeft") setHover(Math.max((hover ?? points.length) - 1, 0));
          else if (e.key === "Escape") setHover(null);
        }}
      >
        {[0, 1, 2, 3, 4].map((i) => {
          const v = (geo.top / 4) * i;
          return (
            <g key={i}>
              <line
                x1={PAD.l}
                x2={W - PAD.r}
                y1={geo.y(v)}
                y2={geo.y(v)}
                className="stroke-border"
                strokeWidth={1}
              />
              <text
                x={PAD.l - 6}
                y={geo.y(v) + 3}
                textAnchor="end"
                className="fill-muted-foreground text-[9px]"
              >
                {format(v)}
              </text>
            </g>
          );
        })}
        {[0, 1, 2, 3, 4].map((i) => {
          const at = geo.start + ((geo.end - geo.start) / 4) * i;
          return (
            <text
              key={i}
              x={geo.x(at)}
              y={H - 6}
              textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"}
              className="fill-muted-foreground text-[9px]"
            >
              {tick(at)}
            </text>
          );
        })}
        {gaps.map((g, i) => (
          <g key={i}>
            <rect
              x={geo.x(g.from)}
              y={PAD.t}
              width={Math.max(geo.x(g.to) - geo.x(g.from), 1)}
              height={H - PAD.t - PAD.b}
              className="fill-muted"
              opacity={0.7}
            />
            {geo.x(g.to) - geo.x(g.from) > 36 && (
              <text
                x={(geo.x(g.from) + geo.x(g.to)) / 2}
                y={(PAD.t + H - PAD.b) / 2}
                textAnchor="middle"
                className="fill-muted-foreground text-[9px]"
              >
                {t("no data")}
              </text>
            )}
          </g>
        ))}
        {runs.map((run, i) =>
          run.length === 1 ? (
            <circle
              key={i}
              cx={geo.x(Date.parse(run[0]!.t))}
              cy={geo.y(run[0]!.v)}
              r={1.8}
              className="fill-primary"
            />
          ) : (
            <polyline
              key={i}
              fill="none"
              strokeWidth={1.5}
              strokeLinejoin="round"
              className="stroke-primary"
              points={run.map((p) => `${geo.x(Date.parse(p.t))},${geo.y(p.v)}`).join(" ")}
            />
          ),
        )}
        {active && (
          <g>
            <line
              x1={geo.x(Date.parse(active.t))}
              x2={geo.x(Date.parse(active.t))}
              y1={PAD.t}
              y2={H - PAD.b}
              className="stroke-muted-foreground"
              strokeDasharray="3 3"
            />
            <circle
              cx={geo.x(Date.parse(active.t))}
              cy={geo.y(active.v)}
              r={3}
              className="fill-primary"
            />
          </g>
        )}
      </svg>
      <span id={`${id}-v`} className="sr-only" aria-live="polite">
        {active
          ? `${new Date(active.t).toLocaleTimeString()}: ${format(active.v)} ${unit}`
          : t("Use the arrow keys to read values.")}
      </span>
      {active && (
        <div
          role="presentation"
          className="pointer-events-none absolute top-2 rounded-md border bg-popover px-2 py-1 text-xs shadow-md"
          style={{
            left: `${Math.min(Math.max((geo.x(Date.parse(active.t)) / W) * 100, 12), 72)}%`,
          }}
        >
          <div className="font-mono text-muted-foreground">
            {new Date(active.t).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
              hour12: false,
            })}
          </div>
          <div className="font-semibold">
            {format(active.v)} {unit}
          </div>
        </div>
      )}
    </div>
  );
}
