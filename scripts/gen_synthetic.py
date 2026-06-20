"""Generate synthetic SAP B1 AP listings for ABC Manufacturing.

Produces a recognisable month of invoice lines that exercises every path in
the pipeline:

  * item-rule activity  (DIESEL-B7 @ Petronas, with litres)         -> CLEAN
  * vendor-rule activity (Tenaga electricity, with kWh)             -> CLEAN
  * item-rule activity  (R134A refrigerant, with kg)               -> CLEAN
  * GL-rule spend        (freight on AcctCode 5400)                -> WARNING
  * item rule, no qty    (DIESEL-B7 with no litres -> spend fallback) -> WARNING
  * unmapped line        (office paper, no rule)                   -> WARNING
  * planted duplicate    (DocNum also in e-Claim)                  -> DUPLICATE

The functions here are imported by the test-suite so fixtures and the shipped
sample files come from one source of truth. Run as a script to write the
sample CSV + XLSX into ``data/synthetic/``.
"""

from __future__ import annotations

from pathlib import Path

HEADER = [
    "DocEntry", "LineNum", "DocNum", "DocDate", "ItemCode", "ItemName",
    "CardName", "AcctCode", "Quantity", "UoM", "LineTotal", "Currency",
]


def _row(**kw: str) -> dict[str, str]:
    return {col: str(kw.get(col, "")) for col in HEADER}


def month_rows() -> list[dict[str, str]]:
    """A clean month: 3 clean, 3 warning, 1 cross-channel duplicate."""
    return [
        _row(DocEntry="18214", LineNum="0", DocNum="AP-2026-001", DocDate="2026-05-03",
             ItemCode="DIESEL-B7", ItemName="Diesel B7 (fleet)",
             CardName="Petronas Dagangan Berhad", AcctCode="5200",
             Quantity="450", UoM="L", LineTotal="1350.00", Currency="MYR"),
        _row(DocEntry="18220", LineNum="0", DocNum="AP-2026-010", DocDate="2026-05-07",
             ItemCode="", ItemName="Electricity — May",
             CardName="Tenaga Nasional Berhad", AcctCode="6200",
             Quantity="12000", UoM="kWh", LineTotal="7020.00", Currency="MYR"),
        _row(DocEntry="18225", LineNum="0", DocNum="AP-2026-015", DocDate="2026-05-11",
             ItemCode="", ItemName="Outbound freight",
             CardName="KL Logistics Sdn Bhd", AcctCode="5400",
             Quantity="", UoM="", LineTotal="3200.00", Currency="MYR"),
        _row(DocEntry="18230", LineNum="0", DocNum="AP-2026-020", DocDate="2026-05-14",
             ItemCode="R134A", ItemName="Refrigerant R-134a",
             CardName="CoolGas Supplies", AcctCode="5300",
             Quantity="8", UoM="KG", LineTotal="960.00", Currency="MYR"),
        _row(DocEntry="18233", LineNum="0", DocNum="AP-2026-025", DocDate="2026-05-18",
             ItemCode="DIESEL-B7", ItemName="Diesel B7 (no meter reading)",
             CardName="Petronas Dagangan Berhad", AcctCode="5200",
             Quantity="", UoM="", LineTotal="800.00", Currency="MYR"),
        _row(DocEntry="18240", LineNum="0", DocNum="AP-2026-030", DocDate="2026-05-22",
             ItemCode="PAPER-A4", ItemName="Copier paper A4",
             CardName="Office Depot", AcctCode="6100",
             Quantity="50", UoM="BOX", LineTotal="250.00", Currency="MYR"),
        # planted duplicate: this DocNum is already captured in e-Claim
        _row(DocEntry="18250", LineNum="0", DocNum="AP-2026-042", DocDate="2026-05-27",
             ItemCode="DIESEL-B7", ItemName="Diesel B7 (claimed staff fuel)",
             CardName="Petronas Dagangan Berhad", AcctCode="5200",
             Quantity="300", UoM="L", LineTotal="900.00", Currency="MYR"),
    ]


def malformed_rows() -> list[dict[str, str]]:
    """The clean month with one malformed row (bad LineTotal) inserted."""
    rows = month_rows()
    rows.insert(
        3,
        _row(DocEntry="18999", LineNum="0", DocNum="AP-2026-099", DocDate="2026-05-31",
             ItemCode="DIESEL-B7", ItemName="Diesel B7",
             CardName="Petronas Dagangan Berhad", AcctCode="5200",
             Quantity="100", UoM="L", LineTotal="N/A", Currency="MYR"),
    )
    return rows


def write_csv(path: str | Path, rows: list[dict[str, str]]) -> None:
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(path: str | Path, rows: list[dict[str, str]]) -> None:
    from openpyxl import Workbook

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append([r[c] for c in HEADER])
    wb.save(path)


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "data" / "synthetic"
    write_csv(out / "abc_month_2026_05.csv", month_rows())
    write_xlsx(out / "abc_month_2026_05.xlsx", month_rows())
    write_csv(out / "abc_malformed.csv", malformed_rows())
    print(f"Wrote synthetic listings to {out}")


if __name__ == "__main__":
    main()
