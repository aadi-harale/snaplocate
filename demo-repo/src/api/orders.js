export async function submitOrder() {
  const r = await fetch("/api/orders", { method: "POST" });
  return r.json();
}
