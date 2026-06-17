using DevLoop.SqlDiagnosticsMcp.Configuration;
using DevLoop.SqlDiagnosticsMcp.Models;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Reads SQL Server schema and index metadata for diagnostics.
/// </summary>
public sealed class SqlSchemaReader
{
    private readonly SqlDiagnosticsConfiguration _configuration;
    private readonly ILogger<SqlSchemaReader> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="SqlSchemaReader"/> class.
    /// </summary>
    /// <param name="configuration">The SQL diagnostics configuration.</param>
    /// <param name="logger">The structured logger.</param>
    public SqlSchemaReader(
        SqlDiagnosticsConfiguration configuration,
        ILogger<SqlSchemaReader> logger)
    {
        _configuration = configuration;
        _logger = logger;
    }

    /// <summary>
    /// Describes the largest tables and indexes in a configured database.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="topTables">The maximum number of table summaries to return.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>The database schema description.</returns>
    public async Task<SqlDatabaseDescription> DescribeDatabaseAsync(
        string connectionName,
        int topTables,
        CancellationToken cancellationToken)
    {
        var profile = _configuration.GetRequiredConnection(connectionName);
        var result = new SqlDatabaseDescription
        {
            ConnectionName = profile.Name
        };

        try
        {
            await using var connection = new SqlConnection(profile.ConnectionString);
            await connection.OpenAsync(cancellationToken).ConfigureAwait(false);

            var tableRows = await SqlDiagnosticsService.ExecuteInternalRowsAsync(
                connection,
                BuildTableSummarySql(Math.Clamp(topTables, 1, 200)),
                _configuration.GetCommandTimeoutSeconds(profile),
                _configuration.GetMaxTextLength(),
                Math.Clamp(topTables, 1, 200),
                cancellationToken).ConfigureAwait(false);

            result.Tables = tableRows
                .Select(row => new SqlTableSummary(
                    row["SchemaName"]?.ToString() ?? string.Empty,
                    row["TableName"]?.ToString() ?? string.Empty,
                    Convert.ToInt64(row["RowCount"] ?? 0),
                    Convert.ToDecimal(row["TotalMegabytes"] ?? 0),
                    Convert.ToInt32(row["ColumnCount"] ?? 0),
                    Convert.ToInt32(row["IndexCount"] ?? 0)))
                .ToList();

            result.Indexes = await TryReadRowsAsync(
                connection,
                BuildIndexSummarySql(Math.Clamp(topTables * 5, 10, 500)),
                profile,
                Math.Clamp(topTables * 5, 10, 500),
                cancellationToken).ConfigureAwait(false);
            result.Succeeded = true;
        }
        catch (Exception ex) when (ex is SqlException or InvalidOperationException or TimeoutException)
        {
            _logger.LogWarning(ex, "Database description failed for {ConnectionName}", profile.Name);
            result.Succeeded = false;
            result.ErrorMessage = SqlDiagnosticsService.SanitizeException(ex);
        }

        return result;
    }

