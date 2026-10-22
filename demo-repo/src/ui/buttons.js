import { submitOrder } from "../api/orders.js";

export function retryButton() {
  const b = document.createElement("button");
  b.className = "pri";
  b.textContent = "Retry payment";
  b.onclick = submitOrder;
  return b;
}

export function cancelButton() {
  const b = document.createElement("button");
  b.textContent = "Cancel order";
  return b;
}
