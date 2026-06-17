using System.Data;
using System.Diagnostics;
using DevLoop.SqlDiagnosticsMcp.Configuration;
using DevLoop.SqlDiagnosticsMcp.Models;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Executes bounded read-only SQL diagnostics against configured SQL Server connections.
/// </summary>
public sealed class SqlDiagnosticsService
{
    private readonly SqlDiagnosticsConfiguration _configuration;
    private readonly SqlSafetyValidator _validator;
    private readonly SqlStatisticsParser _statisticsParser;
    private readonly SqlPlanAnalyzer _planAnalyzer;
    private readonly ILogger<SqlDiagnosticsService> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="SqlDiagnosticsService"/> class.
    /// </summary>
    /// <param name="configuration">The SQL diagnostics configuration.</param>
    /// <param name="validator">The SQL safety validator.</param>
    /// <param name="statisticsParser">The SQL statistics parser.</param>
    /// <param name="planAnalyzer">The SQL execution-plan analyzer.</param>
    /// <param name="logger">The structured logger.</param>
    public SqlDiagnosticsService(
        SqlDiagnosticsConfiguration configuration,
        SqlSafetyValidator validator,
        SqlStatisticsParser statisticsParser,
        SqlPlanAnalyzer planAnalyzer,
        ILogger<SqlDiagnosticsService> logger)
    {
        _configuration = configuration;
        _validator = validator;
        _statisticsParser = statisticsParser;
        _planAnalyzer = planAnalyzer;
        _logger = logger;
    }

    /// <summary>
    /// Tests connectivity and reads safe server/database metadata.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>The connectivity test result.</returns>
    public async Task<SqlConnectionTestResult> TestConnectionAsync(
        string connectionName,
        CancellationToken cancellationToken)
    {
        var profile = _configuration.GetRequiredConnection(connectionName);
        var result = new SqlConnectionTestResult
        {
            ConnectionName = profile.Name
        };

        try
        {
            await using var connection = new SqlConnection(profile.ConnectionString);
            await connection.OpenAsync(cancellationToken).ConfigureAwait(false);

            const string metadataSql = """
                SELECT
                    CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)) AS ServerName,
                    DB_NAME() AS DatabaseName,
                    CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS ProductVersion,
                    CAST(DATABASEPROPERTYEX(DB_NAME(), 'CompatibilityLevel') AS nvarchar(32)) AS CompatibilityLevel;
                """;

            var metadata = await ExecuteInternalRowsAsync(
                connection,
                metadataSql,
                _configuration.GetCommandTimeoutSeconds(profile),
                _configuration.GetMaxTextLength(),
                1,
                cancellationToken).ConfigureAwait(false);

            var row = metadata.FirstOrDefault();
            result.ServerName = row?.GetValueOrDefault("ServerName")?.ToString();
            result.DatabaseName = row?.GetValueOrDefault("DatabaseName")?.ToString();
            result.ProductVersion = row?.GetValueOrDefault("ProductVersion")?.ToString();
            result.CompatibilityLevel = row?.GetValueOrDefault("CompatibilityLevel")?.ToString();
            result.QueryStoreState = await ReadScalarStringOrNullAsync(
                connection,
                "SELECT TOP (1) actual_state_desc FROM sys.database_query_store_options;",
                _configuration.GetCommandTimeoutSeconds(profile),
                cancellationToken).ConfigureAwait(false);
            result.Permissions = await ReadPermissionProbeAsync(
                connection,
                _configuration.GetCommandTimeoutSeconds(profile),
                cancellationToken).ConfigureAwait(false);
            result.Succeeded = true;
        }
        catch (Exception ex) when (ex is SqlException or InvalidOperationException or TimeoutException)
        {
            _logger.LogWarning(ex, "SQL diagnostics connection test failed for {ConnectionName}", profile.Name);
            result.Succeeded = false;
            result.ErrorMessage = SanitizeException(ex);
        }

        return result;
    }