    /// <summary>
    /// Reads table and index health metadata for one table or the largest tables.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="tableName">Optional schema-qualified table name.</param>
    /// <param name="topTables">The maximum number of tables when no table is specified.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>The table health result.</returns>
    public async Task<SqlTableHealthResult> ReadTableHealthAsync(
        string connectionName,
        string? tableName,
        int topTables,
        CancellationToken cancellationToken)
    {
        var profile = _configuration.GetRequiredConnection(connectionName);
        var result = new SqlTableHealthResult
        {
            ConnectionName = profile.Name
        };

        try
        {
            await using var connection = new SqlConnection(profile.ConnectionString);
            await connection.OpenAsync(cancellationToken).ConfigureAwait(false);

            var objectFilter = string.IsNullOrWhiteSpace(tableName)
                ? null
                : await ResolveObjectIdAsync(connection, tableName, _configuration.GetCommandTimeoutSeconds(profile), cancellationToken).ConfigureAwait(false);

            result.Tables = await SqlDiagnosticsService.ExecuteInternalRowsAsync(
                connection,
                BuildTableHealthSql(objectFilter, Math.Clamp(topTables, 1, 50)),
                _configuration.GetCommandTimeoutSeconds(profile),
                _configuration.GetMaxTextLength(),
                Math.Clamp(topTables, 1, 50),
                cancellationToken).ConfigureAwait(false);

            result.IndexUsage = await TryReadRowsAsync(
                connection,
                BuildIndexUsageSql(objectFilter, Math.Clamp(topTables * 20, 20, 1000)),
                profile,
                Math.Clamp(topTables * 20, 20, 1000),
                cancellationToken).ConfigureAwait(false);
            result.Recommendations = BuildTableHealthRecommendations(result);
            result.Succeeded = true;
        }
        catch (Exception ex) when (ex is SqlException or InvalidOperationException or TimeoutException)
        {
            _logger.LogWarning(ex, "Table health read failed for {ConnectionName}", profile.Name);
            result.Succeeded = false;
            result.ErrorMessage = SqlDiagnosticsService.SanitizeException(ex);
        }

        return result;
    }

    private async Task<IReadOnlyList<IReadOnlyDictionary<string, object?>>> TryReadRowsAsync(
        SqlConnection connection,
        string sql,
        SqlConnectionOptions profile,
        int maxRows,
        CancellationToken cancellationToken)
    {
        try
        {
            return await SqlDiagnosticsService.ExecuteInternalRowsAsync(
                connection,
                sql,
                _configuration.GetCommandTimeoutSeconds(profile),
                _configuration.GetMaxTextLength(),
                maxRows,
                cancellationToken).ConfigureAwait(false);
        }
        catch (SqlException ex)
        {
            _logger.LogInformation(ex, "Optional schema diagnostic query was unavailable for {ConnectionName}", profile.Name);
            return [];
        }
    }

