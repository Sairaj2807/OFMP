// Display formatting. Times are shown in IST (the exchange's clock); data is UTC epoch.

const IST = "Asia/Kolkata";

export function compact(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)}L`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}K`;
  return `${sign}${Math.round(abs)}`;
}

export function signed(value: number): string {
  return (value > 0 ? "+" : "") + compact(value);
}

export function price(value: number | null | undefined, digits = 2): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);
}

export function istTime(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleTimeString("en-GB", { timeZone: IST, hour12: false, hour: "2-digit", minute: "2-digit" });
}

export function intervalLabel(sec: number): string {
  return sec % 3600 === 0 ? `${sec / 3600}h` : `${sec / 60}m`;
}
