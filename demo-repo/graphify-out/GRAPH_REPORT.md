# Graph Report - demo-repo  (2026-10-22)

## Corpus Check
- Corpus is ~156 words - fits in a single context window. You may not need a graph.

## Summary
- 18 nodes · 21 edges · 4 communities (0 shown, 4 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3

## God Nodes (most connected - your core abstractions)
1. `charge_card()` - 5 edges
2. `finalise()` - 3 edges
3. `PaymentDeclined` - 3 edges
4. `submitOrder()` - 2 edges
5. `in_stock()` - 2 edges
6. `Checkout flow for the Northwind storefront.` - 1 edges
7. `Card charging against the payment gateway.` - 1 edges
8. `LINKS` - 1 edges

## Surprising Connections (you probably didn't know these)
- `finalise()` --calls--> `charge_card()`  [EXTRACTED]
  src/checkout.py → src/payments.py
- `finalise()` --calls--> `in_stock()`  [EXTRACTED]
  src/checkout.py → src/inventory.py

## Import Cycles
- None detected.

## Communities (4 total, 4 thin omitted)

## Knowledge Gaps
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `charge_card()` connect `Community 0` to `Community 2`?**
  _High betweenness centrality (0.097) - this node is a cross-community bridge._
- **Why does `finalise()` connect `Community 2` to `Community 0`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._