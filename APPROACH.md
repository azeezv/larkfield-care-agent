# Approach

## Problem Summary

<!-- Describe the problem you are solving and the key constraints. -->

## Technical Approach

<!-- Outline your solution architecture, data pipeline, and implementation steps. -->
    
## AI Tool Usage

<!-- Document how you used Cadra (OpenCode) during development — prompts, iterations, and what worked. -->

## Rules where RP-3.2 is silent

- Days are calendar dates in UTC (request date minus delivery or last-scan
  date), not elapsed hours; 30/45/10 are inclusive limits.
- Stalled days count from the last scan on record, even if it predates
  shipped_at; the evidence notes the inconsistency.
- An undelivered, still-moving claim gets no refund yet: outcome PROCEED with
  an empty reason-code list; the window check explains why.
- `affected_lines[].qty` is clamped to the ordered quantity (2.5), and a ticket
  asking for the whole order carries `requested_all` so 2.4 can reduce it.
- ORDER_STATUS and OTHER tickets never propose money.

## Trade-offs & Limitations

<!-- Note compromises, assumptions, and what you would improve with more time. -->
