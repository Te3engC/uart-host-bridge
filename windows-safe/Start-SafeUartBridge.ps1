#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$LinuxHost = 'user@<server-ip>',
    [int]$RemotePort = 15039,
    [int]$LocalPort = 15039
)

$ErrorActionPreference = 'Stop'
$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
if (-not $ssh) { throw 'Windows OpenSSH client (ssh.exe) is not installed.' }

function Send-Reply([System.IO.Stream]$Stream, [object]$Object) {
    $data = [Text.Encoding]::UTF8.GetBytes(($Object | ConvertTo-Json -Compress) + "`n")
    $Stream.Write($data, 0, $data.Length); $Stream.Flush()
}
function Get-PortName([string]$Port) {
    if ($Port -notmatch '^COM[0-9]+$') { throw "Invalid COM port: $Port" }
    $match = [System.IO.Ports.SerialPort]::GetPortNames() | Where-Object { $_ -ieq $Port }
    if (-not $match) { throw "Windows does not currently expose $Port" }
    return $match
}
function Get-PortList {
    $entries = @{}
    $pnpEntries = @{}
    try {
        Get-CimInstance Win32_SerialPort | ForEach-Object {
            $entries[$_.DeviceID.ToUpperInvariant()] = $_
        }
    } catch {}
    try {
        Get-CimInstance Win32_PnPEntity | ForEach-Object {
            if ($_.Name -match '\((COM[0-9]+)\)') { $pnpEntries[$matches[1].ToUpperInvariant()] = $_ }
        }
    } catch {}
    @([System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object | ForEach-Object {
        $key = $_.ToUpperInvariant(); $entry = $entries[$key]; $pnp = $pnpEntries[$key]
        [PSCustomObject]@{
            port = $_
            name = if ($entry) { $entry.Name } elseif ($pnp) { $pnp.Name } else { $null }
            pnp_id = if ($entry) { $entry.PNPDeviceID } elseif ($pnp) { $pnp.DeviceID } else { $null }
            description = if ($entry) { $entry.Description } elseif ($pnp) { $pnp.Description } else { $null }
            manufacturer = if ($entry) { $entry.Manufacturer } elseif ($pnp) { $pnp.Manufacturer } else { $null }
        }
    })
}
function New-Serial([object]$Request) {
    $port = Get-PortName $Request.port
    $parity = [System.IO.Ports.Parity]::Parse([System.IO.Ports.Parity], (Get-Culture).TextInfo.ToTitleCase($Request.parity))
    $stop = if ([int]$Request.stopbits -eq 2) { [System.IO.Ports.StopBits]::Two } else { [System.IO.Ports.StopBits]::One }
    $serial = [System.IO.Ports.SerialPort]::new($port, [int]$Request.baudrate, $parity, [int]$Request.bytesize, $stop)
    $serial.Handshake = switch ($Request.flow) { 'rtscts' { [System.IO.Ports.Handshake]::RequestToSend }; 'xonxoff' { [System.IO.Ports.Handshake]::XOnXOff }; default { [System.IO.Ports.Handshake]::None } }
    $serial.ReadTimeout = 1000; $serial.WriteTimeout = 1000; $serial.Open(); return $serial
}

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $LocalPort)
$listener.Start()
Write-Host "Windows UART bridge listening locally: 127.0.0.1:$LocalPort"
Write-Host "Opening encrypted SSH reverse tunnel: Ubuntu 127.0.0.1:$RemotePort -> Windows 127.0.0.1:$LocalPort"
Write-Host 'Press Ctrl+C or close this window to stop. No firewall rule or scheduled task is created.'

$job = Start-Job -ScriptBlock {
    param($Exe, $Target, $Remote, $Local)
    while ($true) {
        & $Exe -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -N -R "127.0.0.1:${Remote}:127.0.0.1:${Local}" $Target
        Write-Output "SSH reverse tunnel ended (exit $LASTEXITCODE); retrying in 3 seconds."
        Start-Sleep -Seconds 3
    }
} -ArgumentList $ssh.Source, $LinuxHost, $RemotePort, $LocalPort
Start-Sleep -Seconds 1
if ($job.State -eq 'Failed') {
    $detail = (Receive-Job $job | Out-String).Trim()
    throw "SSH reverse tunnel failed to start. $detail"
}

try {
    while ($true) {
        $client = $listener.AcceptTcpClient(); $stream = $client.GetStream(); $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8, $false, 65536, $true)
        try {
            $line = $reader.ReadLine(); if (-not $line) { continue }; $request = $line | ConvertFrom-Json
            Write-Host "UART request: $($request.op)"
            switch ($request.op) {
                'list' { Send-Reply $stream @{ ok = $true; ports = @(Get-PortList) } }
                'info' { $port = Get-PortName $request.port; $entry = (Get-PortList | Where-Object { $_.port -ieq $port }); Send-Reply $stream @{ ok = $true; port = $port; name = $entry.name; pnp_id = $entry.pnp_id; description = $entry.description; manufacturer = $entry.manufacturer } }
                'probe' { $serial = New-Serial $request; $serial.Close(); Send-Reply $stream @{ ok = $true; port = $request.port; settings = "opened; $($request.baudrate) $($request.bytesize)$($request.parity[0].ToString().ToUpper())$($request.stopbits) flow=$($request.flow)" } }
                'open' {
                    $serial = New-Serial $request; Send-Reply $stream @{ ok = $true; port = $request.port }
                    $toSerial = $stream.CopyToAsync($serial.BaseStream); $toNetwork = $serial.BaseStream.CopyToAsync($stream)
                    [Threading.Tasks.Task]::WaitAny(@($toSerial, $toNetwork)) | Out-Null; $serial.Close()
                }
                default { throw "Unsupported operation: $($request.op)" }
            }
        } catch {
            Write-Host "UART request failed: $($_.Exception.Message)" -ForegroundColor Red
            try { Send-Reply $stream @{ ok = $false; error = $_.Exception.Message } } catch { Write-Host "UART error reply failed: $($_.Exception.Message)" -ForegroundColor Red }
        }
        finally { $reader.Dispose(); $stream.Dispose(); $client.Close() }
    }
} finally { $listener.Stop(); Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue }
