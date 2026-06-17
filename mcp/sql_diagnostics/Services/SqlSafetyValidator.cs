using System.Text;
using System.Text.RegularExpressions;
using DevLoop.SqlDiagnosticsMcp.Models;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Validates user-supplied SQL before it can be sent to SQL Server.
/// </summary>
public sealed partial class SqlSafetyValidator
{
    private static readonly string[] BlockedTokens =
    [
        "ALTER",
        "BACKUP",
        "BULK",
        "CREATE",
        "DELETE",
        "DROP",
        "EXEC",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "INTO",
        "MERGE",
        "OPENDATASOURCE",
        "OPENQUERY",
        "OPENROWSET",
        "RECONFIGURE",
        "RESTORE",
        "REVOKE",
        "TRUNCATE",
        "UPDATE",
        "WAITFOR",
        "XP_CMDSHELL"
    ];

    /// <summary>
    /// Validates that SQL text is a single read-only statement.
    /// </summary>
    /// <param name="sql">The user-supplied SQL text.</param>
    /// <returns>A validation result explaining whether the SQL is safe to attempt.</returns>
    public SqlValidationResult ValidateReadOnlyStatement(string? sql)
    {
        if (string.IsNullOrWhiteSpace(sql))
        {
            return SqlValidationResult.Invalid("SQL text is required.");
        }

        var withoutComments = StripComments(sql);
        var scrubbed = ScrubStringAndQuotedIdentifierContent(withoutComments).Trim();

        if (string.IsNullOrWhiteSpace(scrubbed))
        {
            return SqlValidationResult.Invalid("SQL text contains no executable statement.");
        }

        scrubbed = scrubbed.TrimStart('\uFEFF').TrimStart();
        scrubbed = TrimLeadingSemicolon(scrubbed);

        if (!HasSingleStatement(scrubbed))
        {
            return SqlValidationResult.Invalid("Only one read-only SQL statement is allowed.");
        }

        var firstKeyword = FirstKeywordRegex().Match(scrubbed).Value.ToUpperInvariant();
        if (firstKeyword is not ("SELECT" or "WITH"))
        {
            return SqlValidationResult.Invalid("Only SELECT statements and WITH CTE queries are allowed.");
        }

        foreach (var blockedToken in BlockedTokens)
        {
            if (Regex.IsMatch(scrubbed, $@"(?<![A-Z0-9_]){blockedToken}(?![A-Z0-9_])", RegexOptions.IgnoreCase))
            {
                return SqlValidationResult.Invalid($"Blocked SQL token detected: {blockedToken}.");
            }
        }

        return SqlValidationResult.Valid(firstKeyword);
    }

    private static string TrimLeadingSemicolon(string sql)
    {
        var result = sql;
        while (result.StartsWith(';'))
        {
            result = result[1..].TrimStart();
        }

        return result;
    }

    private static bool HasSingleStatement(string sql)
    {
        var trimmed = sql.TrimEnd();
        var lastNonSemicolon = trimmed.TrimEnd(';', ' ', '\r', '\n', '\t');
        var trailingOnly = trimmed[lastNonSemicolon.Length..];

        if (trailingOnly.All(character => character is ';' or ' ' or '\r' or '\n' or '\t'))
        {
            return !lastNonSemicolon.Contains(';');
        }

        return !trimmed.Contains(';');
    }

    private static string StripComments(string sql)
    {
        var builder = new StringBuilder(sql.Length);

        for (var index = 0; index < sql.Length; index++)
        {
            if (index + 1 < sql.Length && sql[index] == '-' && sql[index + 1] == '-')
            {
                index += 2;
                while (index < sql.Length && sql[index] is not '\r' and not '\n')
                {
                    index++;
                }

                if (index < sql.Length)
                {
                    builder.Append(sql[index]);
                }
            }
            else if (index + 1 < sql.Length && sql[index] == '/' && sql[index + 1] == '*')
            {
                index += 2;
                while (index + 1 < sql.Length && !(sql[index] == '*' && sql[index + 1] == '/'))
                {
                    builder.Append(' ');
                    index++;
                }

                if (index + 1 < sql.Length)
                {
                    index++;
                }
            }
            else
            {
                builder.Append(sql[index]);
            }
        }

        return builder.ToString();
    }

    private static string ScrubStringAndQuotedIdentifierContent(string sql)
    {
        var builder = new StringBuilder(sql.Length);

        for (var index = 0; index < sql.Length; index++)
        {
            var character = sql[index];

            if (character == '\'')
            {
                builder.Append("''");
                index++;
                while (index < sql.Length)
                {
                    if (sql[index] == '\'' && index + 1 < sql.Length && sql[index + 1] == '\'')
                    {
                        index += 2;
                        continue;
                    }

                    if (sql[index] == '\'')
                    {
                        break;
                    }

                    index++;
                }
            }
            else if (character == '[')
            {
                builder.Append("[]");
                while (index < sql.Length && sql[index] != ']')
                {
                    index++;
                }
            }
            else if (character == '"')
            {
                builder.Append("\"\"");
                index++;
                while (index < sql.Length)
                {
                    if (sql[index] == '"' && index + 1 < sql.Length && sql[index + 1] == '"')
                    {
                        index += 2;
                        continue;
                    }

                    if (sql[index] == '"')
                    {
                        break;
                    }

                    index++;
                }
            }
            else
            {
                builder.Append(character);
            }
        }

        return builder.ToString();
    }

    [GeneratedRegex(@"^[A-Z_]+", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex FirstKeywordRegex();
}

