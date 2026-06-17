using System.ComponentModel;
using DevLoop.SqlDiagnosticsMcp.Configuration;
using DevLoop.SqlDiagnosticsMcp.Models;
using DevLoop.SqlDiagnosticsMcp.Services;
using ModelContextProtocol.Server;

namespace DevLoop.SqlDiagnosticsMcp.Tools;

/// <summary>
/// Exposes safe SQL Server diagnostics as MCP tools for Codex.
/// </summary>
[McpServerToolType]
public static class SqlDiagnosticsTools
{
    /// <summary>
    /// Lists configured SQL diagnostics connections without exposing secrets.
    /// </summary>
    /// <param name="configuration">The SQL diagnostics configuration service.</param>
    /// <returns>The configured connection summaries.</returns>
    [McpServerTool(Name = "sql_list_connections", Title = "List SQL diagnostic connections", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Lists configured SQL Server diagnostic connections. Connection strings and passwords are never returned.")]
    public static IReadOnlyList<SqlConnectionSummary> ListConnections(SqlDiagnosticsConfiguration configuration)
    {
        return configuration.ListConnections();
    }

    /// <summary>
    /// Tests a configured SQL diagnostics connection.
    /// </summary>
    /// <param name="service">The SQL diagnostics service.</param>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="cancellationToken">Cancellation token for the MCP request.</param>
    /// <returns>The connection test result.</returns>
    [McpServerTool(Name = "sql_test_connection", Title = "Test SQL diagnostic connection", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Tests a configured SQL Server connection and reports server, database, Query Store, and permission metadata without exposing secrets.")]
    public static Task<SqlConnectionTestResult> TestConnection(
        SqlDiagnosticsService service,
        [Description("Configured SQL diagnostics connection name.")] string connectionName,
        CancellationToken cancellationToken)
    {
        return service.TestConnectionAsync(connectionName, cancellationToken);
    }

    /// <summary>
    /// Describes database schema metadata.
    /// </summary>
    /// <param name="schemaReader">The SQL schema reader.</param>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="topTables">The maximum number of table summaries to return.</param>
    /// <param name="cancellationToken">Cancellation token for the MCP request.</param>
    /// <returns>The database schema description.</returns>
    [McpServerTool(Name = "sql_describe_database", Title = "Describe SQL database", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Describes SQL Server tables, row counts, sizes, and indexes for a configured connection.")]
    public static Task<SqlDatabaseDescription> DescribeDatabase(
        SqlSchemaReader schemaReader,
        [Description("Configured SQL diagnostics connection name.")] string connectionName,
        [Description("Maximum number of largest tables to return. Defaults to 25.")] int topTables = 25,
        CancellationToken cancellationToken = default)
    {
        return schemaReader.DescribeDatabaseAsync(connectionName, topTables, cancellationToken);
    }

    /// <summary>
    /// Runs a bounded read-only SQL statement.
    /// </summary>
    /// <param name="service">The SQL diagnostics service.</param>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="sql">The read-only SQL statement.</param>
    /// <param name="maxRows">Optional maximum rows to return.</param>
    /// <param name="cancellationToken">Cancellation token for the MCP request.</param>
    /// <returns>The bounded read-only query result.</returns>
    [McpServerTool(Name = "sql_run_readonly", Title = "Run read-only SQL", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Runs one bounded read-only SELECT/CTE statement. Write, schema, EXEC, and administrative commands are blocked before execution.")]
    public static Task<SqlReadOnlyQueryResult> RunReadOnly(
        SqlDiagnosticsService service,
        [Description("Configured SQL diagnostics connection name.")] string connectionName,
        [Description("Single read-only SELECT statement or WITH CTE query.")] string sql,
        [Description("Optional maximum rows returned to Codex.")] int? maxRows = null,
        CancellationToken cancellationToken = default)
    {
        return service.RunReadOnlyAsync(connectionName, sql, maxRows, cancellationToken);
    }

    /// <summary>
    /// Analyzes a bounded read-only SQL statement.
    /// </summary>
    /// <param name="service">The SQL diagnostics service.</param>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="sql">The read-only SQL statement.</param>
    /// <param name="maxRows">Optional maximum rows to return.</param>
    /// <param name="cancellationToken">Cancellation token for the MCP request.</param>
    /// <returns>The statement analysis result.</returns>
    [McpServerTool(Name = "sql_analyze_statement", Title = "Analyze SQL statement", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Analyzes one read-only SQL statement using bounded execution, STATISTICS IO/TIME, and estimated plan data where permissions allow.")]
    public static Task<SqlStatementAnalysisResult> AnalyzeStatement(
        SqlDiagnosticsService service,
        [Description("Configured SQL diagnostics connection name.")] string connectionName,
        [Description("Single read-only SELECT statement or WITH CTE query to analyze.")] string sql,
        [Description("Optional maximum rows returned to Codex.")] int? maxRows = null,
        CancellationToken cancellationToken = default)
    {
        return service.AnalyzeStatementAsync(connectionName, sql, maxRows, cancellationToken);
    }

    /// <summary>
    /// Reads Query Store or DMV workload summaries.
    /// </summary>
    /// <param name="workloadReader">The SQL workload reader.</param>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="lookbackHours">The workload lookback window in hours.</param>
    /// <param name="topQueries">The maximum number of queries to return.</param>
    /// <param name="cancellationToken">Cancellation token for the MCP request.</param>
    /// <returns>The workload summary.</returns>
    [McpServerTool(Name = "sql_workload_summary", Title = "Summarize SQL workload", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Summarizes recent SQL workload from Query Store first, then DMVs when Query Store is unavailable.")]
    public static Task<SqlWorkloadSummary> WorkloadSummary(
        SqlWorkloadReader workloadReader,
        [Description("Configured SQL diagnostics connection name.")] string connectionName,
        [Description("Lookback window in hours. Defaults to 24.")] int lookbackHours = 24,
        [Description("Maximum number of workload queries to return. Defaults to 25.")] int topQueries = 25,
        CancellationToken cancellationToken = default)
    {
        return workloadReader.ReadWorkloadSummaryAsync(connectionName, lookbackHours, topQueries, cancellationToken);
    }

    /// <summary>
    /// Reads table health and index usage metadata.
    /// </summary>
    /// <param name="schemaReader">The SQL schema reader.</param>
    /// <param name="connectionName">The configured connection name.</param>
    /// <param name="tableName">Optional schema-qualified table name.</param>
    /// <param name="topTables">Maximum largest tables to inspect when tableName is not supplied.</param>
    /// <param name="cancellationToken">Cancellation token for the MCP request.</param>
    /// <returns>The table health result.</returns>
    [McpServerTool(Name = "sql_table_health", Title = "Read SQL table health", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Reports table size, statistics metadata, and visible index usage. Recommendations are advisory and require manual DBA review.")]
    public static Task<SqlTableHealthResult> TableHealth(
        SqlSchemaReader schemaReader,
        [Description("Configured SQL diagnostics connection name.")] string connectionName,
        [Description("Optional schema-qualified table name, for example dbo.MyTable.")] string? tableName = null,
        [Description("Maximum largest tables to inspect when tableName is omitted. Defaults to 10.")] int topTables = 10,
        CancellationToken cancellationToken = default)
    {
        return schemaReader.ReadTableHealthAsync(connectionName, tableName, topTables, cancellationToken);
    }

    /// <summary>
    /// Searches the local target repository for SQL object usage.
    /// </summary>
    /// <param name="codeSearch">The code usage search service.</param>
    /// <param name="searchTerm">The SQL table, entity, or query term.</param>
    /// <param name="maxMatches">The maximum number of matches to return.</param>
    /// <returns>The code usage search result.</returns>
    [McpServerTool(Name = "sql_find_code_usage", Title = "Find SQL code usage", ReadOnly = true, Destructive = false, Idempotent = true, OpenWorld = false)]
    [Description("Searches the local target workspace for table, entity, LINQ, DbSet, raw SQL, or CRUD usage related to a SQL finding.")]
    public static CodeUsageResult FindCodeUsage(
        CodeUsageSearchService codeSearch,
        [Description("SQL table, entity, or query term to search in the local target repository.")] string searchTerm,
        [Description("Maximum number of source matches to return. Defaults to 50.")] int maxMatches = 50)
    {
        return codeSearch.FindUsage(searchTerm, maxMatches);
    }
}

