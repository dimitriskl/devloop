using DevLoop.SqlDiagnosticsMcp.Models;
using Microsoft.Extensions.Options;

namespace DevLoop.SqlDiagnosticsMcp.Configuration;

/// <summary>
/// Provides validated access to named SQL diagnostics connection profiles.
/// </summary>
public sealed class SqlDiagnosticsConfiguration
{
    private readonly SqlDiagnosticsOptions _options;
    private readonly Dictionary<string, SqlConnectionOptions> _connections;

    /// <summary>
    /// Initializes a new instance of the <see cref="SqlDiagnosticsConfiguration"/> class.
    /// </summary>
    /// <param name="options">The configured SQL diagnostics options.</param>
    public SqlDiagnosticsConfiguration(IOptions<SqlDiagnosticsOptions> options)
    {
        _options = options.Value;
        _connections = _options.Connections
            .Where(connection => !string.IsNullOrWhiteSpace(connection.Name))
            .GroupBy(connection => connection.Name, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Gets configured connection summaries without exposing connection strings.
    /// </summary>
    /// <returns>The configured connection summaries.</returns>
    public IReadOnlyList<SqlConnectionSummary> ListConnections()
    {
        return _connections.Values
            .OrderBy(connection => connection.Name, StringComparer.OrdinalIgnoreCase)
            .Select(connection => new SqlConnectionSummary(
                connection.Name,
                connection.Description,
                GetCommandTimeoutSeconds(connection),
                GetMaxRows(connection),
                connection.EnableQueryStoreDiagnostics,
                connection.EnableDmvDiagnostics,
                connection.AllowEstimatedPlan))
            .ToList();
    }

    /// <summary>
    /// Gets a required connection by name.
    /// </summary>
    /// <param name="connectionName">The configured connection name.</param>
    /// <returns>The configured connection profile.</returns>
    /// <exception cref="ArgumentException">Thrown when the connection is missing or incomplete.</exception>
    public SqlConnectionOptions GetRequiredConnection(string connectionName)
    {
        if (string.IsNullOrWhiteSpace(connectionName))
        {
            throw new ArgumentException("Connection name is required.", nameof(connectionName));
        }

        if (!_connections.TryGetValue(connectionName, out var connection))
        {
            throw new ArgumentException($"SQL diagnostics connection '{connectionName}' is not configured.", nameof(connectionName));
        }

        if (string.IsNullOrWhiteSpace(connection.ConnectionString))
        {
            throw new ArgumentException($"SQL diagnostics connection '{connectionName}' has no connection string.", nameof(connectionName));
        }

        return connection;
    }

    /// <summary>
    /// Gets the effective command timeout for a connection.
    /// </summary>
    /// <param name="connection">The configured connection profile.</param>
    /// <returns>The command timeout in seconds.</returns>
    public int GetCommandTimeoutSeconds(SqlConnectionOptions connection)
    {
        return Math.Clamp(
            connection.CommandTimeoutSeconds ?? _options.DefaultCommandTimeoutSeconds,
            5,
            1800);
    }

    /// <summary>
    /// Gets the effective maximum row count for a connection.
    /// </summary>
    /// <param name="connection">The configured connection profile.</param>
    /// <returns>The maximum number of rows returned to Codex.</returns>
    public int GetMaxRows(SqlConnectionOptions connection)
    {
        return Math.Clamp(connection.MaxRows ?? _options.DefaultMaxRows, 1, 5000);
    }

    /// <summary>
    /// Gets the effective maximum text length for one returned value.
    /// </summary>
    /// <returns>The maximum text length.</returns>
    public int GetMaxTextLength()
    {
        return Math.Clamp(_options.DefaultMaxTextLength, 200, 20000);
    }

    /// <summary>
    /// Gets the configured repository root or discovers it from the current process.
    /// </summary>
    /// <returns>The repository root path, when available.</returns>
    public string? GetRepositoryRoot()
    {
        if (!string.IsNullOrWhiteSpace(_options.RepositoryRoot) && Directory.Exists(_options.RepositoryRoot))
        {
            return _options.RepositoryRoot;
        }

        return FindRepositoryRoot(Directory.GetCurrentDirectory())
            ?? FindRepositoryRoot(AppContext.BaseDirectory);
    }

    private static string? FindRepositoryRoot(string startPath)
    {
        var directory = new DirectoryInfo(startPath);

        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "<solution>.sln")))
            {
                return directory.FullName;
            }

            directory = directory.Parent;
        }

        return null;
    }
}

