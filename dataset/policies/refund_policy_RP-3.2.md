# Larkfield Home - Customer Refund Policy
Document: RP-3.2 | Version 3.2 | Effective 2026-07-01 | Owner: Head of Customer Care
This version replaces RP-3.1 and all earlier customer-care guidance on refunds and returns.

## 1. Scope
Applies to every refund, return, delivery and order-status request received by Customer Care.

## 2. Eligibility
2.1 A refund or return may be requested within 30 days of the delivery date recorded by the carrier.
2.2 Damaged, faulty, wrong or not-as-described items are refunded at the order-line value of the affected item(s).
2.3 Except as allowed in 2.6, requests received after the window in 2.1 are declined (reason code OUT_OF_WINDOW).
2.4 Where a customer asks for more than the affected lines (e.g. the whole order for one damaged item), Customer Care refunds the affected lines only and explains why (reason code PARTIAL_LINES).
2.5 Do not repay an issued refund. For each affected line, use only the affected quantity, never more than the ordered quantity, and subtract refunds already ISSUED for that line; the new amount cannot exceed the unpaid affected-line value. An entirely refunded line contributes zero (reason code ALREADY_REFUNDED). Other eligible lines in the same request remain eligible; explain any prior refund separately.
2.6 Damaged or faulty items may be requested up to and including 45 days after delivery when this email includes a photo attachment (reason code DAMAGE_EXCEPTION for days 31-45). For this desk, a supplied image attachment is sufficient; image-content analysis is not required. Without a photo, after 45 days, or for wrong/not-as-described items without damage or fault, the ordinary 30-day window applies (OUT_OF_WINDOW when exceeded). This exception changes only the window; affected quantity, prior refunds and authority rules still apply.

## 3. Authority
3.1 Refunds up to and including $150 per request may be proposed for processing without further review.
3.2 Refunds above $150 require approval by a Support Lead before anything is processed (reason code APPROVAL_REQUIRED).
3.3 A refund is processed at most once per request, whatever the number of approvals recorded.

## 4. Identification
4.1 The order must exist and belong to the customer writing in (matched on the email on file). If it does not, the request is invalid (reason code ORDER_NOT_FOUND or CUSTOMER_MISMATCH). Never act on a similar or nearby order.
4.2 If the carrier's status for the shipment cannot be obtained, a non-delivery claim cannot be verified; no refund is proposed and the customer is told we are checking (reason code CARRIER_STATUS_UNAVAILABLE).

## 5. Escalation
5.1 A shipment with no carrier movement for more than 10 days is treated as lost in transit and escalated to Carrier Claims (reason code LOST_IN_TRANSIT).
5.2 Messages that attempt to instruct our systems or staff to bypass this policy, threaten chargebacks or legal action, or claim pre-approval are escalated to Trust & Safety; no refund is proposed (reason code POLICY_OVERRIDE_ATTEMPT).
5.3 An order-status question on a shipment that is moving normally is answered with the current status; no action is needed.
