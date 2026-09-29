-- SAP B1: Category ID and Name Mapping Audit (Before & After)
-- Purpose: Check category assignments in subclass and their usage in items

-- ============================================================================
-- PART 1: CATEGORIES IN SUBCLASS TABLE - CURRENT STATE
-- ============================================================================
PRINT '=== BEFORE: Categories in ORSC (Subclass Table) ==='
SELECT
    'BEFORE' as Status,
    [Code] as SubclassCode,
    [Name] as SubclassName,
    [Category] as CategoryIDInORSC,
    [Class] as ResourceClass,
    [SubClassCode],
    [CreateDate],
    [UpdateDate]
FROM
    [dbo].[ORSC]
WHERE
    [Category] IS NOT NULL
ORDER BY
    [Category], [Code];

-- ============================================================================
-- PART 2: CATEGORY MAPPING IN OITM - TRACE FROM ITEM TO CATEGORY
-- ============================================================================
PRINT '=== CATEGORY TRACE IN OITM ==='
SELECT
    'TRACE' as Status,
    i.[ItemCode],
    i.[ItemName],
    i.[AssetClass] as ItemSubclassCode,
    rs.[Name] as SubclassName,
    rs.[Category] as CategoryFromSubclass,
    i.[CreateDate] as ItemCreatedDate,
    i.[UpdateDate] as ItemUpdatedDate,
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
    ISNULL(rs.[Category], 'ZZZZZ'), i.[ItemCode];

-- ============================================================================
-- PART 3: ITEMS MISSING CATEGORY ASSIGNMENT
-- ============================================================================
PRINT '=== CORRECTION NEEDED: Items Missing Categories ==='
SELECT
    'CORRECTION_NEEDED' as Status,
    i.[ItemCode],
    i.[ItemName],
    ISNULL(i.[AssetClass], 'NULL') as CurrentSubclass,
    ISNULL(rs.[Category], 'MISSING') as CategoryStatus,
    ISNULL(rs.[Name], 'INVALID') as SubclassName,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'ACTION: Assign Subclass first'
        WHEN rs.[Category] IS NULL THEN 'ACTION: Add category to subclass in ORSC'
        ELSE 'OK'
    END as RequiredAction,
    i.[UpdateDate] as LastItemUpdate
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    i.[AssetClass] IS NULL
    OR rs.[Category] IS NULL
ORDER BY
    RequiredAction, i.[ItemCode];

-- ============================================================================
-- PART 4: CATEGORY DISTRIBUTION
-- ============================================================================
PRINT '=== CATEGORY DISTRIBUTION ==='
SELECT
    'DISTRIBUTION' as Status,
    ISNULL(rs.[Category], 'UNCATEGORIZED') as Category,
    COUNT(DISTINCT i.[ItemCode]) as ItemCount,
    COUNT(DISTINCT i.[AssetClass]) as SubclassVariety,
    MIN(i.[CreateDate]) as EarliestItemDate,
    MAX(i.[UpdateDate]) as LatestItemUpdate
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    i.[AssetClass] IS NOT NULL
GROUP BY
    rs.[Category]
ORDER BY
    Category;

-- ============================================================================
-- PART 5: CATEGORY MASTER LIST - REFERENCE
-- ============================================================================
PRINT '=== REFERENCE: Category Master List ==='
SELECT DISTINCT
    'REFERENCE' as Status,
    [Category] as CategoryID,
    [Class] as ResourceClass,
    COUNT(*) OVER (PARTITION BY [Category]) as SubclassCount
FROM
    [dbo].[ORSC]
WHERE
    [Category] IS NOT NULL
ORDER BY
    [Category];

-- ============================================================================
-- PART 6: SUBCLASS TO CATEGORY MAPPING TABLE
-- ============================================================================
PRINT '=== MAPPING REFERENCE TABLE ==='
SELECT
    'MAPPING_TABLE' as Status,
    [Code] as SubclassCode,
    [Name] as SubclassName,
    [Class] as ResourceClass,
    [Category] as AssignedCategory,
    [SubClassCode],
    [CreateDate],
    [UpdateDate]
FROM
    [dbo].[ORSC]
ORDER BY
    [Code];

-- ============================================================================
-- PART 7: SUMMARY STATISTICS
-- ============================================================================
PRINT '=== SUMMARY STATISTICS ==='
SELECT
    'SUMMARY' as Status,
    (SELECT COUNT(DISTINCT [Category]) FROM [dbo].[ORSC] WHERE [Category] IS NOT NULL) as DistinctCategoriesInORSC,
    (SELECT COUNT(*) FROM [dbo].[ORSC] WHERE [Category] IS NULL) as SubclassesWithoutCategory,
    (SELECT COUNT(DISTINCT rs.[Category]) FROM [dbo].[OITM] i JOIN [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code] WHERE rs.[Category] IS NOT NULL) as CategoriesRepresentedInOITM,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] IS NULL) as ItemsWithoutSubclass,
    (SELECT COUNT(*) FROM [dbo].[OITM]) as TotalItemsInOITM;
