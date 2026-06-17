using System.Text.RegularExpressions;
using DevLoop.SqlDiagnosticsMcp.Models;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Parses SQL Server STATISTICS IO/TIME messages into structured counters.
/// </summary>
public sealed partial class SqlStatisticsParser
{
    /// <summary>
    /// Parses SQL Server informational messages.
    /// </summary>
    /// <param name="messages">Messages captured from the SQL connection.</param>
    /// <returns>Aggregated statistics.</returns>
    public SqlStatementStatistics Parse(IEnumerable<string> messages)
    {
        var tableReads = new List<SqlTableReadStatistics>();
        var cpuMilliseconds = 0;
        var elapsedMilliseconds = 0;

        foreach (var message in messages)
        {
            var tableMatch = TableReadsRegex().Match(message);
            if (tableMatch.Success)
            {
                tableReads.Add(new SqlTableReadStatistics(
                    tableMatch.Groups["table"].Value,
                    ParseInt(tableMatch.Groups["scan"].Value),
                    ParseInt(tableMatch.Groups["logical"].Value),
                    ParseInt(tableMatch.Groups["physical"].Value),
                    ParseInt(tableMatch.Groups["readahead"].Value)));
            }

            var timeMatch = ExecutionTimeRegex().Match(message);
            if (timeMatch.Success)
            {
                cpuMilliseconds += ParseInt(timeMatch.Groups["cpu"].Value);
                elapsedMilliseconds += ParseInt(timeMatch.Groups["elapsed"].Value);
            }
        }

        return new SqlStatementStatistics(
            tableReads.Sum(read => read.LogicalReads),
            tableReads.Sum(read => read.PhysicalReads),
            tableReads.Sum(read => read.ReadAheadReads),
            cpuMilliseconds,
            elapsedMilliseconds,
            tableReads);
    }

    private static int ParseInt(string value)
    {
        return int.TryParse(value, out var result) ? result : 0;
    }

    [GeneratedRegex(@"Table '(?<table>[^']+)'\.\s+Scan count (?<scan>\d+), logical reads (?<logical>\d+), physical reads (?<physical>\d+), read-ahead reads (?<readahead>\d+)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex TableReadsRegex();

    [GeneratedRegex(@"CPU time = (?<cpu>\d+) ms,\s+elapsed time = (?<elapsed>\d+) ms", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex ExecutionTimeRegex();
}

