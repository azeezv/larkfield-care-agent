# Data dictionary - Larkfield Home support data room

Everything here is exactly as the source systems exported it on 2026-11-02. Nothing has been cleaned.

## data/customers.csv  (CRM export)
| column | meaning |
|---|---|
| customer_id | CRM customer key |
| name | customer name |
| email | email on file (as typed at signup) |
| country | ISO country |
| created_at | account creation date |
| loyalty_tier | none / silver / gold |

## data/orders.csv  (order management system export)
| column | meaning |
|---|---|
| order_id | order number, `LF-` + 6 digits |
| customer_id | buyer, joins to customers.customer_id |
| order_date | date the order was placed |
| status | DELIVERED / SHIPPED |
| Order Total | order value (items) |
| shipping_fee | delivery charge paid, USD |
| currency | USD |
| payment_method | card / paypal |

## data/order_items.csv
| column | meaning |
|---|---|
| order_id | joins to orders.order_id |
| line_no | line number within the order |
| sku | product code |
| product_name | product name |
| qty | quantity |
| unit_price | price per unit, USD |

## data/shipments.csv  (carrier feed)
| column | meaning |
|---|---|
| shipment_id | shipment key |
| order_id | joins to orders.order_id |
| carrier | carrier name |
| tracking_no | carrier tracking number |
| status | DELIVERED / IN_TRANSIT / UNAVAILABLE (carrier did not return a status) |
| shipped_at | handed to carrier |
| delivered_at | delivery scan, empty if not delivered |
| last_event_at | latest carrier scan, empty when the carrier returned nothing |

## data/refunds.csv  (payments ledger, refunds already issued)
| column | meaning |
|---|---|
| refund_id | refund key |
| order_id | joins to orders.order_id |
| line_no | refunded order line |
| amount | amount refunded, USD |
| status | ISSUED |
| requested_at | when it was requested |
| approved_by | auto or approver |

## policies/
`refund_policy_RP-3.2.md`, `help_centre_faq_v2.md` and `warehouse_picking_WH-PICK-1.4.md`: exported documents with scope, owner, version and date in each header.

## tickets/support_inbox_export.json
30 emails from the support inbox, October 2026, in the same shape as the API request body.

## tickets/eval_seed_labelled.json
15 tickets from the inbox export with the outcome, approval flag and approver role a senior agent assigned. Use them however you like.

## inbox/public_tickets.json
10 further tickets in the API request shape, without outcomes.

## schema/response_schema.py
Pydantic models of the request and response bodies in the brief's contract.
