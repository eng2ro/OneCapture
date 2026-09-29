#!/usr/bin/env python3
"""
SAP B1 Subclass and Category Mapping Audit Tool

Purpose:
  - Check and map subclass IDs and names from ORSC to OITM
  - Verify category assignments in subclass table
  - Generate before/after mapping corrections
  - Export audit reports

Usage:
  python sap_b1_mapping_audit.py --server <server> --user <user> --password <password> --db <database>
  python sap_b1_mapping_audit.py --config-file config.ini  # Load from config file
  python sap_b1_mapping_audit.py --audit-type subclass  # Run only subclass audit
  python sap_b1_mapping_audit.py --audit-type category  # Run only category audit
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import pyodbc
    import pandas as pd
except ImportError:
    print("ERROR: Required packages not found. Install with:")
    print("  pip install pyodbc pandas")
    sys.exit(1)


class SAPB1MappingAudit:
    """Audit SAP B1 subclass and category mappings."""

    def __init__(self, connection_string: str):
        """Initialize with SAP B1 database connection string."""
        self.connection_string = connection_string
        self.conn = None
        self.audit_results = {}

    def connect(self) -> bool:
        """Establish database connection."""
        try:
            self.conn = pyodbc.connect(self.connection_string)
            print("✓ Connected to SAP B1 database")
            return True
        except pyodbc.Error as e:
            print(f"✗ Connection failed: {e}")
            return False

    def disconnect(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            print("✓ Disconnected from database")

    def execute_query(self, query: str) -> Optional[pd.DataFrame]:
        """Execute SQL query and return results as DataFrame."""
        try:
            return pd.read_sql(query, self.conn)
        except pyodbc.Error as e:
            print(f"✗ Query execution failed: {e}")
            return None

    def audit_subclass_mapping(self) -> dict:
        """Audit subclass mappings between ORSC and OITM."""
        print("\n" + "="*80)
        print("SUBCLASS MAPPING AUDIT")
        print("="*80)

        results = {
            "audit_type": "subclass",
            "timestamp": datetime.now().isoformat(),
            "findings": {}
        }

        # Query 1: Missing/Mismatched Subclass Assignments
        print("\n[1/4] Checking for missing or mismatched subclass assignments...")
        query1 = """
        SELECT
            i.[ItemCode],
            i.[ItemName],
            ISNULL(i.[AssetClass], 'NULL') as CurrentSubclassInOITM,
            rs.[Code] as SubclassCodeInORSC,
            rs.[Name] as SubclassNameInORSC,
            CASE
                WHEN i.[AssetClass] IS NULL THEN 'MISSING'
                WHEN i.[AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC]) THEN 'INVALID'
                ELSE 'MATCH'
            END as IssueType
        FROM
            [dbo].[OITM] i
        LEFT JOIN
            [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
        WHERE
            i.[AssetClass] IS NULL
            OR i.[AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC])
        """

        df_issues = self.execute_query(query1)
        if df_issues is not None:
            results["findings"]["issues_found"] = len(df_issues)
            results["findings"]["issues"] = df_issues.to_dict('records')
            print(f"  Found {len(df_issues)} items with issues")
            print(df_issues.head(10).to_string())

        # Query 2: Summary Statistics
        print("\n[2/4] Generating summary statistics...")
        query2 = """
        SELECT
            COUNT(*) as TotalItems,
            SUM(CASE WHEN [AssetClass] IS NULL THEN 1 ELSE 0 END) as ItemsWithoutSubclass,
            SUM(CASE WHEN [AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC]) THEN 1 ELSE 0 END) as ItemsWithInvalidSubclass,
            COUNT(DISTINCT [AssetClass]) as UniqueSubclassesUsed,
            (SELECT COUNT(DISTINCT [Code]) FROM [dbo].[ORSC]) as TotalAvailableSubclasses
        FROM
            [dbo].[OITM]
        """

        df_stats = self.execute_query(query2)
        if df_stats is not None:
            stats = df_stats.iloc[0].to_dict()
            results["findings"]["summary"] = stats
            print(f"  Total Items: {stats['TotalItems']}")
            print(f"  Items without subclass: {stats['ItemsWithoutSubclass']}")
            print(f"  Items with invalid subclass: {stats['ItemsWithInvalidSubclass']}")
            print(f"  Unique subclasses used: {stats['UniqueSubclassesUsed']}/{stats['TotalAvailableSubclasses']}")

        # Query 3: Subclass Inventory
        print("\n[3/4] Retrieving all available subclasses...")
        query3 = """
        SELECT
            [Code] as SubclassCode,
            [Name] as SubclassName,
            [Class] as ResourceClass,
            [Category] as Category,
            [ReceiptTolerance] as Tolerance,
            [Canceled] as IsCanceled
        FROM
            [dbo].[ORSC]
        ORDER BY
            [Code]
        """

        df_subclasses = self.execute_query(query3)
        if df_subclasses is not None:
            results["findings"]["subclass_inventory"] = df_subclasses.to_dict('records')
            print(f"  Total subclasses: {len(df_subclasses)}")
            print(df_subclasses.head(10).to_string())

        # Query 4: Mapping Correction Proposal
        print("\n[4/4] Generating correction mapping proposal...")
        query4 = """
        SELECT
            i.[ItemCode],
            i.[ItemName],
            ISNULL(i.[AssetClass], 'UNASSIGNED') as BeforeSubclass,
            ISNULL(rs.[Code], 'NEEDS_ASSIGNMENT') as AfterSubclass,
            rs.[Name] as SubclassName,
            rs.[Category] as Category
        FROM
            [dbo].[OITM] i
        LEFT JOIN
            [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
        WHERE
            i.[AssetClass] IS NULL
            OR i.[AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC])
        """

        df_corrections = self.execute_query(query4)
        if df_corrections is not None:
            results["findings"]["corrections"] = df_corrections.to_dict('records')
            print(f"  Correction proposals generated for {len(df_corrections)} items")

        self.audit_results["subclass"] = results
        return results

    def audit_category_mapping(self) -> dict:
        """Audit category mappings in subclass and items."""
        print("\n" + "="*80)
        print("CATEGORY MAPPING AUDIT")
        print("="*80)

        results = {
            "audit_type": "category",
            "timestamp": datetime.now().isoformat(),
            "findings": {}
        }

        # Query 1: Categories in Subclass Table
        print("\n[1/4] Analyzing categories in ORSC (Subclass table)...")
        query1 = """
        SELECT
            [Code] as SubclassCode,
            [Name] as SubclassName,
            [Category] as CategoryID,
            [Class] as ResourceClass,
            COUNT(*) OVER (PARTITION BY [Category]) as ItemsUsingThisCategory
        FROM
            [dbo].[ORSC]
        WHERE
            [Category] IS NOT NULL
        ORDER BY
            [Category], [Code]
        """

        df_categories = self.execute_query(query1)
        if df_categories is not None:
            results["findings"]["categories"] = df_categories.to_dict('records')
            print(f"  Found {df_categories['CategoryID'].nunique()} unique categories")
            print(df_categories.head(10).to_string())

        # Query 2: Category Trace in OITM
        print("\n[2/4] Tracing category assignments in OITM...")
        query2 = """
        SELECT
            i.[ItemCode],
            i.[ItemName],
            i.[AssetClass] as SubclassCode,
            rs.[Name] as SubclassName,
            rs.[Category] as CategoryFromSubclass,
            CASE
                WHEN i.[AssetClass] IS NULL THEN 'NO_SUBCLASS'
                WHEN rs.[Category] IS NULL THEN 'NO_CATEGORY'
                ELSE 'HAS_CATEGORY'
            END as CategoryStatus
        FROM
            [dbo].[OITM] i
        LEFT JOIN
            [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
        WHERE
            i.[AssetClass] IS NOT NULL
        ORDER BY
            rs.[Category], i.[ItemCode]
        """

        df_item_categories = self.execute_query(query2)
        if df_item_categories is not None:
            results["findings"]["item_categories"] = df_item_categories.to_dict('records')
            print(f"  Analyzed {len(df_item_categories)} items")
            print(df_item_categories.head(10).to_string())

        # Query 3: Items Missing Category Assignment
        print("\n[3/4] Identifying items needing category assignment...")
        query3 = """
        SELECT
            i.[ItemCode],
            i.[ItemName],
            ISNULL(i.[AssetClass], 'NULL') as SubclassCode,
            CASE
                WHEN i.[AssetClass] IS NULL THEN 'ASSIGN_SUBCLASS'
                WHEN rs.[Category] IS NULL THEN 'ADD_CATEGORY_TO_SUBCLASS'
                ELSE 'OK'
            END as RequiredAction,
            rs.[Name] as SubclassName
        FROM
            [dbo].[OITM] i
        LEFT JOIN
            [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
        WHERE
            i.[AssetClass] IS NULL
            OR rs.[Category] IS NULL
        ORDER BY
            RequiredAction, i.[ItemCode]
        """

        df_needed = self.execute_query(query3)
        if df_needed is not None:
            results["findings"]["needed_corrections"] = df_needed.to_dict('records')
            print(f"  Items needing corrections: {len(df_needed)}")
            print(df_needed.head(10).to_string())

        # Query 4: Summary Statistics
        print("\n[4/4] Generating category summary statistics...")
        query4 = """
        SELECT
            ISNULL(rs.[Category], 'UNCATEGORIZED') as Category,
            COUNT(DISTINCT i.[ItemCode]) as ItemCount,
            COUNT(DISTINCT i.[AssetClass]) as SubclassVariety
        FROM
            [dbo].[OITM] i
        LEFT JOIN
            [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
        WHERE
            i.[AssetClass] IS NOT NULL
        GROUP BY
            rs.[Category]
        ORDER BY
            Category
        """

        df_cat_summary = self.execute_query(query4)
        if df_cat_summary is not None:
            results["findings"]["category_summary"] = df_cat_summary.to_dict('records')
            print(f"  Category distribution:")
            print(df_cat_summary.to_string())

        self.audit_results["category"] = results
        return results

    def export_report(self, output_file: str):
        """Export audit results to JSON file."""
        output_path = Path(output_file)
        output_path.write_text(json.dumps(self.audit_results, indent=2, default=str))
        print(f"\n✓ Report exported to {output_path}")


def build_connection_string(server: str, database: str, user: str, password: str) -> str:
    """Build SAP B1 connection string."""
    return f"Driver={{ODBC Driver 17 for SQL Server}};Server={server};Database={database};UID={user};PWD={password}"


def main():
    parser = argparse.ArgumentParser(
        description="SAP B1 Subclass and Category Mapping Audit Tool"
    )

    parser.add_argument("--server", help="SAP B1 SQL Server hostname")
    parser.add_argument("--database", default="SBO_DEFAULT", help="SAP B1 database name")
    parser.add_argument("--user", help="Database user")
    parser.add_argument("--password", help="Database password")
    parser.add_argument(
        "--audit-type",
        choices=["all", "subclass", "category"],
        default="all",
        help="Type of audit to run"
    )
    parser.add_argument(
        "--output",
        default=f"sap_b1_mapping_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        help="Output file for audit report"
    )

    args = parser.parse_args()

    if not args.server or not args.user or not args.password:
        print("ERROR: Missing required connection parameters")
        print("Use: python sap_b1_mapping_audit.py --server <server> --user <user> --password <password>")
        sys.exit(1)

    # Build connection and run audit
    conn_str = build_connection_string(args.server, args.database, args.user, args.password)
    audit = SAPB1MappingAudit(conn_str)

    if not audit.connect():
        sys.exit(1)

    try:
        if args.audit_type in ["all", "subclass"]:
            audit.audit_subclass_mapping()

        if args.audit_type in ["all", "category"]:
            audit.audit_category_mapping()

        audit.export_report(args.output)
        print(f"\n✓ Audit complete. Results saved to {args.output}")

    finally:
        audit.disconnect()


if __name__ == "__main__":
    main()
