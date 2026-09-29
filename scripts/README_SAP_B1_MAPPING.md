# SAP B1 Subclass & Category Mapping Audit Scripts

## Overview

These scripts audit and correct SAP B1 resource subclass and category mappings between:
- **ORSC** (Resource Subclass table) - Master data definitions
- **OITM** (Items table) - Where subclass assignments are used

## Files

### 1. **sap_b1_subclass_mapping_check.sql**
T-SQL script to check and map subclass IDs and names from ORSC to OITM.

**Output Sections:**
- **BEFORE**: Current state of items with missing/mismatched subclass assignments
- **CORRECTION MAPPING**: Proposed mapping corrections showing before → after
- **SUMMARY**: Statistics on missing/invalid subclass assignments
- **REFERENCE**: Complete inventory of available subclasses

**How to use:**
```sql
-- Execute in SQL Server Management Studio or any T-SQL client
-- Connected to your SAP B1 database
sqlcmd -S <server> -d <database> -i sap_b1_subclass_mapping_check.sql -o report_subclass.txt
```

### 2. **sap_b1_category_mapping_check.sql**
T-SQL script to verify category IDs registered in ORSC subclass table and their usage in OITM items.

**Output Sections:**
- **BEFORE**: Categories defined in ORSC and their distribution
- **CATEGORY TRACE**: How categories flow from ORSC to OITM via subclass assignments
- **CORRECTION NEEDED**: Items missing category assignments
- **CATEGORY DISTRIBUTION**: Breakdown of items by category
- **REFERENCE**: Category master list
- **MAPPING REFERENCE**: Complete subclass-to-category mapping table

**How to use:**
```sql
-- Execute in SQL Server Management Studio or any T-SQL client
sqlcmd -S <server> -d <database> -i sap_b1_category_mapping_check.sql -o report_category.txt
```

### 3. **sap_b1_mapping_audit.py**
Python script that executes both audits programmatically and generates JSON reports.

**Requirements:**
```bash
pip install pyodbc pandas
# Requires ODBC Driver 17 for SQL Server
```

**Usage:**
```bash
# Run all audits
python sap_b1_mapping_audit.py \
  --server <SAP-B1-SERVER> \
  --database SBO_DEFAULT \
  --user <USERNAME> \
  --password <PASSWORD> \
  --output mapping_audit_report.json

# Run only subclass audit
python sap_b1_mapping_audit.py \
  --server <SAP-B1-SERVER> \
  --user <USERNAME> \
  --password <PASSWORD> \
  --audit-type subclass

# Run only category audit
python sap_b1_mapping_audit.py \
  --server <SAP-B1-SERVER> \
  --user <USERNAME> \
  --password <PASSWORD> \
  --audit-type category
```

**Output:**
- JSON report file with detailed findings
- Console output with key statistics and issues
- Records in format suitable for data correction workflows

## Data Mapping Reference

### Subclass Mapping (ORSC → OITM)

| Field | Table | Purpose |
|-------|-------|---------|
| Code | ORSC | Unique subclass identifier |
| Name | ORSC | Human-readable subclass name |
| AssetClass | OITM | Field where subclass code is stored |
| Class | ORSC | Resource class category |
| Category | ORSC | Category assignment for the subclass |

### Example Mapping Flow
```
Item (ItemCode=ABC-123)
  ↓
Item.AssetClass = "SV-MECH"
  ↓
ORSC.Code = "SV-MECH"
  ↓
ORSC.Name = "SERVICING (HC)"
ORSC.Category = "SV01"
```

## Before & After Information

Each script provides:

### BEFORE (Current State)
- Current assignments in OITM
- Items with missing subclass/category
- Invalid assignments
- Orphaned references

### AFTER (Proposed Corrections)
- Recommended subclass mappings
- Category assignments via subclass
- Correction actions required
- Updated status

## Common Issues & Solutions

### Issue 1: Items with NULL AssetClass
**Problem:** Items in OITM have no subclass assigned
```sql
WHERE i.[AssetClass] IS NULL
```
**Solution:** Run subclass audit to identify which subclass each item should have

### Issue 2: Invalid Subclass Codes
**Problem:** OITM references subclass codes that don't exist in ORSC
```sql
WHERE i.[AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC])
```
**Solution:** Either correct to valid subclass codes or remove invalid assignments

### Issue 3: Missing Category Assignments
**Problem:** Subclass defined in ORSC but has NULL category
```sql
WHERE rs.[Category] IS NULL
```
**Solution:** Update ORSC records to assign appropriate categories

## Export & Analysis

### Export to CSV (from SQL)
```sql
-- Query results can be saved as CSV for Excel analysis
-- In SSMS: Results → Save Results As → CSV
```

### Process JSON Output (Python)
```python
import json
import pandas as pd

with open('mapping_audit_report.json') as f:
    audit_data = json.load(f)

# Access subclass findings
subclass_issues = audit_data['subclass']['findings']['issues']
df_issues = pd.DataFrame(subclass_issues)
df_issues.to_csv('subclass_corrections.csv', index=False)

# Access category findings
category_corrections = audit_data['category']['findings']['needed_corrections']
df_category = pd.DataFrame(category_corrections)
df_category.to_csv('category_corrections.csv', index=False)
```

## Implementation Workflow

1. **Run Audit**
   ```bash
   python sap_b1_mapping_audit.py --server <SAP-B1-SERVER> --user <USER> --password <PWD>
   ```

2. **Review Findings**
   - Check issues_found count
   - Review specific items needing correction
   - Validate proposed mappings

3. **Prepare Corrections**
   - Export correction list to CSV
   - Validate corrections against business rules
   - Get approval for changes

4. **Apply Corrections** (in SAP B1)
   - Use Business One to update OITM.AssetClass field
   - Or execute UPDATE statements:
   ```sql
   UPDATE OITM 
   SET AssetClass = 'NEW_SUBCLASS_CODE'
   WHERE ItemCode = 'ITEM_CODE'
   ```

5. **Verify**
   - Re-run audit to confirm corrections applied
   - Validate category assignments resolved
   - Document changes in change log

## Database Backup Reminder
**ALWAYS backup your SAP B1 database before making corrections!**

```bash
# SQL Server backup
sqlcmd -S <server> -d master -Q "BACKUP DATABASE SBO_DEFAULT TO DISK='C:\backup\SBO_DEFAULT_backup.bak'"
```

## Support & Notes

- These scripts are **read-only** by default (queries only, no updates)
- All corrections must be reviewed and approved before implementation
- Test corrections in a non-production environment first
- Keep audit reports for audit trail purposes
- Update scripts if your SAP B1 schema differs from standard