    private static async Task<int?> ResolveObjectIdAsync(
        SqlConnection connection,
        string tableName,
        int commandTimeoutSeconds,
        CancellationToken cancellationToken)
    {
        await using var command = connection.CreateCommand();
        command.CommandTimeout = commandTimeoutSeconds;
        command.CommandText = "SELECT OBJECT_ID(@tableName);";
        command.Parameters.AddWithValue("@tableName", tableName);
        var value = await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false);
        return value is null or DBNull ? null : Convert.ToInt32(value);
    }

    private static string BuildTableSummarySql(int topTables)
    {
        return $$"""
            SELECT TOP ({{topTables}})
                s.name AS SchemaName,
                t.name AS TableName,
                SUM(p.rows) AS RowCount,
                CAST(SUM(a.total_pages) * 8.0 / 1024.0 AS decimal(18, 2)) AS TotalMegabytes,
                COUNT(DISTINCT c.column_id) AS ColumnCount,
                COUNT(DISTINCT i.index_id) AS IndexCount
            FROM sys.tables AS t
            INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            INNER JOIN sys.indexes AS i ON i.object_id = t.object_id
            INNER JOIN sys.partitions AS p ON p.object_id = i.object_id AND p.index_id = i.index_id
            INNER JOIN sys.allocation_units AS a ON a.container_id = p.partition_id
            LEFT JOIN sys.columns AS c ON c.object_id = t.object_id
            WHERE t.is_ms_shipped = 0
              AND i.index_id IN (0, 1)
            GROUP BY s.name, t.name
            ORDER BY TotalMegabytes DESC, RowCount DESC;
            """;
    }

    private static string BuildIndexSummarySql(int topIndexes)
    {
        return $$"""
            SELECT TOP ({{topIndexes}})
                s.name AS SchemaName,
                t.name AS TableName,
                i.name AS IndexName,
                i.type_desc AS IndexType,
                i.is_unique AS IsUnique,
                i.is_primary_key AS IsPrimaryKey,
                i.has_filter AS HasFilter
            FROM sys.indexes AS i
            INNER JOIN sys.tables AS t ON t.object_id = i.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            WHERE t.is_ms_shipped = 0
              AND i.index_id > 0
            ORDER BY s.name, t.name, i.index_id;
            """;
    }

    private static string BuildTableHealthSql(int? objectId, int topTables)
    {
        var filter = objectId.HasValue
            ? $"AND t.object_id = {objectId.Value}"
            : string.Empty;

        return $$"""
            SELECT TOP ({{topTables}})
                s.name AS SchemaName,
                t.name AS TableName,
                SUM(p.rows) AS RowCount,
                CAST(SUM(a.total_pages) * 8.0 / 1024.0 AS decimal(18, 2)) AS TotalMegabytes,
                MAX(sp.last_updated) AS LastStatisticsUpdate
            FROM sys.tables AS t
            INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            INNER JOIN sys.indexes AS i ON i.object_id = t.object_id
            INNER JOIN sys.partitions AS p ON p.object_id = i.object_id AND p.index_id = i.index_id
            INNER JOIN sys.allocation_units AS a ON a.container_id = p.partition_id
            OUTER APPLY sys.dm_db_stats_properties(t.object_id, i.index_id) AS sp
            WHERE t.is_ms_shipped = 0
              AND i.index_id IN (0, 1)
              {{filter}}
            GROUP BY s.name, t.name
            ORDER BY TotalMegabytes DESC, RowCount DESC;
            """;
    }

    private static string BuildIndexUsageSql(int? objectId, int topIndexes)
    {
        var filter = objectId.HasValue
            ? $"AND t.object_id = {objectId.Value}"
            : string.Empty;

        return $$"""
            SELECT TOP ({{topIndexes}})
                s.name AS SchemaName,
                t.name AS TableName,
                i.name AS IndexName,
                i.type_desc AS IndexType,
                COALESCE(us.user_seeks, 0) AS UserSeeks,
                COALESCE(us.user_scans, 0) AS UserScans,
                COALESCE(us.user_lookups, 0) AS UserLookups,
                COALESCE(us.user_updates, 0) AS UserUpdates
            FROM sys.indexes AS i
            INNER JOIN sys.tables AS t ON t.object_id = i.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            LEFT JOIN sys.dm_db_index_usage_stats AS us
                ON us.database_id = DB_ID()
               AND us.object_id = i.object_id
               AND us.index_id = i.index_id
            WHERE t.is_ms_shipped = 0
              AND i.index_id > 0
              {{filter}}
            ORDER BY COALESCE(us.user_updates, 0) DESC, COALESCE(us.user_seeks, 0) ASC;
            """;
    }

    private static IReadOnlyList<string> BuildTableHealthRecommendations(SqlTableHealthResult result)
    {
        var recommendations = new List<string>();

        if (result.IndexUsage.Count == 0)
        {
            recommendations.Add("Index usage data was unavailable or empty. Confirm DMV permissions before drawing index conclusions.");
        }

        var unusedIndexes = result.IndexUsage.Count(row =>
            Convert.ToInt64(row.GetValueOrDefault("UserSeeks") ?? 0) == 0 &&
            Convert.ToInt64(row.GetValueOrDefault("UserScans") ?? 0) == 0 &&
            Convert.ToInt64(row.GetValueOrDefault("UserLookups") ?? 0) == 0 &&
            Convert.ToInt64(row.GetValueOrDefault("UserUpdates") ?? 0) > 100);

        if (unusedIndexes > 0)
        {
            recommendations.Add($"{unusedIndexes} index(es) have updates but no visible reads. Treat as manual_review_required before dropping or changing anything.");
        }

        if (recommendations.Count == 0)
        {
            recommendations.Add("No obvious table-health issue was detected from visible metadata. Compare with query-specific plans before changing indexes.");
        }

        return recommendations;
    }
}

