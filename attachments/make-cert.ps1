param(
  [string]$Names = "localhost"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$list = $Names.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$cert = New-SelfSignedCertificate -DnsName $list -CertStoreLocation "Cert:\CurrentUser\My" -KeyAlgorithm RSA -KeyLength 2048 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(2)
$pfx = Join-Path $here "shelf.pfx"
$pwd = ConvertTo-SecureString "shelf" -AsPlainText -Force
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $pwd | Out-Null
$loaded = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfx, "shelf", "Exportable,PersistKeySet")
$certPem = "-----BEGIN CERTIFICATE-----`r`n" + [Convert]::ToBase64String($loaded.RawData, "InsertLineBreaks") + "`r`n-----END CERTIFICATE-----`r`n"
$rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($loaded)
$keyPem = "-----BEGIN PRIVATE KEY-----`r`n" + [Convert]::ToBase64String($rsa.ExportPkcs8PrivateKey(), "InsertLineBreaks") + "`r`n-----END PRIVATE KEY-----`r`n"
Set-Content -Path (Join-Path $here "cert.pem") -Value $certPem -Encoding ascii
Set-Content -Path (Join-Path $here "key.pem") -Value $keyPem -Encoding ascii
Write-Host "Wrote cert.pem and key.pem for: $($list -join ', ')"
