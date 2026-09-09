#Requires -Version 5.1
<#
.SYNOPSIS
    PortfolioDB runner for Windows - the three commands that are not a one-liner.

.DESCRIPTION
    The Makefile is the runner on macOS and Linux. Its recipes are POSIX shell
    (sed, base64, gzip, /dev/urandom), so a make.exe alone will not run them,
    and this script is the Windows entry point instead.

    It deliberately implements only the three targets that carry real logic:

        init      create .env, generate the secrets that are empty, lock it down
        backup    gzipped pg_dump of the whole database
        restore   load a dump into an EMPTY database

    Every other Makefile target wraps a single `docker compose` invocation that
    is identical on all platforms, so wrapping it here would add drift surface
    and buy nothing. docs/commands.md lists each one beside the compose line it
    runs; `pdb.ps1 help` prints the common ones.

    Runs on Windows PowerShell 5.1 (what a fresh Windows box has) and on
    PowerShell 7+. Nothing here may use 7-only syntax - no ??, no ?., no
    ternary - and CI runs the test suite under both hosts to keep it that way.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'help',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

# Everything is resolved against the script's own directory, not the caller's:
# `docker compose` finds its project from the working directory, and a scheduled
# task or a shortcut can start anywhere.
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $Root '.env'
$TemplateFile = Join-Path $Root '.env.template'
$RawBase = 'https://raw.githubusercontent.com/amosgeva/PortfolioDB/main'

# -- helpers --------------------------------------------------------------

function Test-OnWindows {
    # $IsWindows does not exist in 5.1, so it must never be evaluated there.
    if ($PSVersionTable.PSVersion.Major -lt 6) { return $true }
    return [bool]$IsWindows
}

function Write-EnvFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        # AllowEmptyString as well as AllowEmptyCollection: a mandatory
        # [string[]] otherwise rejects an array that contains a blank line, and
        # .env.template is full of them.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines
    )
    # UTF-8 *without* a BOM, written through .NET rather than Out-File. Windows
    # PowerShell 5.1's `>` and Out-File default to UTF-16LE, and Compose cannot
    # parse either that or a BOM - it reports the variables as unset rather than
    # as malformed, which is a confusing way to lose an evening.
    $text = ($Lines -join "`n") + "`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $text, $utf8NoBom)
}

function Read-EnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    return [System.IO.File]::ReadAllLines($Path)
}

function Get-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )
    $pattern = '^' + [regex]::Escape($Key) + '=(.*)$'
    foreach ($line in (Read-EnvFile -Path $Path)) {
        if ($line -match $pattern) { return $Matches[1] }
    }
    return ''
}

function Set-EnvValue {
    # SupportsShouldProcess because this one really does change state -- it
    # rewrites a file holding secrets. That makes -WhatIf work through
    # $WhatIfPreference, which is worth having on the only function here that
    # can overwrite a password.
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    $lines = @(Read-EnvFile -Path $Path)
    $pattern = '^' + [regex]::Escape($Key) + '='
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    # The value is deliberately not in the message: it is a secret, and
    # -WhatIf output goes to a console and often into a log.
    if ($PSCmdlet.ShouldProcess($Path, "set $Key")) {
        Write-EnvFile -Path $Path -Lines $lines
    }
}

function Get-RandomSecret {
    param([Parameter(Mandatory = $true)][int]$Length)
    # Mirrors the Makefile: head -c 48 /dev/urandom | base64 | tr -d '/+=' .
    # RandomNumberGenerator::Create() rather than Get-Random (not cryptographic)
    # or RNGCryptoServiceProvider (obsolete in modern .NET).
    $bytes = New-Object 'byte[]' 64
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $s = [Convert]::ToBase64String($bytes) -replace '[/+=]', ''
    if ($s.Length -lt $Length) {
        throw "Could not generate a $Length-character secret (got $($s.Length))."
    }
    return $s.Substring(0, $Length)
}

function Protect-File {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-OnWindows) {
        # On NTFS this is an ACL, not a mode bit. /inheritance:r is the part
        # that matters: without dropping inherited ACEs the grant is merely
        # additive and the file stays readable by others.
        $me = "$($env:USERDOMAIN)\$($env:USERNAME)"
        & icacls $Path /inheritance:r /grant:r "$($me):(R,W)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not tighten permissions on $Path (icacls exit $LASTEXITCODE). Check them by hand."
            return
        }
        Write-Host "Restricted $Path to $me."
    }
    else {
        & chmod 600 $Path
        Write-Host "Tightened permissions on $Path to 600."
    }
}

