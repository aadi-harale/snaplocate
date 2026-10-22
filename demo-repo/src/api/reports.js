export async function exportReport() {
  const r = await fetch("/api/reports/export", { method: "POST" });
  return r.json();
}
