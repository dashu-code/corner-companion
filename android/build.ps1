# Offline APK build (no Gradle: avoids hundreds of MB download and RAM cost)
# Needs: Android SDK build-tools (aapt2/d8/zipalign/apksigner) + javac + openssl
# ---- 工具链自动探测（改为你自己机器上的路径也可以）----
param([int]$VersionCode = 1, [string]$VersionName = "1.0")

$SDK = $env:ANDROID_SDK_ROOT
if (-not $SDK) { $SDK = $env:ANDROID_HOME }
if (-not $SDK) { $SDK = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $SDK)) { throw "找不到 Android SDK：请设 ANDROID_SDK_ROOT 环境变量" }

# 取 build-tools 里版本号最大的那个
$BT = (Get-ChildItem (Join-Path $SDK "build-tools") -Directory -ErrorAction SilentlyContinue |
       Sort-Object { [version]($_.Name -replace "[^0-9.]", "") } -Descending |
       Select-Object -First 1).FullName
if (-not $BT) { throw "找不到 build-tools：请用 sdkmanager 安装" }

# 取 platforms 里版本号最大的 android.jar
$JAR = (Get-ChildItem (Join-Path $SDK "platforms") -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "android.jar" } |
        Where-Object { Test-Path $_ } |
        Select-Object -First 1)
if (-not $JAR) { throw "找不到 android.jar：请用 sdkmanager 安装 platform" }

# openssl：先看 PATH，再看常见安装位置
$OPENSSL = (Get-Command openssl -ErrorAction SilentlyContinue).Source
if (-not $OPENSSL) {
    foreach ($c in @("C:\Program Files\Git\usr\bin\openssl.exe",
                     "C:\msys64\usr\bin\openssl.exe",
                     "C:\Program Files\OpenSSL-Win64\bin\openssl.exe")) {
        if (Test-Path $c) { $OPENSSL = $c; break }
    }
}
if (-not $OPENSSL) { throw "找不到 openssl（可用 Git for Windows 自带的）" }


$ErrorActionPreference = "Continue"


$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT
$OUT = Join-Path $ROOT "out"
if (Test-Path $OUT) { Remove-Item $OUT -Recurse -Force }
New-Item -ItemType Directory -Force -Path $OUT | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OUT "classes") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OUT "dex") | Out-Null

function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

Step "0/7 aapt2 compile (res)"
$resZip = Join-Path $OUT "res.zip"
if (Test-Path "res") {
    & (Join-Path $BT "aapt2.exe") compile --dir res -o $resZip
    if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }
} else {
    Write-Host "   (no res/ dir)"
}

Step "1/7 aapt2 link"
$linkArgs = @("link", "-o", (Join-Path $OUT "base.apk"), "-I", $JAR, "--manifest", "AndroidManifest.xml",
              "--version-code", "$VersionCode", "--version-name", "$VersionName")
if (Test-Path $resZip) { $linkArgs += $resZip }
& (Join-Path $BT "aapt2.exe") @linkArgs
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

Step "2/7 javac"
$srcs = Get-ChildItem -Recurse src -Filter *.java | ForEach-Object { $_.FullName }
& javac --release 17 -encoding UTF-8 -classpath $JAR -d (Join-Path $OUT "classes") @srcs
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

Step "3/7 d8"
$classes = Get-ChildItem -Recurse (Join-Path $OUT "classes") -Filter *.class | ForEach-Object { $_.FullName }
& (Join-Path $BT "d8.bat") --release --lib $JAR --min-api 26 --output (Join-Path $OUT "dex") @classes
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

Step "4/7 inject classes.dex"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$baseApk = Join-Path $OUT "base.apk"
$zip = [System.IO.Compression.ZipFile]::Open($baseApk, 'Update')
try {
    $existing = $zip.GetEntry("classes.dex")
    if ($existing) { $existing.Delete() }
    $entry = $zip.CreateEntry("classes.dex", [System.IO.Compression.CompressionLevel]::Optimal)
    $es = $entry.Open()
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $OUT "dex\classes.dex"))
    $es.Write($bytes, 0, $bytes.Length)
    $es.Close()
} finally { $zip.Dispose() }

Step "5/7 zipalign"
& (Join-Path $BT "zipalign.exe") -f -p 4 $baseApk (Join-Path $OUT "aligned.apk")
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

Step "6/7 sign"
# 签名密钥放在 keystore/（不在 out/ 里，构建时不会被删）。
# 为什么必须固定：Android 只允许同一签名的新版本覆盖安装，换签名就得先卸载。
$keyDir  = Join-Path $ROOT "keystore"
New-Item -ItemType Directory -Force -Path $keyDir | Out-Null
$keyPem  = Join-Path $keyDir "key.pem"
$keyPk8  = Join-Path $keyDir "key.pk8"
$certPem = Join-Path $keyDir "cert.pem"
if (-not (Test-Path $keyPk8)) {
    & $OPENSSL req -x509 -newkey rsa:2048 -keyout $keyPem -out $certPem -days 10000 -nodes -subj "/CN=Companion"
    if ($LASTEXITCODE -ne 0) { throw "openssl req failed" }
    & $OPENSSL pkcs8 -topk8 -inform PEM -outform DER -in $keyPem -out $keyPk8 -nocrypt
    if ($LASTEXITCODE -ne 0) { throw "openssl pkcs8 failed" }
}
$finalApk = Join-Path $OUT "companion.apk"
& (Join-Path $BT "apksigner.bat") sign --key $keyPk8 --cert $certPem --out $finalApk (Join-Path $OUT "aligned.apk")
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }

& (Join-Path $BT "apksigner.bat") verify --print-certs $finalApk | Select-Object -First 4

Write-Host ""
Write-Host ("DONE: " + $finalApk) -ForegroundColor Green
"size_bytes=" + (Get-Item $finalApk).Length
