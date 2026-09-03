# Pester suite for pdb.ps1's `init`.
#
# Black box on purpose: the script is invoked as a real process in a temp
# directory and the assertions are made against the .env it produces. Two of
# the defects this covers -- the file's text encoding, and the script running at
# all under Windows PowerShell 5.1 -- are invisible to any test that dot-sources
# the functions instead.
#
# `init` is the only command tested here because it is the only one that needs
# no Docker. backup and restore are covered end to end by the wrapper-live job,
# which drives this same script against a real Postgres container.
#
# The host running the script is chosen by PDB_PS_HOST so CI can run the whole
# suite twice: once under powershell.exe (5.1, what a fresh Windows box has) and
# once under pwsh (7+). Pester itself always runs under pwsh.

BeforeAll {
    $script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script:Wrapper = Join-Path $script:RepoRoot 'pdb.ps1'
    $script:Template = Join-Path $script:RepoRoot '.env.template'

    $script:PsHost = $env:PDB_PS_HOST
    if (-not $script:PsHost) { $script:PsHost = 'pwsh' }

    function New-Sandbox {
        param([switch]$NoTemplate)
        $dir = Join-Path ([System.IO.Path]::GetTempPath()) ("pdbt-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Copy-Item -LiteralPath $script:Wrapper -Destination $dir
        if (-not $NoTemplate) {
            Copy-Item -LiteralPath $script:Template -Destination (Join-Path $dir '.env.template')
        }
        return $dir
    }

    function Invoke-Pdb {
        param(
            [Parameter(Mandatory = $true)][string]$Sandbox,
            [string[]]$Arguments = @('init')
        )
        $script = Join-Path $Sandbox 'pdb.ps1'
        $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script) + $Arguments
        $out = & $script:PsHost @all 2>&1
        return [pscustomobject]@{
            Output   = ($out | Out-String)
            ExitCode = $LASTEXITCODE
        }
    }

    function Get-Key {
        param([string]$Sandbox, [string]$Key)
        $envFile = Join-Path $Sandbox '.env'
        foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
            if ($line -match ('^' + [regex]::Escape($Key) + '=(.*)$')) { return $Matches[1] }
        }
        return ''
    }
}

