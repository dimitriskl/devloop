using DevLoop.SqlDiagnosticsMcp.Configuration;
using DevLoop.SqlDiagnosticsMcp.Models;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Reads SQL Server workload summaries from Query Store or DMVs.
/// </summary>
public sealed class SqlWorkloadReader
{
    private readonly SqlDiagnosticsConfiguration _configuration;
    private readonly ILogger<SqlWorkloadReader> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="SqlWorkloadReader"/> class.
    /// </summary>
    /// <param name="configuration">The SQL diagnostics configuration.</param>
    /// <param name="logger">The structured logger.</param>
    public SqlWorkloadReader(
        SqlDiagnosticsConfiguration configuration,
        ILogger<SqlWorkloadReader> logger)
    {
        _configuration = configuration;
        _logger = logger;
    }

    /// <summary>
    /// Reads top workload queries for a configured connection.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="lookbackHours">The workload lookback window in hours.</param>
    /// <param name="topQueries">The maximum number of queries to return.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>The workload summary.</returns>
    public async Task<SqlWorkloadSummary> ReadWorkloadSummaryAsync(
        string connectionName,
        int lookbackHours,
        int topQueries,
        CancellationToken cancellationToken)
    {
        var profile = _configuration.GetRequiredConnection(connectionName);
        var result = new SqlWorkloadSummary
        {
            ConnectionName = profile.Name
        };

        var notes = new List<string>();
        var boundedLookback = Math.Clamp(lookbackHours, 1, 720);
        var boundedTop = Math.Clamp(topQueries, 1, 100);

        try
        {
            await using var connection = new SqlConnection(profile.ConnectionString);
            await connection.OpenAsync(cancellationToken).ConfigureAwait(false);

            if (profile.EnableQueryStoreDiagnostics)
            {
                var queryStoreRows = await TryReadRowsAsync(
                    connection,
                    BuildQueryStoreSql(boundedLookback, boundedTop),
                    profile,
                    boundedTop,
                    cancellationToken).ConfigureAwait(false);

                if (queryStoreRows.Count > 0)
                {
                    result.Succeeded = true;
                    result.Source = "QueryStore";
                    result.Queries = queryStoreRows;
                    result.Notes = notes;
                    return result;
                }

                notes.Add("Query Store returned no visible rows or was unavailable for the current SQL login.");
            }

            if (profile.EnableDmvDiagnostics)
            {
                var dmvRows = await TryReadRowsAsync(
                    connection,
                    BuildDmvSql(boundedLookback, boundedTop),
                    profile,
                    boundedTop,
                    cancellationToken).ConfigureAwait(false);

                result.Succeeded = true;
                result.Source = "DMV";
                result.Queries = dmvRows;
                if (dmvRows.Count == 0)
                {
                    notes.Add("DMV workload query returned no visible rows. Check VIEW SERVER STATE or VIEW SERVER PERFORMANCE STATE permissions.");
                }
            }
            else
            {
                result.Succeeded = true;
                result.Source = "None";
                notes.Add("DMV diagnostics are disabled for this connection.");
            }
        }
        catch (Exception ex) when (ex is SqlException or InvalidOperationException or TimeoutException)
        {
            _logger.LogWarning(ex, "Workload summary failed for {ConnectionName}", profile.Name);
            result.Succeeded = false;
            result.ErrorMessage = SqlDiagnosticsService.SanitizeException(ex);
        }

        result.Notes = notes;
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
            _logger.LogInformation(ex, "Optional workload query was unavailable for {ConnectionName}", profile.Name);
            return [];
        }
    }

    private static string BuildQueryStoreSql(int lookbackHours, int topQueries)
    {
        return $$"""
            SELECT TOP ({{topQueries}})
                LEFT(qt.query_sql_text, 4000) AS QueryText,
                SUM(rs.count_executions) AS Executions,
                CAST(SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS decimal(18, 2)) AS AvgDurationMs,
                CAST(SUM(rs.avg_cpu_time * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS decimal(18, 2)) AS AvgCpuMs,
                CAST(SUM(rs.avg_logical_io_reads * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) AS decimal(18, 2)) AS AvgLogicalReads,
                MAX(rs.last_execution_time) AS LastExecutionTime
            FROM sys.query_store_query_text AS qt
            INNER JOIN sys.query_store_query AS q ON q.query_text_id = qt.query_text_id
            INNER JOIN sys.query_store_plan AS p ON p.query_id = q.query_id
            INNER JOIN sys.query_store_runtime_stats AS rs ON rs.plan_id = p.plan_id
            WHERE rs.last_execution_time >= DATEADD(hour, -{{lookbackHours}}, SYSUTCDATETIME())
            GROUP BY qt.query_sql_text
            ORDER BY AvgDurationMs DESC;
            """;
    }

    private static string BuildDmvSql(int lookbackHours, int topQueries)
    {
        return $$"""
            SELECT TOP ({{topQueries}})
                LEFT(
                    SUBSTRING(
                        st.text,
                        (qs.statement_start_offset / 2) + 1,
                        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text) ELSE qs.statement_end_offset END - qs.statement_start_offset) / 2) + 1),
                    4000) AS QueryText,
                qs.execution_count AS Executions,
                CAST(qs.total_elapsed_time / NULLIF(qs.execution_count, 0) / 1000.0 AS decimal(18, 2)) AS AvgDurationMs,
                CAST(qs.total_worker_time / NULLIF(qs.execution_count, 0) / 1000.0 AS decimal(18, 2)) AS AvgCpuMs,
                CAST(qs.total_logical_reads / NULLIF(qs.execution_count, 0) AS decimal(18, 2)) AS AvgLogicalReads,
                qs.last_execution_time AS LastExecutionTime
            FROM sys.dm_exec_query_stats AS qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
            WHERE qs.last_execution_time >= DATEADD(hour, -{{lookbackHours}}, GETDATE())
            ORDER BY AvgDurationMs DESC;
            """;
    }
}

