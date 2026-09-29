# User P&L Analysis Pipeline

`build_workbook.py` turns a raw `User_Details` export into a multi-sheet Excel
workbook with cycle-wise and month-wise P&L pivots, split by INT / POS running
type and by Anand Rathi cohort.

## Running it

```
python build_workbook.py
```

Three prompts, in order:

| Prompt | File | Required |
|---|---|---|
| User_Details CSV | `data/User_Details.csv` | yes |
| Exception accounts | `data/Exceptions.xlsx` | no (blank = skip) |
| Anand Rathi accounts | `data/ANANDRATHI_Users.xlsx` | no (blank = skip) |

Output is written next to the input CSV as
`User_Details_processed_YYYYMMDD_HHMMSS.xlsx`. Timestamped, so no run
overwrites another.

`python test_build_workbook.py` runs the self-checks (cycle bounding,
classification, backfill, normalisation, both pivot methods).

## Input

`User_Details.csv` — 34,325 rows, 10 columns:

`Date, User ID, Alias, Algo, Allocation, MTM, MTM %, Server, DTE, Index`

- `Date` — `DD-Mon-YY` (e.g. `01-Sep-26`)
- `Allocation` — rupees (`12500000` = 1.25 Cr)
- `DTE` — `0DTE`, `1DTE`, `4DTE`
- `Index` — `NF`, `SX` (also `BNF`, `NF_SX` in the raw file)

The exception and Anand Rathi files are single-column ID lists; the header may
be `User_ID` or `userId` (either spelling is accepted).

---

## Step 1 — Filtering (RawData to MainData)

Five drop rules, applied in order. Each logs its row count.

| # | Rule | Rows dropped |
|---|---|---|
| 1 | Algo not in {1, 7, 15} | 9,128 |
| 2 | Allocation < 1,00,00,000 (1 Cr, inclusive keep) | 7,695 |
| 3 | Index not in {NF, SX} | 287 |
| 4 | User ID in the exception list (17 IDs) | 1,038 |
| 5 | Account-cycle with no 0DTE row (see Step 4) | 6 |

**34,325 to 16,171 rows.**

Before filtering, `Algo` and `Allocation` are coerced to numeric and any row
that fails is dropped with a warning; `Index` and `DTE` are trimmed and
upper-cased.

## Step 2 — Cycle assignment

A cycle is one expiry window, per index:

- **SX** — Wednesday (1DTE) to Thursday (0DTE)
- **NF** — previous Friday (4DTE) to Tuesday (0DTE), via Monday (1DTE)

**Method:** walk each index's dates in order. A cycle ends on a 0DTE date; the
next date after it opens a new cycle. No holiday calendar is needed — a holiday
simply means fewer days in that cycle.

**Cycle ID** = `INDEX_YYYY-MM-DD` of the cycle's 0DTE day, e.g. `NF_2026-09-01`.

**Result: 35 cycles** — 18 NF, 17 SX.

Holiday cases seen in the data and handled correctly:

| Cycle | What happened |
|---|---|
| `NF_2026-05-05`, `NF_2026-06-30` | Friday holiday, no 4DTE day. 1DTE+0DTE accounts are still POS. |
| `SX_2026-05-27` | Wednesday holiday, 0DTE only. Every account in it is INT. |

## Step 3 — POS / INT classification

Per **Cycle x Index x User ID x Algo**:

- **POS** — the account traded on any non-0DTE day of its cycle
- **INT** — the account traded on 0DTE only

This one rule covers every case:

- NF 4DTE + 1DTE + 0DTE → POS
- NF 1DTE + 0DTE (Friday holiday, no 4DTE) → POS
- SX 1DTE + 0DTE → POS
- 0DTE only → INT
- a cycle where no non-0DTE day ran at all → everyone is INT, automatically

Written to MainData as the **`Running Type`** column; the `AccountType` sheet
lists one row per account-cycle with its DTE set.