function Invoke-Compose {
    # Arguments are passed as an array, never as one string: PowerShell 5.1's
    # native-command parser is unreliable with embedded quotes, and an array
    # reaches docker as exactly the arguments written here.
    param(
        [Parameter(Mandatory = $true)][string[]]$ComposeArgs,
        [switch]$Capture
    )
    Push-Location $Root
    try {
        $all = @('compose') + $ComposeArgs
        if ($Capture) { $out = & docker @all } else { & docker @all; $out = $null }
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose $($ComposeArgs -join ' ') failed (exit $LASTEXITCODE)."
        }
        return $out
    }
    finally { Pop-Location }
}

# -- init -----------------------------------------------------------------

function Invoke-Init {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        if (Test-Path -LiteralPath $TemplateFile) {
            Copy-Item -LiteralPath $TemplateFile -Destination $EnvFile
            Write-Host 'Created .env from .env.template.'
        }
        else {
            Write-Host 'No .env here, and no .env.template to copy from.'
            Write-Host 'Fetch one:'
            Write-Host "  curl.exe -fsSL $RawBase/.env.template -o .env"
            exit 1
        }
    }

    $pg = Get-EnvValue -Path $EnvFile -Key 'POSTGRES_PASSWORD'
    $app = Get-EnvValue -Path $EnvFile -Key 'PORTFOLIODB_PASSWORD'

    if ((-not $pg) -and (-not $app)) {
        $pw = Get-RandomSecret -Length 22
        Set-EnvValue -Path $EnvFile -Key 'POSTGRES_PASSWORD'   -Value $pw
        Set-EnvValue -Path $EnvFile -Key 'PORTFOLIODB_PASSWORD' -Value $pw
        Write-Host 'Generated a Postgres password and set both keys to it.'
    }
    elseif (-not $app) {
        Set-EnvValue -Path $EnvFile -Key 'PORTFOLIODB_PASSWORD' -Value $pg
        Write-Host 'PORTFOLIODB_PASSWORD was empty - set it to match POSTGRES_PASSWORD.'
    }
    elseif (-not $pg) {
        Set-EnvValue -Path $EnvFile -Key 'POSTGRES_PASSWORD' -Value $app
        Write-Host 'POSTGRES_PASSWORD was empty - set it to match PORTFOLIODB_PASSWORD.'
    }
    elseif ($pg -ne $app) {
        Write-Host 'WARNING: POSTGRES_PASSWORD and PORTFOLIODB_PASSWORD are different.'
        Write-Host '         Postgres will start and the app will fail to connect.'
        Write-Host '         Left both alone - make them equal by hand.'
    }
    else {
        Write-Host 'Postgres password already set - left alone.'
    }

    if (-not (Get-EnvValue -Path $EnvFile -Key 'PORTFOLIODB_MCP_TOKEN')) {
        Set-EnvValue -Path $EnvFile -Key 'PORTFOLIODB_MCP_TOKEN' -Value (Get-RandomSecret -Length 40)
        Write-Host 'Generated an MCP token.'
    }
    else {
        Write-Host 'MCP token already set - left alone.'
    }

    Protect-File -Path $EnvFile

    Write-Host ''
    Write-Host 'Next:'
    Write-Host '  1. docker compose up -d'
    Write-Host '     docker compose run --rm dashboard python app/apply_schema.py'
    Write-Host '  2. docker compose run --rm dashboard python app/demo_seed.py --yes'
    Write-Host '     (fictional data to look at - skip it for a real ledger)'
    Write-Host '  3. open http://localhost:8501'
    Write-Host ''
    Write-Host '  Optional: an LLM API key in .env enables the advisor'
    Write-Host '            (docs/llm-providers.md), and your investor one-pager is'
    Write-Host "            pasted into the dashboard's Advisor tab (docs/philosophy.md)."
}

# -- backup ---------------------------------------------------------------

