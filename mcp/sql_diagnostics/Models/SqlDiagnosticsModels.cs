namespace DevLoop.SqlDiagnosticsMcp.Models;

/// <summary>
/// Represents a configured SQL connection without exposing secrets.
/// </summary>
public sealed record SqlConnectionSummary(
    string Name,
    string? Description,
    int CommandTimeoutSeconds,
    int MaxRows,
    bool EnableQueryStoreDiagnostics,
    bool EnableDmvDiagnostics,
    bool AllowEstimatedPlan);

/// <summary>
/// Represents the result of validating SQL text before execution.
/// </summary>
public sealed record SqlValidationResult(
    bool IsValid,
    string? FirstKeyword,
    string? ErrorMessage)
{
    /// <summary>
    /// Gets a successful read-only validation result.
    /// </summary>
    public static SqlValidationResult Valid(string firstKeyword) => new(true, firstKeyword, null);

    /// <summary>
    /// Gets a failed validation result.
    /// </summary>
    public static SqlValidationResult Invalid(string errorMessage) => new(false, null, errorMessage);
}

/// <summary>
/// Represents SQL Server statistics extracted from diagnostic messages.
/// </summary>
public sealed record SqlStatementStatistics(
    int TotalLogicalReads,
    int TotalPhysicalReads,
    int TotalReadAheadReads,
    int CpuMilliseconds,
    int StatisticsElapsedMilliseconds,
    IReadOnlyList<SqlTableReadStatistics> TableReads);

/// <summary>
/// Represents read statistics for one SQL Server table.
/// </summary>
public sealed record SqlTableReadStatistics(
    string TableName,
    int ScanCount,
    int LogicalReads,
    int PhysicalReads,
    int ReadAheadReads);

/// <summary>
/// Represents a bounded read-only SQL execution result.
/// </summary>
public sealed class SqlReadOnlyQueryResult
{
    /// <summary>
    /// Gets or sets whether the query succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the configured connection name used for execution.
    /// </summary>
    public string ConnectionName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the elapsed wall-clock time in milliseconds.
    /// </summary>
    public long ElapsedMilliseconds { get; set; }

    /// <summary>
    /// Gets or sets the total row count read before the configured row limit stopped collection.
    /// </summary>
    public int RowsRead { get; set; }

    /// <summary>
    /// Gets or sets the maximum rows requested for this execution.
    /// </summary>
    public int MaxRows { get; set; }

    /// <summary>
    /// Gets or sets whether rows were truncated by the configured row limit.
    /// </summary>
    public bool Truncated { get; set; }

    /// <summary>
    /// Gets or sets the first result-set column names.
    /// </summary>
    public IReadOnlyList<string> Columns { get; set; } = [];

    /// <summary>
    /// Gets or sets bounded result rows.
    /// </summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object?>> Rows { get; set; } = [];

    /// <summary>
    /// Gets or sets SQL Server informational messages captured during execution.
    /// </summary>
    public IReadOnlyList<string> Messages { get; set; } = [];

    /// <summary>
    /// Gets or sets parsed SQL Server statement statistics.
    /// </summary>
    public SqlStatementStatistics? Statistics { get; set; }

    /// <summary>
    /// Gets or sets the safe error message, when execution failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents a database connectivity and metadata check result.
/// </summary>
public sealed class SqlConnectionTestResult
{
    /// <summary>
    /// Gets or sets whether the connection check succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the configured connection name.
    /// </summary>
    public string ConnectionName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the SQL Server name reported by the server.
    /// </summary>
    public string? ServerName { get; set; }

    /// <summary>
    /// Gets or sets the current database name.
    /// </summary>
    public string? DatabaseName { get; set; }

    /// <summary>
    /// Gets or sets the SQL Server product version.
    /// </summary>
    public string? ProductVersion { get; set; }

    /// <summary>
    /// Gets or sets the database compatibility level.
    /// </summary>
    public string? CompatibilityLevel { get; set; }

    /// <summary>
    /// Gets or sets the Query Store state description when visible.
    /// </summary>
    public string? QueryStoreState { get; set; }

    /// <summary>
    /// Gets or sets permission probe values.
    /// </summary>
    public IReadOnlyDictionary<string, int?> Permissions { get; set; } = new Dictionary<string, int?>();

    /// <summary>
    /// Gets or sets the safe error message, when the connection failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents a table-level schema summary.
/// </summary>
public sealed record SqlTableSummary(
    string SchemaName,
    string TableName,
    long RowCount,
    decimal TotalMegabytes,
    int ColumnCount,
    int IndexCount);

/// <summary>
/// Represents a database schema overview.
/// </summary>
public sealed class SqlDatabaseDescription
{
    /// <summary>
    /// Gets or sets whether the schema read succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the configured connection name.
    /// </summary>
    public string ConnectionName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets table summaries ordered by size.
    /// </summary>
    public IReadOnlyList<SqlTableSummary> Tables { get; set; } = [];

