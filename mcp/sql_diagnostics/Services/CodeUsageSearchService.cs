using DevLoop.SqlDiagnosticsMcp.Configuration;
using DevLoop.SqlDiagnosticsMcp.Models;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Searches the local target workspace for SQL object usage.
/// </summary>
public sealed class CodeUsageSearchService
{
    private static readonly string[] AllowedExtensions =
    [
        ".cs",
        ".ts",
        ".sql",
        ".json",
        ".md"
    ];

    private static readonly string[] ExcludedDirectories =
    [
        ".git",
        ".vs",
        "bin",
        "obj",
        "node_modules",
        "dist",
        ".angular",
        ".build-tmp",
        "artifacts"
    ];

    private readonly SqlDiagnosticsConfiguration _configuration;

    /// <summary>
    /// Initializes a new instance of the <see cref="CodeUsageSearchService"/> class.
    /// </summary>
    /// <param name="configuration">The SQL diagnostics configuration.</param>
    public CodeUsageSearchService(SqlDiagnosticsConfiguration configuration)
    {
        _configuration = configuration;
    }

    /// <summary>
    /// Searches local source files for a SQL object, entity, or query term.
    /// </summary>
    /// <param name="searchTerm">The table, entity, or SQL term to search for.</param>
    /// <param name="maxMatches">The maximum number of matches to return.</param>
    /// <returns>The bounded code usage result.</returns>
    public CodeUsageResult FindUsage(string searchTerm, int maxMatches)
    {
        var result = new CodeUsageResult
        {
            SearchTerm = searchTerm,
            RepositoryRoot = _configuration.GetRepositoryRoot()
        };

        if (string.IsNullOrWhiteSpace(searchTerm))
        {
            result.Succeeded = false;
            result.ErrorMessage = "Search term is required.";
            return result;
        }

        if (string.IsNullOrWhiteSpace(result.RepositoryRoot) || !Directory.Exists(result.RepositoryRoot))
        {
            result.Succeeded = false;
            result.ErrorMessage = "Repository root could not be found. Set SqlDiagnostics:RepositoryRoot in appsettings.local.json.";
            return result;
        }

        var matches = new List<CodeUsageMatch>();
        var boundedMaxMatches = Math.Clamp(maxMatches, 1, 500);

        foreach (var filePath in EnumerateSearchFiles(result.RepositoryRoot))
        {
            var lineNumber = 0;
            foreach (var line in File.ReadLines(filePath))
            {
                lineNumber++;
                if (!line.Contains(searchTerm, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                matches.Add(new CodeUsageMatch(
                    Path.GetRelativePath(result.RepositoryRoot, filePath),
                    lineNumber,
                    line.Trim()));

                if (matches.Count >= boundedMaxMatches)
                {
                    result.Succeeded = true;
                    result.Matches = matches;
                    return result;
                }
            }
        }

        result.Succeeded = true;
        result.Matches = matches;
        return result;
    }

    private static IEnumerable<string> EnumerateSearchFiles(string repositoryRoot)
    {
        var pending = new Stack<string>();
        pending.Push(repositoryRoot);

        while (pending.Count > 0)
        {
            var directory = pending.Pop();

            foreach (var childDirectory in Directory.EnumerateDirectories(directory))
            {
                var name = Path.GetFileName(childDirectory);
                if (ExcludedDirectories.Contains(name, StringComparer.OrdinalIgnoreCase))
                {
                    continue;
                }

                pending.Push(childDirectory);
            }

            foreach (var file in Directory.EnumerateFiles(directory))
            {
                if (AllowedExtensions.Contains(Path.GetExtension(file), StringComparer.OrdinalIgnoreCase))
                {
                    yield return file;
                }
            }
        }
    }
}

