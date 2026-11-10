# probe_serial.ps1 —— 串口探测（.NET 自带串口，不需要 pyserial）
# 用法（在你自己的 PowerShell 里）：
#   powershell -ExecutionPolicy Bypass -File probe_serial.ps1
#
# 列出串口 → 逐个试常见波特率 → 打印字节率 / HEX / ASCII → 存 bin

$ErrorActionPreference = 'Continue'
$ws = 'D:\workspace\脑机接口相关'
$outDir = Join-Path $ws '数据\captures'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$BAUDS = @(115200, 460800, 921600, 230400, 57600, 9600, 1000000)
$READ_SECONDS = 1.5

function Get-HexString {
    param([byte[]]$Bytes, [int]$Max = 96)
    $n = $Bytes.Length
    if ($n -gt $Max) { $n = $Max }
    if ($n -le 0) { return '' }
    $part = New-Object byte[] $n
    [Array]::Copy($Bytes, $part, $n)
    return ([System.BitConverter]::ToString($part) -replace '-', ' ')
}

function Get-AsciiString {
    param([byte[]]$Bytes, [int]$Max = 200)
    $n = $Bytes.Length
    if ($n -gt $Max) { $n = $Max }
    if ($n -le 0) { return '' }
    $sb = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $n; $i++) {
        $b = $Bytes[$i]
        if ($b -ge 32 -and $b -lt 127) {
            [void]$sb.Append([char]$b)
        } else {
            [void]$sb.Append('.')
        }
    }
    return $sb.ToString()
}

Write-Host '================ 串口列表 ================' -ForegroundColor Cyan
$ports = [System.IO.Ports.SerialPort]::GetPortNames()
if ($ports.Count -eq 0) {
    Write-Host '没有找到串口。' -ForegroundColor Yellow
    Write-Host '→ 检查：换一根数据线（有些线只供电）/ 装 CH340 驱动 / 设备管理器里有没有带感叹号的未知设备'
    exit 0
}
foreach ($p in $ports) { Write-Host ('  ' + $p) }

foreach ($port in $ports) {
    Write-Host ''
    Write-Host ('================ 探测 ' + $port + ' ================') -ForegroundColor Cyan
    $got = $false

    foreach ($baud in $BAUDS) {
        $sp = $null
        $ok = $true
        try {
            $sp = New-Object System.IO.Ports.SerialPort($port, $baud, 'None', 8, 'One')
            $sp.ReadTimeout = 150
            $sp.Open()
        } catch {
            Write-Host ('    [' + $baud + '] 打不开：' + $_.Exception.Message) -ForegroundColor DarkGray
            $ok = $false
        }

        if (-not $ok) {
            if ($sp) { $sp.Dispose() }
            continue
        }

        $ms = New-Object System.IO.MemoryStream
        $buf = New-Object byte[] 4096
        $t0 = Get-Date
        try {
            while (((Get-Date) - $t0).TotalSeconds -lt $READ_SECONDS -and $ms.Length -lt 200000) {
                $avail = $sp.BytesToRead
                if ($avail -gt 0) {
                    $read = $sp.Read($buf, 0, [Math]::Min($avail, $buf.Length))
                    $ms.Write($buf, 0, $read)
                } else {
                    Start-Sleep -Milliseconds 30
                }
            }
        } catch {
            # 超时属正常
        }
        try { $sp.Close() } catch { }
        $sp.Dispose()

        $arr = $ms.ToArray()
        $ms.Dispose()
        $n = $arr.Length
        $rate = [int]($n / $READ_SECONDS)
        Write-Host ('    [' + $baud + '] ' + $n + ' 字节，约 ' + $rate + ' B/s')

        if ($n -gt 0) {
            Write-Host ('      HEX: ' + (Get-HexString -Bytes $arr))
            Write-Host ('      TXT: ' + (Get-AsciiString -Bytes $arr))
            Write-Host '      参考：250Hz×2字节 ≈ 500 B/s；128Hz×2字节 ≈ 256 B/s'

            $file = Join-Path $outDir ('capture_' + $port + '_' + $baud + '.bin')
            [System.IO.File]::WriteAllBytes($file, $arr)
            Write-Host ('      原始数据已存: ' + $file) -ForegroundColor Green
            $got = $true
            break
        }
    }

    if (-not $got) {
        Write-Host '    所有波特率都没有数据。' -ForegroundColor Yellow
        Write-Host '    可能：需要先发启动命令；或设备走蓝牙而非串口'
    }
}

Write-Host ''
Write-Host '探测结束。请把输出整段贴回来。' -ForegroundColor Cyan