    /// <summary>
    /// Gets or sets index summaries.
    /// </summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object?>> Indexes { get; set; } = [];

    /// <summary>
    /// Gets or sets notes about unavailable optional diagnostics.
    /// </summary>
    public IReadOnlyList<string> Notes { get; set; } = [];

    /// <summary>
    /// Gets or sets the safe error message, when schema inspection failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents an estimated-plan and execution analysis result.
/// </summary>
public sealed class SqlStatementAnalysisResult
{
    /// <summary>
    /// Gets or sets whether analysis succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the configured connection name.
    /// </summary>
    public string ConnectionName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the bounded execution result.
    /// </summary>
    public SqlReadOnlyQueryResult? Execution { get; set; }

    /// <summary>
    /// Gets or sets the estimated execution-plan summary.
    /// </summary>
    public SqlPlanSummary? EstimatedPlan { get; set; }

    /// <summary>
    /// Gets or sets advisory recommendations. Scripts are not executed by this tool.
    /// </summary>
    public IReadOnlyList<string> Recommendations { get; set; } = [];

    /// <summary>
    /// Gets or sets the safe error message, when analysis failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents a summarized SQL Server execution plan.
/// </summary>
public sealed class SqlPlanSummary
{
    /// <summary>
    /// Gets or sets whether an estimated plan was available.
    /// </summary>
    public bool Available { get; set; }

    /// <summary>
    /// Gets or sets the safe reason when the plan was unavailable.
    /// </summary>
    public string? UnavailableReason { get; set; }

    /// <summary>
    /// Gets or sets physical operator counts.
    /// </summary>
    public IReadOnlyDictionary<string, int> PhysicalOperators { get; set; } = new Dictionary<string, int>();

    /// <summary>
    /// Gets or sets whether missing-index information appears in the plan.
    /// </summary>
    public bool HasMissingIndexSuggestion { get; set; }

    /// <summary>
    /// Gets or sets bounded plan warnings.
    /// </summary>
    public IReadOnlyList<string> Warnings { get; set; } = [];
}

/// <summary>
/// Represents workload data from Query Store or DMVs.
/// </summary>
public sealed class SqlWorkloadSummary
{
    /// <summary>
    /// Gets or sets whether workload inspection succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the configured connection name.
    /// </summary>
    public string ConnectionName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the source used for workload data.
    /// </summary>
    public string Source { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the workload rows.
    /// </summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object?>> Queries { get; set; } = [];

    /// <summary>
    /// Gets or sets notes about fallback paths or unavailable permissions.
    /// </summary>
    public IReadOnlyList<string> Notes { get; set; } = [];

    /// <summary>
    /// Gets or sets the safe error message, when workload inspection failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents table health metadata and advisory observations.
/// </summary>
public sealed class SqlTableHealthResult
{
    /// <summary>
    /// Gets or sets whether table-health inspection succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the configured connection name.
    /// </summary>
    public string ConnectionName { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets table rows inspected by the tool.
    /// </summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object?>> Tables { get; set; } = [];

    /// <summary>
    /// Gets or sets index usage rows visible to the current SQL login.
    /// </summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object?>> IndexUsage { get; set; } = [];

    /// <summary>
    /// Gets or sets advisory notes that require manual review.
    /// </summary>
    public IReadOnlyList<string> Recommendations { get; set; } = [];

    /// <summary>
    /// Gets or sets the safe error message, when table inspection failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents local code usage matches for a SQL object or term.
/// </summary>
public sealed class CodeUsageResult
{
    /// <summary>
    /// Gets or sets whether the code search succeeded.
    /// </summary>
    public bool Succeeded { get; set; }

    /// <summary>
    /// Gets or sets the searched term.
    /// </summary>
    public string SearchTerm { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the repository root used for the search.
    /// </summary>
    public string? RepositoryRoot { get; set; }

    /// <summary>
    /// Gets or sets bounded code matches.
    /// </summary>
    public IReadOnlyList<CodeUsageMatch> Matches { get; set; } = [];

    /// <summary>
    /// Gets or sets the safe error message, when the code search failed.
    /// </summary>
    public string? ErrorMessage { get; set; }
}

/// <summary>
/// Represents one local source-code match.
/// </summary>
public sealed record CodeUsageMatch(
    string RelativePath,
    int LineNumber,
    string Preview);