**Result: 5,628 INT and 4,408 POS account-cycles.**

Any **NF** cycle with 0DTE alone (both 4DTE and 1DTE missing — two holidays in
one Fri-Tue window) is dumped in full to the terminal for manual review. None
occur in the current data.

## Step 4 — Allocation backfill

The **0DTE row is the reference** for the whole cycle. Within each
Cycle x Index x User ID x Algo, every non-0DTE row takes the account's 0DTE
allocation:

- **SX** — the 1DTE row is overwritten
- **NF** — the 4DTE and 1DTE rows are overwritten

**73 rows across 58 accounts** were corrected. Allocation is replaced in place
(no original kept), and `MTM %` is recomputed as `MTM / new Allocation * 100`
so the column stays consistent.

**Guard:** if an account ever shows two different 0DTE allocations in one
cycle, the script raises rather than silently picking one.

**No 0DTE row → dropped.** An account-cycle with 1DTE/4DTE rows but no 0DTE row
has no reference allocation, so the whole account-cycle is removed. The rows are
printed first. Six rows, five account-cycles:

| Date | Cycle | User ID | Alias | Algo | DTE |
|---|---|---|---|---|---|
| 08-May-26 | NF_2026-05-12 | DXX40 | A8_MANISHT_DUB_8C | 1 | 4DTE |
| 29-Jun-26 | NF_2026-06-30 | 6954037 | MSR_AJAY_TAMBI_POS | 1 | 1DTE |
| 31-Jul-26, 03-Aug-26 | NF_2026-08-04 | NS0008 | CCV_SANJAY_MITTAL_2C | 1 | 4DTE, 1DTE |
| 17-Jun-26 | SX_2026-06-18 | LOYALKAV | MSR_LOYALKAV_POS | 15 | 1DTE |
| 08-Jul-26 | SX_2026-07-09 | 6954037 | MSR_AJAY_TAMBI_POS | 1 | 1DTE |

## Step 5 — Normalisation to 1 Cr

Every account is scaled to a common **1 Cr** base so a large account cannot
dominate an aggregate:

```
factor          = Allocation / 1,00,00,000
Norm Allocation = 1,00,00,000
Norm MTM        = MTM / factor
Norm MTM %      = Norm MTM / 1,00,00,000 * 100
```

A 2 Cr account contributes 1 Cr of allocation and half its MTM; a 5 Cr account
one fifth.

**Note:** row-level `MTM %` is unchanged by this — it is a ratio, so scaling
both parts cancels out. Normalisation only matters once MTM is **summed across
accounts of different sizes**.

## Step 6 — Cycle MTM % (the `CycleMTM` sheet)

Computed per **Cycle x Index x Algo x Running Type** (206 rows over 35 cycles):

```
Total_Allocation = sum over users of (that user's average Norm Allocation
                                      across the days it ran in the cycle)
Cycle MTM %      = Sum_MTM / Total_Allocation * 100
```

**Why per-user average first:** a user counts its full 1 Cr **once** per cycle,
however many days of that cycle it traded. A POS account running 4DTE+1DTE+0DTE
and one running only 1DTE+0DTE both contribute 1 Cr — the denominator measures
capital deployed, not capital-days.

Worked example — `NF_2026-05-12`, Algo 1, POS:

- 67 users; 49 ran all 3 days, 18 ran only 2
- each user's average allocation = 1 Cr
- `Total_Allocation` = 67 x 1 Cr = **67,00,00,000**
- `Sum_MTM` = 4,10,62,080 → **Cycle MTM % = 6.13**

Columns: `Month, Cycle, Index, Algo, Running Type, Sum_MTM, Days, Accounts,
Rows, Total_Allocation, Cycle MTM %`.

## Step 7 — The two pivot methods

Both roll the cycle figures up to Algo level (overall) or Month x Algo level.

### Sum daily mtm%

```
sum of the Cycle MTM % values in the group
```