    /// <summary>
    /// Runs a user-supplied read-only SQL statement with bounded output.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="sql">The user-supplied read-only SQL statement.</param>
    /// <param name="maxRows">Optional row limit override.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>The bounded query result.</returns>
    public async Task<SqlReadOnlyQueryResult> RunReadOnlyAsync(
        string connectionName,
        string sql,
        int? maxRows,
        CancellationToken cancellationToken)
    {
        var profile = _configuration.GetRequiredConnection(connectionName);
        var result = new SqlReadOnlyQueryResult
        {
            ConnectionName = profile.Name,
            MaxRows = Math.Clamp(maxRows ?? _configuration.GetMaxRows(profile), 1, 5000)
        };

        var validation = _validator.ValidateReadOnlyStatement(sql);
        if (!validation.IsValid)
        {
            result.Succeeded = false;
            result.ErrorMessage = validation.ErrorMessage;
            return result;
        }

        var messages = new List<string>();
        var rows = new List<IReadOnlyDictionary<string, object?>>();
        var columns = new List<string>();
        var stopwatch = Stopwatch.StartNew();

        try
        {
            await using var connection = new SqlConnection(profile.ConnectionString);
            connection.InfoMessage += (_, args) =>
            {
                foreach (SqlError error in args.Errors)
                {
                    messages.Add(error.Message);
                }
            };

            await connection.OpenAsync(cancellationToken).ConfigureAwait(false);

            await using var command = connection.CreateCommand();
            command.CommandTimeout = _configuration.GetCommandTimeoutSeconds(profile);
            command.CommandType = CommandType.Text;
            command.CommandText = $"""
                SET STATISTICS IO ON;
                SET STATISTICS TIME ON;
                {sql}
                SET STATISTICS IO OFF;
                SET STATISTICS TIME OFF;
                """;

            await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
            do
            {
                if (reader.FieldCount <= 0)
                {
                    continue;
                }

                if (columns.Count == 0)
                {
                    for (var index = 0; index < reader.FieldCount; index++)
                    {
                        columns.Add(reader.GetName(index));
                    }
                }

                while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
                {
                    result.RowsRead++;
                    if (rows.Count >= result.MaxRows)
                    {
                        result.Truncated = true;
                        continue;
                    }

                    rows.Add(ReadCurrentRow(reader, _configuration.GetMaxTextLength()));
                }
            }
            while (await reader.NextResultAsync(cancellationToken).ConfigureAwait(false));

            result.Succeeded = true;
        }
        catch (Exception ex) when (ex is SqlException or InvalidOperationException or TimeoutException)
        {
            _logger.LogWarning(ex, "Read-only SQL execution failed for {ConnectionName}", profile.Name);
            result.Succeeded = false;
            result.ErrorMessage = SanitizeException(ex);
        }
        finally
        {
            stopwatch.Stop();
        }

        result.ElapsedMilliseconds = stopwatch.ElapsedMilliseconds;
        result.Columns = columns;
        result.Rows = rows;
        result.Messages = messages;
        result.Statistics = _statisticsParser.Parse(messages);
        return result;
    }

    /// <summary>
    /// Analyzes a read-only SQL statement using estimated plan data and bounded execution.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="sql">The user-supplied read-only SQL statement.</param>
    /// <param name="maxRows">Optional row limit override.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>The statement analysis result.</returns>
    public async Task<SqlStatementAnalysisResult> AnalyzeStatementAsync(
        string connectionName,
        string sql,
        int? maxRows,
        CancellationToken cancellationToken)
    {
        var profile = _configuration.GetRequiredConnection(connectionName);
        var validation = _validator.ValidateReadOnlyStatement(sql);
        if (!validation.IsValid)
        {
            return new SqlStatementAnalysisResult
            {
                Succeeded = false,
                ConnectionName = profile.Name,
                ErrorMessage = validation.ErrorMessage
            };
        }

        var plan = profile.AllowEstimatedPlan
            ? await TryReadEstimatedPlanAsync(profile, sql, cancellationToken).ConfigureAwait(false)
            : new SqlPlanSummary
            {
                Available = false,
                UnavailableReason = "Estimated-plan capture is disabled for this connection."
            };

        var execution = await RunReadOnlyAsync(connectionName, sql, maxRows, cancellationToken).ConfigureAwait(false);
        var recommendations = BuildRecommendations(execution, plan);

        return new SqlStatementAnalysisResult
        {
            Succeeded = execution.Succeeded,
            ConnectionName = profile.Name,
            Execution = execution,
            EstimatedPlan = plan,
            Recommendations = recommendations,
            ErrorMessage = execution.ErrorMessage
        };
    }

