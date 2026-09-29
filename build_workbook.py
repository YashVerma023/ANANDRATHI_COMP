"""Stage 1: load User_Details CSV, filter to relevant algos/allocation/index,
write a two-sheet workbook (RawData, MainData).

Run: python build_workbook.py   -> prompts for the CSV path.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

KEEP_ALGOS = {1, 7, 15}
MIN_ALLOCATION = 10_000_000  # Rs 1 Crore, inclusive
NORM_BASE = 10_000_000  # every account scaled to Rs 1 Crore before aggregating
KEEP_INDEX = {"NF", "SX"}
REQUIRED_COLUMNS = ["Date", "User ID", "Alias", "Algo", "Allocation", "DTE", "Index"]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s"
)
log = logging.getLogger("build_workbook")


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"User ID": str})
    df.columns = df.columns.str.strip()
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    if df.empty:
        raise ValueError("CSV has no data rows")
    log.info("Loaded %s rows x %s cols from %s", len(df), df.shape[1], path.name)
    return df


def load_exceptions(path: Path) -> set[str]:
    """Read the exception-accounts file (single User ID column, any header spelling)."""
    df = pd.read_excel(path) if path.suffix.lower() in {".xlsx", ".xls"} else pd.read_csv(path)
    col = next(
        (c for c in df.columns if c.strip().lower().replace(" ", "_") in {"user_id", "userid"}),
        None,
    )
    if col is None:
        raise ValueError(f"No User ID column found in {path.name}; got {list(df.columns)}")
    ids = set(df[col].dropna().astype(str).str.strip())
    log.info("Loaded %s exception account IDs from %s", len(ids), path.name)
    return ids


def filter_main(df: pd.DataFrame, exceptions: set[str] = frozenset()) -> pd.DataFrame:
    """Normalise, then apply the drop rules. Logs what each rule removed."""
    out = df.copy()
    out["Algo"] = pd.to_numeric(out["Algo"], errors="coerce")
    out["Allocation"] = pd.to_numeric(out["Allocation"], errors="coerce")
    for col in ("Index", "DTE", "Alias", "Server"):
        if col in out.columns:
            out[col] = out[col].astype(str).str.strip()
    out["Index"] = out["Index"].str.upper()
    out["DTE"] = out["DTE"].str.upper()

    bad = out["Algo"].isna() | out["Allocation"].isna()
    if bad.any():
        log.warning("%s rows have non-numeric Algo/Allocation - dropped", bad.sum())
        out = out[~bad]

    rules = (
        ("algo not in 1/7/15", lambda d: ~d["Algo"].isin(KEEP_ALGOS)),
        ("allocation < 1 Cr", lambda d: d["Allocation"] < MIN_ALLOCATION),
        ("index not NF/SX", lambda d: ~d["Index"].isin(KEEP_INDEX)),
        ("exception account", lambda d: d["User ID"].str.strip().isin(exceptions)),
    )
    for name, rule in rules:
        mask = rule(out)
        log.info("Dropping %s rows: %s", int(mask.sum()), name)
        out = out[~mask]

    if out.empty:
        log.warning("MainData is empty after filtering")
    else:
        log.info(
            "MainData: %s rows | dates %s | index/DTE combos %s",
            len(out),
            out["Date"].nunique(),
            sorted(set(zip(out["Index"], out["DTE"]))),
        )
    return out


def assign_cycles(df: pd.DataFrame) -> pd.DataFrame:
    """Tag each row with its Index cycle.

    A cycle runs from the day after the previous 0DTE up to and including the
    next 0DTE (SX: Wed->Thu, NF: prior Fri->Tue). Holidays simply mean fewer
    days in the cycle - the walk handles that without a holiday calendar.
    """
    out = df.copy()
    out["_dt"] = pd.to_datetime(out["Date"], format="%d-%b-%y", errors="coerce")
    if out["_dt"].isna().any():
        bad = out.loc[out["_dt"].isna(), "Date"].unique()
        raise ValueError(f"Unparseable Date values (expected DD-Mon-YY): {bad[:5]}")

    labels = {}
    for index, grp in out.groupby("Index"):
        days = grp[["_dt", "DTE"]].drop_duplicates().sort_values("_dt")
        n, started = 0, False
        for day, dte in days.itertuples(index=False):
            if not started:
                n, started = n + 1, True
            labels[(index, day)] = n
            if dte == "0DTE":
                started = False  # next day opens a new cycle

    out["CycleNo"] = [labels[(i, d)] for i, d in zip(out["Index"], out["_dt"])]
    # Cycle ID = index + its 0DTE date (or the last date, if the cycle has no 0DTE)
    ends = (
        out.groupby(["Index", "CycleNo"])
        .apply(
            lambda g: (g.loc[g["DTE"] == "0DTE", "_dt"].max() or pd.NaT)
            if (g["DTE"] == "0DTE").any()
            else g["_dt"].max(),
            include_groups=False,
        )
        .rename("_end")
    )
    out = out.merge(ends, left_on=["Index", "CycleNo"], right_index=True)
    out["Cycle"] = out["Index"] + "_" + out["_end"].dt.strftime("%Y-%m-%d")
    # A cycle belongs to the month of its 0DTE (expiry) day, so a Jul 4DTE
    # feeding an Aug 0DTE is counted in Aug.
    out["Month"] = out["_end"].dt.strftime("%b-%y").str.upper()
    out["_month_sort"] = out["_end"].dt.to_period("M").astype(str)
    return out


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """POS = account traded any non-0DTE day of its cycle; INT = 0DTE only.

    NF 4DTE may be missing (Friday holiday) - 1DTE+0DTE is still POS. A cycle
    with no non-0DTE day at all therefore yields all-INT, which is the required
    edge-case behaviour for both indices.
    """
    key = ["Cycle", "Index", "User ID", "Algo"]
    acct = (
        df.groupby(key)["DTE"]
        .agg(lambda s: sorted(set(s)))
        .rename("DTEs")
        .reset_index()
    )
    acct["Running Type"] = [
        "INT" if set(d) == {"0DTE"} else "POS" for d in acct["DTEs"]
    ]
    acct["DTEs"] = acct["DTEs"].str.join("+")
    return acct


ACCOUNT_KEY = ["Cycle", "Index", "User ID", "Algo"]


def backfill_allocation(df: pd.DataFrame) -> pd.DataFrame:
    """Within a cycle, force every non-0DTE row to its account's 0DTE allocation.

    The 0DTE row is the reference for the whole cycle (SX: 1DTE; NF: 4DTE and
    1DTE). MTM % is recomputed so it stays consistent with the new allocation.
    An account-cycle with no 0DTE row has no reference allocation, so all of its
    rows in that cycle are printed and then dropped.
    """
    out = df.copy()
    ref = (
        out[out["DTE"] == "0DTE"]
        .set_index(ACCOUNT_KEY)["Allocation"]
        .rename("_ref")
    )
    if ref.index.duplicated().any():
        dup = ref.index[ref.index.duplicated()].tolist()
        raise ValueError(f"Account has multiple 0DTE rows in one cycle: {dup[:5]}")

    out = out.join(ref, on=ACCOUNT_KEY)
    missing = out["_ref"].isna()
    if missing.any():
        print("\n*** No 0DTE row in the cycle - account-cycle DROPPED:")
        print(
            out.loc[missing, ["Date", "Cycle", "Index", "User ID", "Alias", "Algo", "DTE", "Allocation"]]
            .sort_values(["Cycle", "User ID"])
            .to_string(index=False)
        )
        log.info(
            "Dropping %s rows (%s account-cycles): no 0DTE row to backfill from",
            int(missing.sum()),
            out.loc[missing, ACCOUNT_KEY].drop_duplicates().shape[0],
        )
        out = out[~missing]

    changed = out["Allocation"] != out["_ref"]
    log.info(
        "Backfilled allocation on %s rows (%s accounts) from their 0DTE value",
        int(changed.sum()),
        out.loc[changed, ACCOUNT_KEY].drop_duplicates().shape[0],
    )
    out.loc[changed, "Allocation"] = out.loc[changed, "_ref"]
    if "MTM" in out.columns and "MTM %" in out.columns:
        out["MTM %"] = (out["MTM"] / out["Allocation"] * 100).round(2)
    return out.drop(columns=["_ref"])


def report_anomalies(df: pd.DataFrame) -> None:
    """Print any NF cycle that ran 0DTE alone (both 4DTE and 1DTE missing).

    Rare - two holidays in one Fri-Tue window. Every account in such a cycle is
    forced to INT, so the rows are dumped for a manual check.
    """
    nf = df[df["Index"] == "NF"]
    for cycle, dtes in nf.groupby("Cycle")["DTE"].agg(set).items():
        if dtes == {"0DTE"}:
            print(f"\n*** NF 0DTE-only cycle (all accounts forced INT): {cycle}")
            print(nf.loc[nf["Cycle"] == cycle].to_string(index=False))


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Scale every account to a NORM_BASE (1 Cr) allocation.

    Factor = Allocation / NORM_BASE; MTM is divided by the same factor, so a
    2 Cr account contributes 1 Cr of allocation and half its MTM. Row-level
    MTM % is unchanged by this (it is a ratio) - normalisation only matters
    when MTM is summed across accounts of different sizes.
    """
    out = df.copy()
    factor = out["Allocation"] / NORM_BASE
    out["Norm Allocation"] = NORM_BASE
    out["Norm MTM"] = out["MTM"] / factor
    out["Norm MTM %"] = (out["Norm MTM"] / NORM_BASE * 100).round(4)
    return out