Adds per-cycle **ratios**. Every cycle counts equally regardless of how many
accounts ran in it.

### Avg daily mtm%

```
sum of Sum_MTM in the group / MEAN of the group's Total_Allocation
```

Pools the raw **MTM** instead, so cycles with more accounts carry proportionally
more weight.

**These two answer different questions and will not agree.** Where they diverge
sharply, the cause is usually a cycle with an unusual account count — see
Known data issues.

**Avg scales with cycle count.** MAY-26 spans 7 cycles and SEP-26 only 1, so
their Avg figures are not directly comparable; the Sum column does not have
this property.

### Month assignment

A cycle belongs to the month of its **0DTE (expiry) day**. An NF cycle whose
4DTE falls in July but whose 0DTE falls in August counts entirely as **August**.
Months are labelled `MMM-YY` (`MAY-26`) so multi-year data cannot collide.

## Step 8 — Cohorts

Each pivot block carries three cohorts x two methods = six column groups:

| Column group | Population |
|---|---|
| Sum / Avg daily mtm% | everyone — 16,171 rows |
| Excl AR Sum / Avg | everyone except Anand Rathi — 15,177 rows |
| Anand Rathi Sum / Avg | Anand Rathi only — 994 rows |

"Excl AR" and "Anand Rathi" together make up the plain columns.

**76 of the 87 listed Anand Rathi IDs** appear in MainData; the other 11 were
either filtered out upstream or are absent from the source.

---

## Output workbook

| Sheet | Contents |
|---|---|
| `RawData` | the CSV verbatim, 34,325 rows |
| `MainData` | after all filters, with Cycle, CycleNo, Month, Running Type and the Norm columns — 16,171 rows |
| `AccountType` | one row per account-cycle: its DTE set and Running Type |
| `POS` | MainData filtered to POS |
| `INT` | MainData filtered to INT |
| `CycleMTM` | per Cycle x Index x Algo x Running Type — every pivot cell is traceable here |
| `Pivot` | overall block on top, monthly block below |

Every pivot cell can be verified from `CycleMTM`: filter to the month, algo and
running type, then sum `Cycle MTM %` (Sum method) or divide sum of `Sum_MTM` by
the mean of `Total_Allocation` (Avg method).

## Results

### Overall

| Algo | Sum INT | Sum POS | Avg INT | Avg POS | ExAR Sum INT | ExAR Sum POS | ExAR Avg INT | ExAR Avg POS | AR Sum INT | AR Sum POS | AR Avg INT | AR Avg POS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 3.83 | 9.48 | 1.40 | 6.16 | 4.05 | 9.48 | 2.39 | 6.18 | 0.11 | -1.51 | -2.76 | -1.51 |
| 7 | 7.52 | 14.64 | 5.88 | 12.10 | 7.62 | 14.64 | 6.37 | 12.10 | 6.12 | | 3.78 | |
| 15 | 5.63 | 9.62 | 2.33 | 9.29 | 5.59 | 9.62 | 2.99 | 9.29 | -0.69 | | -3.28 | |

### Monthly

