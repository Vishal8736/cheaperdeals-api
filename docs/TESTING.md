# Testing and rehearsal

## Automated tests

```bash
python -m pytest                                   # SQLite, about 15 s
TEST_DATABASE_URL=postgresql://user:pass@host:5432/testdb python -m pytest   # real Postgres (the DB is wiped)
```

What the 65 tests cover:

| Risk | Test |
|---|---|
| Duplicate webhook | same order twice creates one row; double processing creates one supplier order; concurrent claims are blocked |
| Illegal status jumps | state machine rejects them; every change is written to the audit log |
| Real money safety | DRY-RUN never calls the supplier; kill switch stops everything |
| Selling at a loss | negative and below-minimum margin blocked; per-product override; out of stock; unknown product |
| Approvals and cap | threshold holds the order; approve sends, reject cancels; daily cap defers (does not fail) |
| Supplier trouble | 429 retries with 2, 4 minutes growth then hands over to you; permanent error goes to you; **reply lost after send** either finds the order or stops for a human, never a second order |
| Tracking | customer told once per change; status never moves backwards; supplier cancel alerts; store outage is retried |
| Stock and price | hide and unhide, reprice with markup, manual price kept, loss guard, disabled product stays off |
| Webhooks and web | signature check, ping ignored, login rate limit, CSRF, internal key, all pages render, open-redirect blocked, unsafe config refuses to boot |

## Rehearsal before going live

1. Local simulator (`SETUP_WINDOWS.md` A): create normal, 429 and reply-lost orders, resolve the stuck one, run "Move supplier forward".
2. Real store, DRY-RUN: place a test order. Check: it appears once; margin and stock checks behave; the approval rule fires; Telegram pings.
3. Kill the n8n container for 10 minutes: the dashboard heartbeat shows "Late", and the missed-order sweep imports any order whose webhook was lost once n8n returns.
4. First live order: one cheap product, ship to yourself. Compare CJ's real order status values and tracking fields with `docs/ASSUMPTIONS.md` items 1 to 5 and fix the adapter if needed.
5. Only then raise the daily cap and lower the approval threshold.

## Daily 2-minute check

Dashboard headline ("Everything is moving" or "N orders need you"), open alerts, heartbeat all "On time", and the supplier balance in CJ.