def cycle_mtm(df: pd.DataFrame) -> pd.DataFrame:
    """Per Cycle x Algo x Running Type: sum of MTM / average allocation.

    Average allocation = total normalised allocation of the group / number of
    trading days in it, so a POS account running 4DTE+1DTE+0DTE contributes
    3 Cr over 3 days = 1 Cr, the same weight as an INT account's single day.
    """
    keys = ["_month_sort", "Month", "Cycle", "Index", "Algo", "Running Type"]
    g = df.groupby(keys, as_index=False).agg(
        Sum_MTM=("Norm MTM", "sum"),
        Days=("Date", "nunique"),
        Accounts=("User ID", "nunique"),
        Rows=("User ID", "size"),
    )
    # Cycle allocation = each user's average allocation over the days it ran in
    # that cycle, summed across users. A user therefore counts its full 1 Cr
    # once, however many days of the cycle it traded.
    per_user = df.groupby(keys + ["User ID"])["Norm Allocation"].mean()
    g = g.merge(
        per_user.groupby(keys).sum().rename("Total_Allocation"), on=keys
    )
    g["Cycle MTM %"] = (g["Sum_MTM"] / g["Total_Allocation"] * 100).round(4)
    return g.sort_values(["Cycle", "Algo", "Running Type"])


def sum_daily(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Sum of the cycle MTM % values falling in each group."""
    return cycle_mtm(df).pivot_table(
        index=by, columns="Running Type", values="Cycle MTM %", aggfunc="sum"
    )


def avg_daily(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Sum of MTM over the group / the AVERAGE of its cycles' allocations.

    The denominator averages the per-cycle allocations rather than summing
    them, so the figure scales with the number of cycles in the group. Unlike
    sum_daily it pools the raw MTM instead of adding per-cycle ratios, so
    cycles with more accounts carry proportionally more weight.
    """
    g = cycle_mtm(df).groupby(by + ["Running Type"]).agg(
        mtm=("Sum_MTM", "sum"), alloc=("Total_Allocation", "mean")
    )
    return (g["mtm"] / g["alloc"] * 100).unstack("Running Type")


def build_block(
    full: pd.DataFrame, excl: pd.DataFrame, ar: pd.DataFrame, by: list[str]
) -> pd.DataFrame:
    """One pivot block: overall, ex-Anand-Rathi, and Anand-Rathi-only columns.

    `by` is ["Algo"] for the overall block or ["_month_sort", "Month", "Algo"]
    for the monthly one. Every cohort carries both INT and POS.
    """
    parts = {
        "Sum daily mtm%": sum_daily(full, by),
        "Avg daily mtm%": avg_daily(full, by),
        "Excl AR Sum daily mtm%": sum_daily(excl, by),
        "Excl AR Avg daily mtm%": avg_daily(excl, by),
        "Anand Rathi Sum daily mtm%": sum_daily(ar, by) if len(ar) else None,
        "Anand Rathi Avg daily mtm%": avg_daily(ar, by) if len(ar) else None,
    }
    frames = []
    for label, tbl in parts.items():
        if tbl is None:
            continue
        cols = ["INT", "POS"]
        tbl = tbl.reindex(columns=cols)
        tbl.columns = pd.MultiIndex.from_product([[label], cols])
        frames.append(tbl)

    out = pd.concat(frames, axis=1).round(2).reset_index()
    if "_month_sort" in out.columns:
        out = out.sort_values(["_month_sort", "Algo"]).drop(columns="_month_sort")
    # Row labels stay in the index: Excel cannot write MultiIndex columns
    # alongside index=False.
    return out.set_index([c for c in ("Month", "Algo") if c in out.columns])


def main() -> int:
    path = Path(input("Enter path to User_Details CSV: ").strip().strip('"'))
    exc_raw = input("Enter path to Exception accounts file (blank = none): ").strip().strip('"')
    exc_path = Path(exc_raw) if exc_raw else None
    ar_raw = input("Enter path to Anand Rathi accounts file (blank = none): ").strip().strip('"')
    ar_path = Path(ar_raw) if ar_raw else None
    for p in (path, exc_path, ar_path):
        if p is not None and not p.is_file():
            log.error("File not found: %s", p)
            return 1

    try:
        raw = load_csv(path)
        exceptions = load_exceptions(exc_path) if exc_path else set()
        ar_ids = load_exceptions(ar_path) if ar_path else set()
        main_df = backfill_allocation(assign_cycles(filter_main(raw, exceptions)))
        accounts = classify(main_df)
    except (ValueError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        log.error("Failed to read/process %s: %s", path.name, exc)
        return 1

    report_anomalies(main_df)
    log.info(
        "Cycles: %s | accounts classified: %s",
        main_df["Cycle"].nunique(),
        accounts["Running Type"].value_counts().to_dict(),
    )

    main_df = main_df.merge(
        accounts[["Cycle", "Index", "User ID", "Algo", "Running Type"]],
        on=["Cycle", "Index", "User ID", "Algo"],
        how="left",
    ).drop(columns=["_dt", "_end"])

    main_df = normalise(main_df)
    cyc = cycle_mtm(main_df)
    is_ar = main_df["User ID"].str.strip().isin(ar_ids)
    log.info(
        "Anand Rathi rows: %s of %s (%s accounts present of %s listed)",
        int(is_ar.sum()),
        len(main_df),
        main_df.loc[is_ar, "User ID"].nunique(),
        len(ar_ids),
    )
    excl_df, ar_df = main_df[~is_ar], main_df[is_ar]

    pivot = build_block(main_df, excl_df, ar_df, ["Algo"])
    monthly = build_block(main_df, excl_df, ar_df, ["_month_sort", "Month", "Algo"])
    print("\n=== Overall ===")
    print(pivot.to_string())
    print("\n=== Monthly (cycle counted in its 0DTE month) ===")
    print(monthly.to_string())

    out_path = path.with_name(
        f"{path.stem}_processed_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    )
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        raw.to_excel(writer, sheet_name="RawData", index=False)
        main_df.to_excel(writer, sheet_name="MainData", index=False)
        accounts.to_excel(writer, sheet_name="AccountType", index=False)
        for rtype in ("POS", "INT"):
            main_df[main_df["Running Type"] == rtype].to_excel(
                writer, sheet_name=rtype, index=False
            )
        cyc.drop(columns="_month_sort").to_excel(
            writer, sheet_name="CycleMTM", index=False
        )
        # Both pivots on one sheet: overall on top, monthly below it.
        pivot.to_excel(writer, sheet_name="Pivot", startrow=1)
        month_row = len(pivot) + 6
        monthly.to_excel(writer, sheet_name="Pivot", startrow=month_row)
        ws = writer.sheets["Pivot"]
        ws.cell(row=1, column=1, value="Overall")
        ws.cell(row=month_row, column=1, value="Monthly (cycle counted in its 0DTE month)")

    log.info("Wrote %s (RawData=%s, MainData=%s)", out_path, len(raw), len(main_df))
    return 0


if __name__ == "__main__":
    sys.exit(main())