    /// <summary>
    /// Executes trusted internal diagnostic SQL and returns bounded rows.
    /// </summary>
    /// <param name="connection">The open SQL connection.</param>
    /// <param name="sql">The internal diagnostic SQL statement.</param>
    /// <param name="commandTimeoutSeconds">The SQL command timeout.</param>
    /// <param name="maxTextLength">The maximum text length for values.</param>
    /// <param name="maxRows">The maximum rows to return.</param>
    /// <param name="cancellationToken">Cancellation token for the SQL operation.</param>
    /// <returns>Bounded diagnostic rows.</returns>
    public static async Task<IReadOnlyList<IReadOnlyDictionary<string, object?>>> ExecuteInternalRowsAsync(
        SqlConnection connection,
        string sql,
        int commandTimeoutSeconds,
        int maxTextLength,
        int maxRows,
        CancellationToken cancellationToken)
    {
        var rows = new List<IReadOnlyDictionary<string, object?>>();

        await using var command = connection.CreateCommand();
        command.CommandTimeout = commandTimeoutSeconds;
        command.CommandText = sql;

        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false) && rows.Count < maxRows)
        {
            rows.Add(ReadCurrentRow(reader, maxTextLength));
        }

        return rows;
    }

    /// <summary>
    /// Converts an exception into a safe message that does not include connection strings.
    /// </summary>
    /// <param name="exception">The exception to sanitize.</param>
    /// <returns>A safe message for MCP clients.</returns>
    public static string SanitizeException(Exception exception)
    {
        return exception switch
        {
            SqlException sqlException => $"SQL Server error {sqlException.Number}: {sqlException.Message}",
            TimeoutException timeoutException => timeoutException.Message,
            InvalidOperationException invalidOperationException => invalidOperationException.Message,
            _ => "The SQL diagnostics operation failed."
        };
    }

    private async Task<SqlPlanSummary> TryReadEstimatedPlanAsync(
        SqlConnectionOptions profile,
        string sql,
        CancellationToken cancellationToken)
    {
        try
        {
            await using var connection = new SqlConnection(profile.ConnectionString);
            await connection.OpenAsync(cancellationToken).ConfigureAwait(false);

            await using var command = connection.CreateCommand();
            command.CommandTimeout = _configuration.GetCommandTimeoutSeconds(profile);
            command.CommandText = $"""
                SET SHOWPLAN_XML ON;
                {sql}
                SET SHOWPLAN_XML OFF;
                """;

            var planXml = await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false) as string;
            return _planAnalyzer.Summarize(planXml);
        }
        catch (Exception ex) when (ex is SqlException or InvalidOperationException or TimeoutException)
        {
            _logger.LogInformation(ex, "Estimated plan was unavailable for {ConnectionName}", profile.Name);
            return new SqlPlanSummary
            {
                Available = false,
                UnavailableReason = SanitizeException(ex)
            };
        }
    }

    private static async Task<string?> ReadScalarStringOrNullAsync(
        SqlConnection connection,
        string sql,
        int commandTimeoutSeconds,
        CancellationToken cancellationToken)
    {
        try
        {
            await using var command = connection.CreateCommand();
            command.CommandTimeout = commandTimeoutSeconds;
            command.CommandText = sql;
            var value = await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false);
            return value?.ToString();
        }
        catch (SqlException)
        {
            return null;
        }
    }

    private static async Task<IReadOnlyDictionary<string, int?>> ReadPermissionProbeAsync(
        SqlConnection connection,
        int commandTimeoutSeconds,
        CancellationToken cancellationToken)
    {
        const string sql = """
            SELECT
                HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE') AS ViewDatabaseState,
                HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE PERFORMANCE STATE') AS ViewDatabasePerformanceState,
                HAS_PERMS_BY_NAME(NULL, 'SERVER', 'VIEW SERVER STATE') AS ViewServerState,
                HAS_PERMS_BY_NAME(NULL, 'SERVER', 'VIEW SERVER PERFORMANCE STATE') AS ViewServerPerformanceState;
            """;

        IReadOnlyList<IReadOnlyDictionary<string, object?>> rows;
        try
        {
            rows = await ExecuteInternalRowsAsync(connection, sql, commandTimeoutSeconds, 100, 1, cancellationToken).ConfigureAwait(false);
        }
        catch (SqlException)
        {
            return new Dictionary<string, int?>();
        }

        var first = rows.FirstOrDefault();
        if (first is null)
        {
            return new Dictionary<string, int?>();
        }

        return first.ToDictionary(
            pair => pair.Key,
            pair => pair.Value is null ? null : (int?)Convert.ToInt32(pair.Value),
            StringComparer.OrdinalIgnoreCase);
    }

    private static IReadOnlyDictionary<string, object?> ReadCurrentRow(IDataRecord reader, int maxTextLength)
    {
        var row = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);

        for (var index = 0; index < reader.FieldCount; index++)
        {
            var name = reader.GetName(index);
            var value = reader.GetValue(index);
            row[name] = ConvertValue(value, maxTextLength);
        }

        return row;
    }

    private static object? ConvertValue(object value, int maxTextLength)
    {
        return value switch
        {
            DBNull => null,
            null => null,
            byte[] bytes => $"<binary {bytes.Length} bytes>",
            DateTime dateTime => dateTime.ToString("O"),
            DateTimeOffset dateTimeOffset => dateTimeOffset.ToString("O"),
            string text when text.Length > maxTextLength => text[..maxTextLength] + "...<truncated>",
            _ => value
        };
    }

    private static IReadOnlyList<string> BuildRecommendations(
        SqlReadOnlyQueryResult execution,
        SqlPlanSummary plan)
    {
        var recommendations = new List<string>();

        if (!execution.Succeeded)
        {
            recommendations.Add("Fix the SQL error before performance tuning.");
            return recommendations;
        }

        if (execution.Statistics?.TotalLogicalReads > 100000)
        {
            recommendations.Add("High logical reads detected. Review filters, joins, and candidate indexes. Any index script must be manual_review_required.");
        }

        if (execution.ElapsedMilliseconds > 5000)
        {
            recommendations.Add("Elapsed time is above 5 seconds. Compare Query Store history and check whether the execution plan changed recently.");
        }

        if (plan.HasMissingIndexSuggestion)
        {
            recommendations.Add("The estimated plan contains missing-index information. Generate only a reviewed candidate CREATE INDEX script; do not execute it from MCP.");
        }

        if (plan.PhysicalOperators.Any(pair =>
                pair.Key.Contains("Table Scan", StringComparison.OrdinalIgnoreCase) ||
                pair.Key.Contains("Index Scan", StringComparison.OrdinalIgnoreCase)))
        {
            recommendations.Add("The plan includes scan operators. Confirm row estimates, predicates, and existing index coverage before changing indexes.");
        }

        if (recommendations.Count == 0)
        {
            recommendations.Add("No obvious issue was detected from bounded execution and estimated-plan summary. Review Query Store workload for historical context.");
        }

        return recommendations;
    }
}

