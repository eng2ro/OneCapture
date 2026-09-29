-- SAP B1: Subclass ID and Name Mapping Audit (Before & After)
-- Purpose: Check and compare subclass mappings between ORSC (Resource Subclass) and OITM (Items)
-- Shows before (current OITM) and after (corrected) mapping information

-- ============================================================================
-- PART 1: IDENTIFY MISSING OR MISMATCHED SUBCLASS MAPPINGS IN OITM
-- ============================================================================
SELECT
    '[BEFORE - Current State]' as CheckPoint,
    i.[ItemCode] as ItemCode,
    i.[ItemName] as ItemName,
    ISNULL(i.[AssetClass], 'NULL') as CurrentSubclassInOITM,
    rs.[Code] as SubclassCodeInORSC,
    rs.[Name] as SubclassNameInORSC,
    rs.[Class] as ResourceClass,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'MISSING - No subclass assigned'
        WHEN i.[AssetClass] <> rs.[Code] THEN 'MISMATCH - Different subclass code'
        ELSE 'MATCH'
    END as IssueType,
    i.[UpdateDate] as LastOITMUpdate,
    rs.[CreateDate] as SubclassCreateDate,
    rs.[UpdateDate] as SubclassUpdateDate
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    -- Look for items with missing or mismatched subclass
    i.[AssetClass] IS NULL
    OR i.[AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC])
    OR (i.[AssetClass] <> '' AND i.[AssetClass] IS NOT NULL
        AND ISNULL(rs.[Name], '') = '')
ORDER BY
    i.[ItemCode];

-- ============================================================================
-- PART 2: MAPPING CORRECTION PROPOSAL - BEFORE & AFTER
-- ============================================================================
SELECT
    '[CORRECTION MAPPING]' as CheckPoint,
    i.[ItemCode] as ItemCode,
    i.[ItemName] as ItemName,
    ISNULL(i.[AssetClass], 'UNASSIGNED') as BeforeSubclassCode,
    rs.[Code] as AfterSubclassCode,
    rs.[Name] as SubclassName,
    rs.[Class] as ResourceClass,
    rs.[Category] as SubclassCategory,
    rs.[ReceiptTolerance] as TolerancePercent,
    CASE
        WHEN i.[AssetClass] IS NULL THEN 'ASSIGN: First time mapping'
        WHEN i.[AssetClass] <> rs.[Code] THEN CONCAT('CORRECT: ', i.[AssetClass], ' → ', rs.[Code])
        ELSE 'NO CHANGE'
    END as MappingAction,
    i.[CreateDate] as ItemCreatedDate,
    i.[UpdateDate] as ItemLastUpdated
FROM
    [dbo].[OITM] i
LEFT JOIN
    [dbo].[ORSC] rs ON i.[AssetClass] = rs.[Code]
WHERE
    i.[AssetClass] IS NULL OR i.[AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC])
ORDER BY
    i.[ItemCode];

-- ============================================================================
-- PART 3: SUMMARY STATISTICS
-- ============================================================================
SELECT
    '[SUMMARY]' as CheckPoint,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] IS NULL) as ItemsWithoutSubclass,
    (SELECT COUNT(*) FROM [dbo].[OITM] WHERE [AssetClass] NOT IN (SELECT [Code] FROM [dbo].[ORSC])) as ItemsWithInvalidSubclass,
    (SELECT COUNT(*) FROM [dbo].[OITM]) as TotalItems,
    (SELECT COUNT(DISTINCT [Code]) FROM [dbo].[ORSC]) as TotalAvailableSubclasses,
    (SELECT COUNT(DISTINCT [AssetClass]) FROM [dbo].[OITM] WHERE [AssetClass] IS NOT NULL) as UniqueSubclassesUsedInOITM;

-- ============================================================================
-- PART 4: SUBCLASS INVENTORY - ALL AVAILABLE SUBCLASSES FOR REFERENCE
-- ============================================================================
SELECT
    '[REFERENCE - All Available Subclasses]' as CheckPoint,
    [Code] as SubclassCode,
    [Name] as SubclassName,
    [Class] as ResourceClass,
    [SubClassCode] as SubClassCode,
    [Category] as Category,
    [ReceiptTolerance] as Tolerance,
    [CreateDate] as CreatedDate,
    [UpdateDate] as UpdatedDate,
    [Canceled] as IsCanceled
FROM
    [dbo].[ORSC]
ORDER BY
    [Code];