| Month | Algo | Sum INT | Sum POS | Avg INT | Avg POS | ExAR Sum INT | ExAR Sum POS | ExAR Avg INT | ExAR Avg POS | AR Sum INT | AR Avg INT |
|---|---|---|---|---|---|---|---|---|---|---|---|
| MAY-26 | 1 | 4.67 | 5.30 | 4.63 | 5.21 | 4.73 | 5.30 | 4.72 | 5.21 | 4.19 | 3.87 |
| MAY-26 | 7 | 3.84 | 5.48 | 3.80 | 5.42 | 3.96 | 5.48 | 3.85 | 5.42 | 3.07 | 3.47 |
| MAY-26 | 15 | 6.79 | 7.50 | 6.74 | 7.50 | 6.79 | 7.50 | 6.74 | 7.50 | | |
| JUN-26 | 1 | -1.13 | 0.39 | -0.89 | 0.54 | -1.09 | 0.39 | -0.88 | 0.54 | -1.31 | -0.94 |
| JUN-26 | 7 | 2.08 | 4.17 | 2.18 | 4.68 | 2.18 | 4.17 | 2.23 | 4.68 | 1.78 | 1.99 |
| JUN-26 | 15 | -0.04 | 1.81 | -0.27 | 1.66 | -0.02 | 1.81 | -0.09 | 1.66 | -0.00 | -2.58 |
| JUL-26 | 1 | -0.20 | 1.71 | -0.27 | 1.52 | -0.24 | 1.71 | -0.30 | 1.52 | -0.00 | -0.16 |
| JUL-26 | 7 | 1.67 | 3.11 | 1.49 | 3.05 | 1.51 | 3.11 | 1.40 | 3.05 | 2.14 | 1.74 |
| JUL-26 | 15 | -1.65 | 0.33 | -1.91 | 0.33 | -1.70 | 0.33 | -1.91 | 0.33 | -0.69 | -1.28 |
| AUG-26 | 1 | 1.55 | 3.44 | -0.60 | 1.30 | 1.72 | 3.44 | -0.21 | 1.30 | -1.74 | -1.42 |
| AUG-26 | 7 | 0.05 | 2.44 | -0.63 | 0.33 | 0.09 | 2.44 | -0.43 | 0.33 | -0.74 | -0.82 |
| AUG-26 | 15 | 1.18 | 0.94 | -0.12 | 1.24 | 1.18 | 0.94 | -0.12 | 1.24 | | |
| SEP-26 | 1 | -1.06 | -1.35 | -1.06 | -1.35 | -1.07 | -1.35 | -1.07 | -1.35 | -1.03 | -1.03 |
| SEP-26 | 7 | -0.12 | -0.56 | -0.12 | -0.56 | -0.12 | -0.56 | -0.12 | -0.56 | -0.13 | -0.13 |
| SEP-26 | 15 | -0.65 | -0.96 | -0.65 | -0.96 | -0.65 | -0.96 | -0.65 | -0.96 | | |

AR POS is blank except SEP-26 Algo 1 (-1.51), which is correct: of 994 Anand
Rathi rows, **991 are INT and 3 are POS** — a single account, `HMT1P006`, in one
cycle.

---

## Known data issues

**1. `NF_2026-08-11` is a thin cycle.** 70 distinct accounts against 178-220 in
every other cycle — 8 accounts on 4DTE and 12 on 1DTE for Algo 1, against 30 on
0DTE. Algo 15 has no 4DTE or 1DTE rows in it at all. Because the Sum method
weights every cycle equally, this partial cycle swings the AUG-26 figures hard,
which is why Sum and Avg diverge most in that month (Algo 1 INT: +1.55 vs
-0.60). Looks like a partial data capture for 07-Aug and 10-Aug rather than a
real trading pattern.

**2. Eleven Anand Rathi IDs are missing** from MainData (76 of 87 present).
Either filtered out by Steps 1-4 or absent from the source.

**3. `NS0008` ran NF 4DTE and 1DTE with no 0DTE** in `NF_2026-08-04` — a
combination that should not occur. Dropped by Step 4, but worth checking
whether the 0DTE row is missing from the source or the account squared off
early.

**4. MAY-26 and SEP-26 are partial months** — SEP-26 holds a single cycle
(`NF_2026-09-01`, the last in the data), so its figures are not comparable to a
full month.

## Design notes

- **Nothing is silently corrected.** Every drop is logged with its count, every
  anomaly is printed, and contradictory input raises instead of guessing.
- **Timestamped output** — runs never overwrite each other.
- **`CycleMTM` is the audit trail.** Every pivot cell traces back to it.
- If Excel reports "file format is not valid", the workbook was opened while it
  was still being written (a 5 MB file takes ~12 s). Wait for the `Wrote ...`
  log line, and delete any leftover `~$...xlsx` lock file.
