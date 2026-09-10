param([ValidateSet('PublicQueryInfo','PublicQueryLayout','FetchOdsTableInfo','SimpleScroller','VoucherProfileInfo','VoucherStateInfo','VoucherStates','VoucherProfileRelations')][string]$Operation)
$ErrorActionPreference = 'Stop'
try {
    $cfg = Get-Content -LiteralPath 'E:\LocalCode\eConnectorV2\tools\eConnectorV2.SqlDiagnosticsMcp\appsettings.local.json' -Raw | ConvertFrom-Json
    $entry = @($cfg.SqlDiagnostics.Connections | Where-Object Name -eq 'eCv2Local')
    if ($entry.Count -ne 1) { throw 'Expected exactly one eCv2Local connection' }
    $builder = [System.Data.SqlClient.SqlConnectionStringBuilder]::new([string]$entry[0].ConnectionString)
    $builder['Connect Timeout'] = 10
    $conn = [System.Data.SqlClient.SqlConnection]::new($builder.ConnectionString)
    try {
        $conn.Open()
        $cmd = $conn.CreateCommand()
        $cmd.CommandTimeout = 10
        $cmd.CommandText = "SELECT BaseUrl,AuthMode,TokenHeaderName,StaticApiKey FROM dbo.econnector_entersoft_ebs_connections WHERE Id='272531a4-415f-4ecc-b388-6908c33076aa' AND CompanyId='104f2d7c-6d30-408a-a294-0b9e6d512953' AND IsDeleted=0 AND IsActive=1"
        $reader = $cmd.ExecuteReader()
        if (-not $reader.Read()) { throw 'Verified EBS connection is no longer active' }
        if ([int]$reader['AuthMode'] -ne 1) { throw 'Authentication mode changed' }
        $request = @{ BaseUrl=[string]$reader['BaseUrl']; TokenHeaderName=[string]$reader['TokenHeaderName'] }
        $encrypted = [string]$reader['StaticApiKey']
        $reader.Dispose()
        $cmd.Dispose()
    } finally { $conn.Dispose() }
    $settings = Get-Content -LiteralPath 'E:\LocalCode\eConnectorV2\src\eConnectorV2.API\appsettings.json' -Raw | ConvertFrom-Json
    $keyText = [string]$settings.LicenseSettings.EncryptionKey
    if ($keyText.Length -eq 44) { $keyBytes = [Convert]::FromBase64String($keyText) }
    elseif ($keyText.Length -eq 32) { $keyBytes = [Text.Encoding]::UTF8.GetBytes($keyText) }
    else { throw 'Configured encryption key has unsupported length' }
    $combined = [Convert]::FromBase64String($encrypted)
    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.KeySize = 256
        $aes.Mode = [Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $keyBytes
        $aes.IV = $combined[0..15]
        $decryptor = $aes.CreateDecryptor()
        try { $request.Key = [Text.Encoding]::UTF8.GetString($decryptor.TransformFinalBlock($combined,16,$combined.Length-16)) }
        finally { $decryptor.Dispose() }
    } finally { $aes.Dispose() }
    $request | ConvertTo-Json -Compress | & 'C:\Users\Dimitris\AppData\Local\Programs\Python\Python312\python.exe' (Join-Path $PSScriptRoot 'read_ebs.py') $Operation
    if ($LASTEXITCODE -ne 0) { throw 'Read-only HTTP probe failed' }
} catch {
    @{Succeeded=$false; ErrorType=$_.Exception.GetType().Name; Stage='Configuration, decryption, or read-only probe'} | ConvertTo-Json -Compress
    exit 1
}
