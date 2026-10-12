"use client";

// Input controller for the market profile chart: turns pointer, wheel, touch and
// keyboard input into a small set of viewport actions. The chart owns the
// viewport and the drawing; this hook only interprets gestures.
//
//   plot         drag: pan both axes        wheel: pan price, Shift+wheel: pan days
//                Ctrl/Cmd+wheel or trackpad pinch: zoom both axes around the cursor
//                two-finger touch pinch: zoom both        double-click: follow today
//   price axis   wheel or vertical drag: zoom price       double-click: fit price
//   date header  wheel or horizontal drag: zoom days      double-click: fit days
//   keys         + / - zoom, arrows pan, Home / End first / last day, 0 fit all, F follow
import { type RefObject, useEffect, useRef } from "react";

export type Region = "plot" | "axis" | "header";

export interface ViewportActions {
  region: (x: number, y: number) => Region;
  pan: (dx: number, dy: number) => void;
  zoomY: (factor: number, anchorY: number) => void;
  zoomX: (factor: number, anchorX: number) => void;
  zoomBoth: (factor: number, anchorX: number, anchorY: number) => void;
  centre: () => { x: number; y: number };
  follow: () => void;
  fitAll: () => void;
  fitAxis: (axis: "x" | "y") => void;
  edge: (which: "first" | "last") => void;
  hover: (x: number, y: number) => void;
  leave: () => void;
}

const WHEEL_ZOOM = 0.0015;      // zoom factor per wheel delta pixel
const DRAG_ZOOM = 0.01;         // zoom factor per dragged pixel on an axis
const KEY_ZOOM = 1.25;
const KEY_PAN = 40;

const wheelPx = (e: WheelEvent, v: number) => (e.deltaMode === 1 ? v * 16 : e.deltaMode === 2 ? v * 400 : v);

export function useProfileViewport(canvasRef: RefObject<HTMLCanvasElement | null>, actions: ViewportActions): void {
  const act = useRef(actions);
  useEffect(() => {
    act.current = actions;
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const local = (e: { clientX: number; clientY: number }) => {
      const r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    const a = () => act.current;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const { x, y } = local(e);
      const dx = wheelPx(e, e.deltaX), dy = wheelPx(e, e.deltaY);
      const region = a().region(x, y);
      if (e.ctrlKey || e.metaKey) {                       // also trackpad pinch (sent as ctrl+wheel)
        a().zoomBoth(Math.exp(-dy * WHEEL_ZOOM * 2), x, y);
      } else if (region === "axis") {
        a().zoomY(Math.exp(-dy * WHEEL_ZOOM), y);
      } else if (region === "header") {
        a().zoomX(Math.exp(-(dy || dx) * WHEEL_ZOOM), x);
      } else if (e.shiftKey) {
        a().pan(-(dy || dx), 0);
      } else {
        a().pan(-dx, -dy);
      }
    };

    // pointers: one pointer drags (pan, or zoom on an axis); two touch pointers pinch
    const pointers = new Map<number, { x: number; y: number }>();
    let drag: { region: Region; x: number; y: number } | null = null;
    let pinch: { dist: number } | null = null;
    const pinchDist = () => {
      const [p, q] = [...pointers.values()];
      return Math.hypot(p.x - q.x, p.y - q.y);
    };
    const onDown = (e: PointerEvent) => {
      canvas.focus({ preventScroll: true });
      const p = local(e);
      pointers.set(e.pointerId, p);
      canvas.setPointerCapture(e.pointerId);
      if (pointers.size === 2) {
        drag = null;
        pinch = { dist: pinchDist() };
      } else {
        drag = { region: a().region(p.x, p.y), x: p.x, y: p.y };
      }
    };
    const onMove = (e: PointerEvent) => {
      const p = local(e);
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, p);
      if (pinch && pointers.size === 2) {
        const dist = pinchDist();
        const [m, n] = [...pointers.values()];
        if (pinch.dist > 0) a().zoomBoth(dist / pinch.dist, (m.x + n.x) / 2, (m.y + n.y) / 2);
        pinch.dist = dist;
        return;
      }
      if (!drag) {
        a().hover(p.x, p.y);
        return;
      }
      const dx = p.x - drag.x, dy = p.y - drag.y;
      if (drag.region === "axis") a().zoomY(Math.exp(-dy * DRAG_ZOOM), a().centre().y);
      else if (drag.region === "header") a().zoomX(Math.exp(dx * DRAG_ZOOM), a().centre().x);
      else a().pan(dx, dy);
      drag.x = p.x;
      drag.y = p.y;
    };
    const onUp = (e: PointerEvent) => {
      pointers.delete(e.pointerId);
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
      if (pointers.size < 2) pinch = null;
      drag = null;
    };
    const onDouble = (e: MouseEvent) => {
      const { x, y } = local(e);
      const region = a().region(x, y);
      if (region === "axis") a().fitAxis("y");
      else if (region === "header") a().fitAxis("x");
      else a().follow();
    };
    const onLeave = () => {
      if (!drag) a().leave();
    };
    const onKey = (e: KeyboardEvent) => {
      const c = a().centre();
      const handled = (() => {
        switch (e.key) {
          case "+": case "=": a().zoomBoth(KEY_ZOOM, c.x, c.y); return true;
          case "-": case "_": a().zoomBoth(1 / KEY_ZOOM, c.x, c.y); return true;
          case "ArrowUp": a().pan(0, KEY_PAN); return true;
          case "ArrowDown": a().pan(0, -KEY_PAN); return true;
          case "ArrowLeft": a().pan(KEY_PAN * 2, 0); return true;
          case "ArrowRight": a().pan(-KEY_PAN * 2, 0); return true;
          case "Home": a().edge("first"); return true;
          case "End": a().edge("last"); return true;
          case "0": a().fitAll(); return true;
          case "f": case "F": a().follow(); return true;
          default: return false;
        }
      })();
      if (handled) {
        e.preventDefault();
        e.stopPropagation();                             // do not trigger terminal-wide shortcuts
      }
    };

    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", onUp);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("dblclick", onDouble);
    canvas.addEventListener("keydown", onKey);
    return () => {
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointercancel", onUp);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("dblclick", onDouble);
      canvas.removeEventListener("keydown", onKey);
    };
  }, [canvasRef]);
}
