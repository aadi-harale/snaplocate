import { exportReport } from "../api/reports.js";

export const TITLE = "Monthly sales";
export const RANGE_LABEL = "Date range";

export function exportButton() {
  const b = document.createElement("button");
  b.className = "pri";
  b.textContent = "Download CSV";
  b.onclick = exportReport;
  return b;
}

export function errorBanner(msg) {
  const d = document.createElement("div");
  d.className = "err";
  d.textContent = msg;
  return d;
}
