-- SAP B1: Category ID and Name Mapping Audit (Before & After)
-- Purpose: Check category IDs registered in ORSC (Resource Subclass) and validate against OITM (Items)
-- Shows category assignments and any mismatches

-- ============================================================================
-- PART 1: CATEGORY DATA IN SUBCLASS TABLE - CURRENT STATE
-- ============================================================================
SELECT
    '[BEFORE - Categories in ORSC]' as CheckPoint,
    [Code] as SubclassCode,
    [Name] as SubclassName,
    [Category] as CategoryIDInORSC,
    [Class] as ResourceClass,
    [SubClassCode] as SubClassCode,
    [CreateDate] as CreatedDate,
    [UpdateDate] as UpdatedDate,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] = [dbo].[ORSC].[Code]) as CountItemsUsingThisSubclass
FROM
    [dbo].[ORSC]
WHERE
    [Category] IS NOT NULL
ORDER BY
    [Category], [Code];

-- ============================================================================
-- PART 2: CATEGORY MAPPING IN OITM - TRACE FROM ITEM TO SUBCLASS TO CATEGORY
-- ============================================================================
SELECT
    '[CATEGORY TRACE IN OITM]' as CheckPoint,
    i.[ItemCode] as ItemCode,
    i.[ItemName] as ItemName,
    i.[AssetClass] as ItemSubclassCode,
    rs.[Name] as SubclassName,
    rs.[Category] as CategoryFromSubclass,
    i.[CreateDate] as ItemCreatedDate,
    i.[UpdateDate] as ItemUpdatedDate,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'NO SUBCLASS ASSIGNED'
        WHEN rs.[Category] IS NULL THEN 'SUBCLASS HAS NO CATEGORY'
        ELSE 'HAS CATEGORY'
    END as CategoryStatus
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    i.[AssetClass] IS NOT NULL
ORDER BY
    rs.[Category], i.[ItemCode];

-- ============================================================================
-- PART 3: ITEMS MISSING CATEGORY ASSIGNMENT (CORRECTION NEEDED)
-- ============================================================================
SELECT
    '[CORRECTION NEEDED]' as CheckPoint,
    i.[ItemCode] as ItemCode,
    i.[ItemName] as ItemName,
    ISNULL(i.[AssetClass], 'NULL') as CurrentSubclass,
    ISNULL(rs.[Category], 'MISSING') as CategoryStatus,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'ACTION: Assign Subclass first'
        WHEN rs.[Category] IS NULL THEN 'ACTION: Subclass needs category assignment in ORSC'
        ELSE 'OK'
    END as RequiredAction,
    rs.[Code] as SubclassCode,
    rs.[Name] as SubclassName,
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
-- PART 4: CATEGORY DISTRIBUTION - ITEMS BY CATEGORY
-- ============================================================================
SELECT
    '[CATEGORY DISTRIBUTION]' as CheckPoint,
    ISNULL(rs.[Category], 'UNCATEGORIZED') as Category,
    COUNT(DISTINCT i.[ItemCode]) as ItemCount,
    COUNT(DISTINCT i.[AssetClass]) as SubclassVariety,
    MIN(i.[CreateDate]) as EarliestItemDate,
    MAX(i.[UpdateDate]) as LatestItemUpdate,
    COUNT(DISTINCT CASE WHEN rs.[Category] IS NULL THEN 1 END) as ItemsWithoutCategory
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
-- PART 5: CATEGORY MASTER LIST - ALL CATEGORIES DEFINED IN ORSC
-- ============================================================================
SELECT
    '[REFERENCE - Category Master]' as CheckPoint,
    DISTINCT [Category] as CategoryID,
    [Class] as ResourceClass,
    COUNT(*) OVER (PARTITION BY [Category]) as SubclassesInCategory
FROM
    [dbo].[ORSC]
WHERE
    [Category] IS NOT NULL
ORDER BY
    [Category];

-- ============================================================================
-- PART 6: SUBCLASS TO CATEGORY MAPPING TABLE (FOR CORRECTION REFERENCE)
-- ============================================================================
SELECT
    '[MAPPING REFERENCE TABLE]' as CheckPoint,
    [Code] as SubclassCode,
    [Name] as SubclassName,
    [Class] as ResourceClass,
    [Category] as AssignedCategory,
    [SubClassCode] as SubClassDetail,
    [CreateDate] as DateCreated,
    [UpdateDate] as DateModified,
    [Canceled] as IsActive
FROM
    [dbo].[ORSC]
ORDER BY
    [Code];

-- ============================================================================
-- PART 7: SUMMARY STATISTICS
-- ============================================================================
SELECT
    '[SUMMARY STATS]' as CheckPoint,
    (SELECT COUNT(DISTINCT [Category]) FROM [dbo].[ORSC] WHERE [Category] IS NOT NULL) as DistinctCategoriesInORSC,
    (SELECT COUNT(*) FROM [dbo].[ORSC] WHERE [Category] IS NULL) as SubclassesWithoutCategory,
    (SELECT COUNT(DISTINCT rs.[Category]) FROM [dbo].[OITM] i JOIN [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code] WHERE rs.[Category] IS NOT NULL) as CategoriesRepresentedInOITM,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] IS NULL) as ItemsWithoutSubclass,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] IN (SELECT [Code] FROM [dbo].[ORSC] WHERE [Category] IS NULL)) as ItemsInUncategorizedSubclass,
    (SELECT COUNT(*) FROM [dbo].[OITM]) as TotalItemsInOITM;
