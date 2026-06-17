using System.Xml.Linq;
using DevLoop.SqlDiagnosticsMcp.Models;

namespace DevLoop.SqlDiagnosticsMcp.Services;

/// <summary>
/// Converts SQL Server showplan XML into a bounded summary for Codex.
/// </summary>
public sealed class SqlPlanAnalyzer
{
    private static readonly XNamespace ShowPlanNamespace = "http://schemas.microsoft.com/sqlserver/2004/07/showplan";

    /// <summary>
    /// Summarizes estimated execution-plan XML.
    /// </summary>
    /// <param name="planXml">The showplan XML returned by SQL Server.</param>
    /// <returns>A bounded plan summary.</returns>
    public SqlPlanSummary Summarize(string? planXml)
    {
        if (string.IsNullOrWhiteSpace(planXml))
        {
            return new SqlPlanSummary
            {
                Available = false,
                UnavailableReason = "SQL Server did not return estimated plan XML."
            };
        }

        try
        {
            var document = XDocument.Parse(planXml);
            var operators = document
                .Descendants(ShowPlanNamespace + "RelOp")
                .Select(element => element.Attribute("PhysicalOp")?.Value)
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .GroupBy(value => value!, StringComparer.OrdinalIgnoreCase)
                .OrderByDescending(group => group.Count())
                .ToDictionary(group => group.Key, group => group.Count(), StringComparer.OrdinalIgnoreCase);

            var warnings = document
                .Descendants(ShowPlanNamespace + "Warnings")
                .Descendants()
                .Select(element => element.Name.LocalName)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Take(20)
                .ToList();

            return new SqlPlanSummary
            {
                Available = true,
                PhysicalOperators = operators,
                HasMissingIndexSuggestion = document.Descendants(ShowPlanNamespace + "MissingIndex").Any(),
                Warnings = warnings
            };
        }
        catch (Exception ex) when (ex is System.Xml.XmlException or InvalidOperationException)
        {
            return new SqlPlanSummary
            {
                Available = false,
                UnavailableReason = "Execution plan XML could not be parsed."
            };
        }
    }
}

