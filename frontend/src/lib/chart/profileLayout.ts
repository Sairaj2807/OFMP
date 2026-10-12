// Market profile period colours, shared by the profile chart (components/chart/MultiDayProfileChart.tsx).
// Geometry, pan and zoom live in lib/chart/multiDay.ts.

const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

/** A stable colour per period letter (A, B, C ...) cycling through hues. */
export function periodColor(index: number): string {
  const hue = (200 + index * 37) % 360;
  return `hsl(${hue} 62% 62%)`;
}

export function letterIndex(letter: string): number {
  const i = LETTERS.indexOf(letter);
  return i < 0 ? 0 : i;
}
