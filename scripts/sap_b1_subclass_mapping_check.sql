-- SAP B1: Subclass ID and Name Mapping Audit (Before & After)
-- Purpose: Check and compare subclass mappings between ORSC (Resource Subclass) and OITM (Items)

-- ============================================================================
-- PART 1: IDENTIFY MISSING OR MISMATCHED SUBCLASS MAPPINGS IN OITM
-- ============================================================================
PRINT '=== BEFORE: Current State of Subclass Assignments ==='
SELECT
    'BEFORE - Current State' as Status,
    i.[ItemCode],
    i.[ItemName],
    ISNULL(i.[AssetClass], 'NULL') as CurrentSubclassInOITM,
    ISNULL(rs.[Code], 'INVALID') as SubclassCodeInORSC,
    ISNULL(rs.[Name], 'INVALID') as SubclassNameInORSC,
    ISNULL(rs.[Class], 'INVALID') as ResourceClass,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'MISSING'
        WHEN rs.[Code] IS NULL THEN 'INVALID_CODE'
        ELSE 'VALID'
    END as IssueType,
    i.[UpdateDate] as LastOITMUpdate
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    i.[AssetClass] IS NULL
    OR rs.[Code] IS NULL
ORDER BY
    i.[ItemCode];

-- ============================================================================
-- PART 2: MAPPING CORRECTION PROPOSAL - BEFORE & AFTER
-- ============================================================================
PRINT '=== CORRECTION MAPPING: Before and After ==='
SELECT
    'BEFORE-AFTER' as Status,
    i.[ItemCode],
    i.[ItemName],
    ISNULL(i.[AssetClass], 'UNASSIGNED') as BeforeSubclassCode,
    ISNULL(rs.[Code], 'NEEDS_ASSIGNMENT') as AfterSubclassCode,
    ISNULL(rs.[Name], 'N/A') as SubclassName,
    ISNULL(rs.[Class], 'N/A') as ResourceClass,
    ISNULL(rs.[Category], 'N/A') as Category,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'ACTION: Assign Subclass'
        WHEN rs.[Code] IS NULL THEN 'ACTION: Invalid subclass - needs correction'
        ELSE 'OK'
    END as MappingAction
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    i.[AssetClass] IS NULL
    OR rs.[Code] IS NULL
ORDER BY
    i.[ItemCode];

-- ============================================================================
-- PART 3: SUMMARY STATISTICS
-- ============================================================================
PRINT '=== SUMMARY STATISTICS ==='
SELECT
    'SUMMARY' as Status,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] IS NULL) as ItemsWithoutSubclass,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] IS NOT NULL) as ItemsWithSubclass,
    (SELECT COUNT(DISTINCT [AssetClass]) FROM [dbo].[OITM] WHERE [AssetClass] IS NOT NULL) as UniqueSubclassesUsed,
    (SELECT COUNT(DISTINCT [Code]) FROM [dbo].[ORSC]) as TotalAvailableSubclasses,
    (SELECT COUNT(*) FROM [dbo].[OITM]) as TotalItems;

-- ============================================================================
-- PART 4: SUBCLASS INVENTORY - REFERENCE
-- ============================================================================
PRINT '=== SUBCLASS INVENTORY REFERENCE ==='
SELECT
    'REFERENCE' as Status,
    [Code] as SubclassCode,
    [Name] as SubclassName,
    [Class] as ResourceClass,
    [SubClassCode],
    [Category],
    [ReceiptTolerance],
    [CreateDate],
    [UpdateDate]
FROM
    [dbo].[ORSC]
ORDER BY
    [Code];
