/*
SQL Server diagnostics MCP local permission repair/setup script.

Purpose:
  Give the Codex MCP SQL login read-only diagnostic access to the local
  target databases:

    - TargetMainDatabase
    - TargetStoreDatabase

Run as:
  SQL Server administrator / sysadmin / securityadmin.

What this grants:
  - db_datareader in both databases
  - VIEW DATABASE STATE in both databases
  - VIEW DATABASE PERFORMANCE STATE when supported by the SQL Server version

What this intentionally does NOT grant:
  - db_owner
  - db_datawriter
  - CONTROL
  - ALTER
  - CREATE
  - sysadmin

After a database restore or transfer, rerun this script. Restores can leave the
server login present but remove or orphan the database user mapping.

Before first use:
  Replace <replace-with-strong-password> if the login does not already exist.
  If the login already exists, this script does not change its password.
*/

USE [master];
GO

IF NOT EXISTS (
    SELECT 1
    FROM sys.server_principals
    WHERE name = N'devloop_sql_reader'
)
BEGIN
    CREATE LOGIN [devloop_sql_reader]
    WITH PASSWORD = 'P@ssw0rd',
         CHECK_POLICY = ON,
         CHECK_EXPIRATION = OFF;
END;
GO

DECLARE @Databases TABLE ([Name] sysname NOT NULL PRIMARY KEY);

INSERT INTO @Databases ([Name])
VALUES (N'TargetMainDatabase'), (N'TargetStoreDatabase');

DECLARE @DatabaseName sysname;
DECLARE @Sql nvarchar(max);

DECLARE database_cursor CURSOR LOCAL FAST_FORWARD FOR
SELECT [Name]
FROM @Databases
ORDER BY [Name];

OPEN database_cursor;
FETCH NEXT FROM database_cursor INTO @DatabaseName;

WHILE @@FETCH_STATUS = 0
BEGIN
    IF DB_ID(@DatabaseName) IS NULL
    BEGIN
        RAISERROR('Skipping missing database: %s', 10, 1, @DatabaseName);
    END
    ELSE IF EXISTS (
        SELECT 1
        FROM sys.databases
        WHERE name = @DatabaseName
          AND state_desc <> N'ONLINE'
    )
    BEGIN
        RAISERROR('Skipping database because it is not ONLINE: %s', 10, 1, @DatabaseName);
    END
    ELSE
    BEGIN
        SET @Sql = N'
USE ' + QUOTENAME(@DatabaseName) + N';

IF NOT EXISTS (
    SELECT 1
    FROM sys.database_principals
    WHERE name = N''devloop_sql_reader''
)
BEGIN
    CREATE USER [devloop_sql_reader] FOR LOGIN [devloop_sql_reader];
END
ELSE
BEGIN
    -- Repairs orphaned users after restore/transfer when the name exists but
    -- the SID mapping no longer matches the server login.
    ALTER USER [devloop_sql_reader] WITH LOGIN = [devloop_sql_reader];
END;

IF IS_ROLEMEMBER(N''db_datareader'', N''devloop_sql_reader'') <> 1
BEGIN
    ALTER ROLE [db_datareader] ADD MEMBER [devloop_sql_reader];
END;

GRANT VIEW DATABASE STATE TO [devloop_sql_reader];

BEGIN TRY
    GRANT VIEW DATABASE PERFORMANCE STATE TO [devloop_sql_reader];
END TRY
BEGIN CATCH
    PRINT ''VIEW DATABASE PERFORMANCE STATE not granted in ' + @DatabaseName + N': '' + ERROR_MESSAGE();
END CATCH;
';

        EXEC sys.sp_executesql @Sql;
    END;

    FETCH NEXT FROM database_cursor INTO @DatabaseName;
END;

CLOSE database_cursor;
DEALLOCATE database_cursor;
GO

SELECT
    sp.name AS LoginName,
    sp.type_desc AS LoginType,
    sp.is_disabled AS IsDisabled,
    sp.default_database_name AS DefaultDatabaseName
FROM sys.server_principals sp
WHERE sp.name = N'devloop_sql_reader';
GO

DECLARE @VerificationDatabases TABLE ([Name] sysname NOT NULL PRIMARY KEY);

INSERT INTO @VerificationDatabases ([Name])
VALUES (N'TargetMainDatabase'), (N'TargetStoreDatabase');

DECLARE @VerificationDatabaseName sysname;
DECLARE @VerificationSql nvarchar(max);

DECLARE verification_cursor CURSOR LOCAL FAST_FORWARD FOR
SELECT [Name]
FROM @VerificationDatabases
ORDER BY [Name];

OPEN verification_cursor;
FETCH NEXT FROM verification_cursor INTO @VerificationDatabaseName;

WHILE @@FETCH_STATUS = 0
BEGIN
    IF DB_ID(@VerificationDatabaseName) IS NOT NULL
    BEGIN
        SET @VerificationSql = N'
USE ' + QUOTENAME(@VerificationDatabaseName) + N';

SELECT
    DB_NAME() AS DatabaseName,
    dp.name AS UserName,
    dp.type_desc AS UserType,
    dp.authentication_type_desc AS AuthenticationType,
    IS_ROLEMEMBER(N''db_datareader'', N''devloop_sql_reader'') AS IsDbDataReader
FROM sys.database_principals dp
WHERE dp.name = N''devloop_sql_reader'';

SELECT
    DB_NAME() AS DatabaseName,
    perm.permission_name AS PermissionName,
    perm.state_desc AS StateDescription
FROM sys.database_permissions perm
INNER JOIN sys.database_principals dp
    ON perm.grantee_principal_id = dp.principal_id
WHERE dp.name = N''devloop_sql_reader''
  AND perm.class_desc = N''DATABASE''
  AND perm.permission_name IN (
      N''VIEW DATABASE STATE'',
      N''VIEW DATABASE PERFORMANCE STATE''
  )
ORDER BY perm.permission_name;
';

        EXEC sys.sp_executesql @VerificationSql;
    END;

    FETCH NEXT FROM verification_cursor INTO @VerificationDatabaseName;
END;

CLOSE verification_cursor;
DEALLOCATE verification_cursor;
GO




