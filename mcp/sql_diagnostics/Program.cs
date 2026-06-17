using DevLoop.SqlDiagnosticsMcp.Configuration;
using DevLoop.SqlDiagnosticsMcp.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

var builder = Host.CreateApplicationBuilder(args);

builder.Logging.ClearProviders();
builder.Logging.AddConsole(options =>
{
    // MCP uses stdout for JSON-RPC, so every log line must go to stderr.
    options.LogToStandardErrorThreshold = LogLevel.Trace;
});

AddLocalConfiguration(builder.Configuration, args);

builder.Services.Configure<SqlDiagnosticsOptions>(
    builder.Configuration.GetSection(SqlDiagnosticsOptions.SectionName));
builder.Services.AddSingleton<SqlDiagnosticsConfiguration>();
builder.Services.AddSingleton<SqlSafetyValidator>();
builder.Services.AddSingleton<SqlStatisticsParser>();
builder.Services.AddSingleton<SqlPlanAnalyzer>();
builder.Services.AddSingleton<SqlDiagnosticsService>();
builder.Services.AddSingleton<SqlSchemaReader>();
builder.Services.AddSingleton<SqlWorkloadReader>();
builder.Services.AddSingleton<CodeUsageSearchService>();

builder.Services
    .AddMcpServer()
    .WithStdioServerTransport()
    .WithToolsFromAssembly();

await builder.Build().RunAsync();

static void AddLocalConfiguration(ConfigurationManager configuration, string[] args)
{
    var explicitConfigPath = GetOptionValue(args, "--config")
        ?? Environment.GetEnvironmentVariable("DEVLOOP_SQL_MCP_CONFIG");

    if (!string.IsNullOrWhiteSpace(explicitConfigPath))
    {
        configuration.AddJsonFile(explicitConfigPath, optional: false, reloadOnChange: true);
        return;
    }

    foreach (var candidate in GetDefaultConfigCandidates())
    {
        configuration.AddJsonFile(candidate, optional: true, reloadOnChange: true);
    }
}

static string? GetOptionValue(IReadOnlyList<string> args, string optionName)
{
    for (var index = 0; index < args.Count - 1; index++)
    {
        if (string.Equals(args[index], optionName, StringComparison.OrdinalIgnoreCase))
        {
            return args[index + 1];
        }
    }

    return null;
}

static IEnumerable<string> GetDefaultConfigCandidates()
{
    yield return Path.Combine(AppContext.BaseDirectory, "appsettings.local.json");
    yield return Path.Combine(Directory.GetCurrentDirectory(), "appsettings.local.json");
    yield return Path.Combine(
        Directory.GetCurrentDirectory(),
        "tools",
        "sql_diagnostics",
        "appsettings.local.json");
}