Describe 'pdb.ps1 init' {

    It 'creates .env from the template and sets both passwords to the same value' {
        $sb = New-Sandbox
        try {
            $r = Invoke-Pdb -Sandbox $sb
            $r.ExitCode | Should -Be 0
            Join-Path $sb '.env' | Should -Exist

            $pg = Get-Key $sb 'POSTGRES_PASSWORD'
            $app = Get-Key $sb 'PORTFOLIODB_PASSWORD'

            # The failure this exists to prevent: different values give a
            # Postgres that starts perfectly and an app that cannot connect.
            $pg | Should -Not -BeNullOrEmpty
            $pg | Should -Be $app
            $pg.Length | Should -Be 22
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'generates an MCP token' {
        $sb = New-Sandbox
        try {
            Invoke-Pdb -Sandbox $sb | Out-Null
            (Get-Key $sb 'PORTFOLIODB_MCP_TOKEN').Length | Should -Be 40
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'generates a different secret each run' {
        $a = New-Sandbox
        $b = New-Sandbox
        try {
            Invoke-Pdb -Sandbox $a | Out-Null
            Invoke-Pdb -Sandbox $b | Out-Null
            (Get-Key $a 'POSTGRES_PASSWORD') | Should -Not -Be (Get-Key $b 'POSTGRES_PASSWORD')
        }
        finally { Remove-Item -Recurse -Force $a; Remove-Item -Recurse -Force $b }
    }

    It 'is idempotent and never rotates a secret that is already set' {
        $sb = New-Sandbox
        try {
            Invoke-Pdb -Sandbox $sb | Out-Null
            $before = [System.IO.File]::ReadAllText((Join-Path $sb '.env'))

            $r = Invoke-Pdb -Sandbox $sb
            $after = [System.IO.File]::ReadAllText((Join-Path $sb '.env'))

            $r.ExitCode | Should -Be 0
            $after | Should -Be $before
            $r.Output | Should -Match 'already set'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'writes .env as UTF-8 with no BOM' {
        # Windows PowerShell 5.1's `>` and Out-File default to UTF-16LE, which
        # Compose cannot read -- and it reports the variables as unset rather
        # than as malformed, so the symptom points nowhere near the cause.
        $sb = New-Sandbox
        try {
            Invoke-Pdb -Sandbox $sb | Out-Null
            $bytes = [System.IO.File]::ReadAllBytes((Join-Path $sb '.env'))

            # UTF-8 BOM
            @($bytes[0], $bytes[1], $bytes[2]) | Should -Not -Be @(0xEF, 0xBB, 0xBF)
            # UTF-16 LE/BE BOMs
            @($bytes[0], $bytes[1]) | Should -Not -Be @(0xFF, 0xFE)
            @($bytes[0], $bytes[1]) | Should -Not -Be @(0xFE, 0xFF)
            # A UTF-16 encoding of ASCII text interleaves NUL bytes.
            $bytes[0..200] | Should -Not -Contain 0
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'fills only the empty key when one password is already set' {
        $sb = New-Sandbox
        try {
            @(
                'POSTGRES_PASSWORD=alreadyhere'
                'PORTFOLIODB_PASSWORD='
                'PORTFOLIODB_MCP_TOKEN=keepme'
            ) | Set-Content -Path (Join-Path $sb '.env') -Encoding utf8

            $r = Invoke-Pdb -Sandbox $sb

            $r.ExitCode | Should -Be 0
            Get-Key $sb 'POSTGRES_PASSWORD' | Should -Be 'alreadyhere'
            Get-Key $sb 'PORTFOLIODB_PASSWORD' | Should -Be 'alreadyhere'
            Get-Key $sb 'PORTFOLIODB_MCP_TOKEN' | Should -Be 'keepme'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'warns and changes nothing when the two passwords disagree' {
        $sb = New-Sandbox
        try {
            @(
                'POSTGRES_PASSWORD=aaa'
                'PORTFOLIODB_PASSWORD=bbb'
                'PORTFOLIODB_MCP_TOKEN=keepme'
            ) | Set-Content -Path (Join-Path $sb '.env') -Encoding utf8

            $r = Invoke-Pdb -Sandbox $sb

            $r.Output | Should -Match 'WARNING'
            $r.Output | Should -Match 'fail to connect'
            # Guessing which one is right is how you lock someone out of their
            # own database, so it must leave both alone.
            Get-Key $sb 'POSTGRES_PASSWORD' | Should -Be 'aaa'
            Get-Key $sb 'PORTFOLIODB_PASSWORD' | Should -Be 'bbb'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'fails with a fetch hint when there is no .env and no template' {
        $sb = New-Sandbox -NoTemplate
        try {
            $r = Invoke-Pdb -Sandbox $sb
            $r.ExitCode | Should -Be 1
            $r.Output | Should -Match '\.env\.template'
            $r.Output | Should -Match 'curl'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }
}

Describe 'pdb.ps1 dispatch' {

    It 'prints help by default' {
        $sb = New-Sandbox
        try {
            $r = Invoke-Pdb -Sandbox $sb -Arguments @()
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Match 'init'
            $r.Output | Should -Match 'backup'
            $r.Output | Should -Match 'restore'
            # Help must point at the mapping for everything it does not wrap.
            $r.Output | Should -Match 'docs/commands\.md'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'rejects an unknown command and points at the docs' {
        $sb = New-Sandbox
        try {
            $r = Invoke-Pdb -Sandbox $sb -Arguments @('frobnicate')
            $r.ExitCode | Should -Be 1
            $r.Output | Should -Match 'Unknown command'
            $r.Output | Should -Match 'docs/commands\.md'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'refuses restore without a file argument' {
        $sb = New-Sandbox
        try {
            $r = Invoke-Pdb -Sandbox $sb -Arguments @('restore')
            $r.ExitCode | Should -Be 1
            $r.Output | Should -Match 'usage'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }

    It 'refuses restore when the named dump does not exist' {
        $sb = New-Sandbox
        try {
            $r = Invoke-Pdb -Sandbox $sb -Arguments @('restore', 'nope.sql.gz')
            $r.ExitCode | Should -Be 1
            $r.Output | Should -Match 'no such file'
        }
        finally { Remove-Item -Recurse -Force $sb }
    }
}
