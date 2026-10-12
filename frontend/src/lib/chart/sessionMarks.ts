// Chart-library primitive for the multi-day footprint: a dashed separator
// before each session's first column and the session's date, kept in view
// while any part of the session is (sticky label). Draws only; no data.

export interface SessionMark {
  date: string;
  live: boolean;
  time: number;            // the session's first bar
}

// The parts of the library's render context this primitive uses (CSS pixels;
// the canvas is in device pixels, hence `dpr`).
interface RenderContext {
  dpr: number;
  plotWidth: number;
  plotHeight: number;
  dataLayer: { timeToIndex(time: number): number | undefined };
  timeScale: { indexToX(index: number): number; barSpacing: number };
}

const LINE = "rgba(163,172,186,0.45)";
const LABEL_BG = "rgba(18,21,27,0.85)";
const LABEL = "#a3acba";
const TODAY = "#e2b93b";

/** "2026-10-08" -> "Thu 08 Oct". */
export function dayLabel(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return date;
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", timeZone: "UTC" });
}

export class SessionMarks {
  private marks: SessionMark[] = [];
  private host: { requestUpdate(): void } | null = null;

  attached(host: { requestUpdate(): void }): void {
    this.host = host;
  }

  detached(): void {
    this.host = null;
  }

  zOrder(): "top" {
    return "top";
  }

  autoscaleInfo(): null {
    return null;
  }

  setMarks(marks: SessionMark[]): void {
    const same = marks.length === this.marks.length
      && marks.every((m, i) => m.time === this.marks[i].time && m.live === this.marks[i].live);
    this.marks = marks;
    if (!same) this.host?.requestUpdate();
  }

  draw(ctx: CanvasRenderingContext2D, rc: RenderContext): void {
    if (this.marks.length < 1) return;
    const r = rc.dpr;
    const half = rc.timeScale.barSpacing / 2;
    const xs = this.marks.map((m) => {
      const i = rc.dataLayer.timeToIndex(m.time);
      return i === undefined ? null : rc.timeScale.indexToX(i) - half;
    });
    ctx.save();
    ctx.font = `${11 * r}px system-ui, sans-serif`;
    ctx.textBaseline = "middle";
    for (let k = 0; k < this.marks.length; k++) {
      const x0 = xs[k];
      if (x0 === null) continue;
      const x1 = xs.slice(k + 1).find((x) => x !== null) ?? rc.plotWidth;
      if (x1 < 0 || x0 > rc.plotWidth) continue;
      if (k > 0 && x0 >= 0) {
        ctx.strokeStyle = LINE;
        ctx.lineWidth = Math.max(1, r);
        ctx.setLineDash([4 * r, 4 * r]);
        ctx.beginPath();
        ctx.moveTo(Math.round(x0 * r) + 0.5, 0);
        ctx.lineTo(Math.round(x0 * r) + 0.5, rc.plotHeight * r);
        ctx.stroke();
      }
      const m = this.marks[k];
      const text = m.live ? `TODAY · ${dayLabel(m.date)}` : dayLabel(m.date);
      const w = ctx.measureText(text).width / r + 10;
      const left = Math.min(Math.max(x0, 0) + 4, x1 - w - 4);
      if (left < Math.max(x0, 0)) continue;        // the session is too narrow on screen for its label
      ctx.setLineDash([]);
      ctx.fillStyle = LABEL_BG;
      ctx.fillRect(left * r, 4 * r, w * r, 16 * r);
      ctx.fillStyle = m.live ? TODAY : LABEL;
      ctx.fillText(text, (left + 5) * r, 12 * r);
    }
    ctx.restore();
  }
}
