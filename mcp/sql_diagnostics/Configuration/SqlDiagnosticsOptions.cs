namespace DevLoop.SqlDiagnosticsMcp.Configuration;

/// <summary>
/// Represents the local configuration used by the SQL diagnostics MCP server.
/// </summary>
public sealed class SqlDiagnosticsOptions
{
    /// <summary>
    /// Gets the configuration section name that contains SQL diagnostics settings.
    /// </summary>
    public const string SectionName = "SqlDiagnostics";

    /// <summary>
    /// Gets or sets the repository root used by the code-usage search tool.
    /// </summary>
    public string? RepositoryRoot { get; set; }

    /// <summary>
    /// Gets or sets the default SQL command timeout in seconds.
    /// </summary>
    public int DefaultCommandTimeoutSeconds { get; set; } = 120;

    /// <summary>
    /// Gets or sets the default maximum number of rows returned to Codex.
    /// </summary>
    public int DefaultMaxRows { get; set; } = 500;

    /// <summary>
    /// Gets or sets the default maximum text length returned for a single value.
    /// </summary>
    public int DefaultMaxTextLength { get; set; } = 4000;

    /// <summary>
    /// Gets or sets the named SQL Server connections available to the MCP.
    /// </summary>
    public List<SqlConnectionOptions> Connections { get; set; } = [];
}

/// <summary>
/// Represents a single named SQL Server connection profile.
/// </summary>
public sealed class SqlConnectionOptions
{
    /// <summary>
    /// Gets or sets the unique name used by MCP tool calls.
    /// </summary>
    public string Name { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets an operator-facing description for this connection.
    /// </summary>
    public string? Description { get; set; }

    /// <summary>
    /// Gets or sets the SQL Server connection string.
    /// </summary>
    public string ConnectionString { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the optional command timeout override in seconds.
    /// </summary>
    public int? CommandTimeoutSeconds { get; set; }

    /// <summary>
    /// Gets or sets the optional maximum row override.
    /// </summary>
    public int? MaxRows { get; set; }

    /// <summary>
    /// Gets or sets whether Query Store diagnostics should be attempted.
    /// </summary>
    public bool EnableQueryStoreDiagnostics { get; set; } = true;

    /// <summary>
    /// Gets or sets whether DMV diagnostics should be attempted.
    /// </summary>
    public bool EnableDmvDiagnostics { get; set; } = true;

    /// <summary>
    /// Gets or sets whether estimated execution plans should be attempted.
    /// </summary>
    public bool AllowEstimatedPlan { get; set; } = true;
}

