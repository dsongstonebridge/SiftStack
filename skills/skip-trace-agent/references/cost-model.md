# Cost Model

Use this to pre-cost a batch before running anything billed.

| Source | Bills on | Rate | Stage |
|---|---|---|---|
| SmartSkip | per hit (record with a result) | **$0.15** | run outside this agent; the export is the input |
| DirectSkip | per result (record with a match) | **$0.10** | Stage 2 |
| TrestleIQ (phone validator) | per API call (one per unique number) | **$0.015** | Stage 5 |

## Rules of thumb
- **TrestleIQ dominates** — typically ~70% of total spend, because it validates every number in the union.
- **Always dedupe numbers globally before TrestleIQ.** A number's dial tier is the same wherever it appears, so validate it once. (Per-record validation on a 100-property batch wastes ~180 calls ≈ ~$2.70.)
- A ~100-property batch ≈ ~3,000 unique numbers ≈ **~$45 TrestleIQ** + ~$9 DirectSkip.

## Worked example — the July 2026 100-property batch
| Source | Volume | Cost |
|---|---|---|
| SmartSkip | 73 hits | $10.95 |
| DirectSkip | 93 results | $9.30 |
| TrestleIQ | 2,988 unique calls | $44.82 |
| **Total** | | **≈ $65** |

## Levers to trim spend (if needed)
- Don't re-validate numbers already tiered recently (skip existing numbers that already carry a dial tier).
- Validate only priority relatives (e.g., skip low-confidence DirectSkip-only people).
- Watch the TrestleIQ balance: a mid-batch credit-out returns 403 (surfaces as "Invalid API key") and halts validation — top up and resume the remaining chunks.