function Invoke-Backup {
    param([string]$Destination)

    if (-not $Destination) { $Destination = Join-Path $Root 'backups' }
    if (-not (Test-Path -LiteralPath $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outFile = Join-Path $Destination "portfoliodb-$stamp.sql.gz"
    $tmp = "/tmp/pdb-$stamp.sql.gz"

    # Compress INSIDE the container and copy a finished file out. The POSIX
    # one-liner (pg_dump | gzip > file) cannot be reused on Windows twice over:
    # there is no host gzip, and PowerShell 5.1 decodes a native command's
    # output as text before redirection, so piping a binary dump through `>`
    # silently corrupts it - a corruption that only surfaces on restore day.
    #
    # Three checks, in the container, before anything is copied out. A
    # pipeline's exit status is its LAST command's, so a pg_dump that died
    # still handed gzip a valid, non-empty archive of nothing, and Docker
    # reported the pipeline as a success. `bash -o pipefail` makes a producer
    # failure the pipeline's failure; `gzip -t` proves the archive
    # decompresses; the completion marker is the line pg_dump writes last and a
    # dump killed mid-way never reaches. `grep -c` rather than `grep -q`: -q
    # exits on the first match and the SIGPIPE it sends gunzip would count as a
    # failure under pipefail. No double quotes anywhere in the command string:
    # Windows PowerShell 5.1 passes a native argument's embedded double quotes
    # unescaped, and bash then fails to parse the command (exit 2). `grep -c`
    # always prints exactly one word, so the substitution is safe unquoted.
    $partFile = "$outFile.part"
    try {
        Invoke-Compose -ComposeArgs @(
            'exec', '-T', 'postgres', 'bash', '-o', 'pipefail', '-c',
            "pg_dump -U portfoliouser -d portfoliodb | gzip -c > $tmp && gzip -t $tmp && test `$(gunzip -c $tmp | grep -c 'PostgreSQL database dump complete') -gt 0"
        )
        Invoke-Compose -ComposeArgs @('cp', "postgres:$tmp", $partFile)
    }
    catch {
        if (Test-Path -LiteralPath $partFile) { Remove-Item -LiteralPath $partFile -Force }
        throw "backup FAILED - nothing was written. pg_dump did not finish, or the archive did not verify: $($_.Exception.Message)"
    }
    finally {
        # Never leave the dump inside the container, even if the copy failed.
        try { Invoke-Compose -ComposeArgs @('exec', '-T', 'postgres', 'rm', '-f', $tmp) }
        catch { Write-Warning "Could not remove $tmp from the container: $($_.Exception.Message)" }
    }

    if (-not (Test-Path -LiteralPath $partFile)) {
        throw "backup produced no file at $partFile."
    }
    $size = (Get-Item -LiteralPath $partFile).Length
    if ($size -le 0) {
        Remove-Item -LiteralPath $partFile -Force
        throw 'backup is empty - is postgres running?'
    }
    # Only a verified archive gets the real name: nothing that failed above can
    # be mistaken for a backup by a script that globs *.sql.gz.
    Move-Item -LiteralPath $partFile -Destination $outFile -Force

    Write-Host "wrote $outFile ($([math]::Round($size / 1KB, 1)) KB)"
    Write-Host 'Copy it off this machine, and keep .env + philosophy.md with it.'
}

# -- restore --------------------------------------------------------------

function Invoke-Restore {
    param([string]$DumpPath)

    if (-not $DumpPath) {
        Write-Host 'usage: .\pdb.ps1 restore .\backups\portfoliodb-....sql.gz'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $DumpPath)) {
        Write-Host "no such file: $DumpPath"
        exit 1
    }
    $DumpPath = (Resolve-Path -LiteralPath $DumpPath).Path

    # The same guard as the Makefile, and for the same reason: restoring over a
    # live ledger is how data gets lost twice.
    $countRaw = Invoke-Compose -Capture -ComposeArgs @(
        'exec', '-T', 'postgres', 'psql', '-q', '-U', 'portfoliouser', '-d', 'portfoliodb',
        '-tAc', "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
    )
    $count = ($countRaw | Out-String).Trim()
    if ($count -ne '0') {
        Write-Host "Refusing to restore: the database already has $count table(s)."
        Write-Host 'Restoring over a live ledger is how data gets lost twice.'
        Write-Host 'To rebuild from scratch:'
        Write-Host '  docker compose down'
        Write-Host '  docker volume rm portfoliodb_pgdata'
        Write-Host '  docker compose up -d'
        exit 1
    }

    $tmp = "/tmp/pdb-restore-$(Get-Date -Format 'yyyyMMdd-HHmmss').sql.gz"
    Invoke-Compose -ComposeArgs @('cp', $DumpPath, "postgres:$tmp")
    try {
        # The archive must decompress whole before a byte of it reaches psql:
        # a truncated download or a corrupt copy fails here, with the database
        # untouched, instead of half-loading and stopping at a syntax error.
        try {
            Invoke-Compose -ComposeArgs @('exec', '-T', 'postgres', 'gzip', '-t', $tmp)
        }
        catch {
            throw "not a valid gzip: $DumpPath - the archive is truncated or corrupt, nothing was restored."
        }
        # psql keeps going after a SQL error by default and exits 0, so a
        # half-loaded database used to print "restored". ON_ERROR_STOP makes
        # the first error fatal (exit 3), --single-transaction rolls everything
        # back so the target is as empty as the guard above found it, and
        # pipefail makes a gunzip failure count even though psql is last.
        #
        # One error that used to be skipped silently and is now fatal: a dump
        # taken from a database whose public schema had been dropped and
        # recreated carries `CREATE SCHEMA public;`, and a fresh database
        # already has one. The guard above proved the target holds no tables,
        # so when the dump wants to create the schema, drop the empty one
        # first - inside the same transaction, so a failed restore puts it
        # back. `grep -c` rather than `grep -q`, for the pipefail reason above,
        # and no double quotes in the command string, for the 5.1 reason above.
        try {
            Invoke-Compose -ComposeArgs @(
                'exec', '-T', 'postgres', 'bash', '-o', 'pipefail', '-c',
                "pre=''; if test `$(gunzip -c $tmp | grep -c '^CREATE SCHEMA public;') -gt 0; then pre='DROP SCHEMA IF EXISTS public CASCADE;'; fi; { echo `$pre; gunzip -c $tmp; } | psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U portfoliouser -d portfoliodb"
            )
        }
        catch {
            throw "restore FAILED: psql stopped at the first error and rolled back - the database is still empty. $($_.Exception.Message)"
        }
    }
    finally {
        try { Invoke-Compose -ComposeArgs @('exec', '-T', 'postgres', 'rm', '-f', $tmp) }
        catch { Write-Warning "Could not remove $tmp from the container: $($_.Exception.Message)" }
    }

    Write-Host "restored $DumpPath"
    $lots = Invoke-Compose -Capture -ComposeArgs @(
        'exec', '-T', 'postgres', 'psql', '-q', '-U', 'portfoliouser', '-d', 'portfoliodb',
        '-tAc', "SELECT 'lots: '||count(*) FROM lots"
    )
    Write-Host ($lots | Out-String).Trim()
}

# -- help -----------------------------------------------------------------

function Invoke-Help {
    Write-Host ''
    Write-Host 'PortfolioDB - Windows runner' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  .\pdb.ps1 init                       create .env, generate empty secrets, lock it down'
    Write-Host '  .\pdb.ps1 backup [destination]       gzipped pg_dump (default: .\backups)'
    Write-Host '  .\pdb.ps1 restore <file.sql.gz>      load a dump into an EMPTY database'
    Write-Host ''
    Write-Host 'Everything else is one docker compose command, the same on every platform:'
    Write-Host ''
    Write-Host '  docker compose up -d                                                    start the stack'
    Write-Host '  docker compose down                                                     stop it'
    Write-Host '  docker compose ps                                                       service status'
    Write-Host '  docker compose logs -f scheduler                                        follow one service'
    Write-Host '  docker compose run --rm dashboard python app/apply_schema.py             create/refresh tables'
    Write-Host '  docker compose run --rm dashboard python app/demo_seed.py --yes          fictional demo data'
    Write-Host '  docker compose run --rm dashboard python app/positions.py                current positions'
    Write-Host '  docker compose exec postgres psql -U portfoliouser -d portfoliodb        interactive psql'
    Write-Host ''
    Write-Host 'docs/commands.md lists every Makefile target beside the command it runs.'
    Write-Host ''
}

# -- dispatch -------------------------------------------------------------

$first = ''
if ($Rest -and $Rest.Count -gt 0) { $first = $Rest[0] }

switch ($Command.ToLowerInvariant()) {
    'init' { Invoke-Init }
    'backup' { Invoke-Backup -Destination $first }
    'restore' { Invoke-Restore -DumpPath $first }
    'help' { Invoke-Help }
    '-h' { Invoke-Help }
    '--help' { Invoke-Help }
    default {
        Write-Host "Unknown command: $Command"
        Write-Host ''
        Write-Host 'This script covers init, backup and restore. Everything else is a'
        Write-Host 'single docker compose command - see docs/commands.md, or run:'
        Write-Host '  .\pdb.ps1 help'
        exit 1
    }
}
