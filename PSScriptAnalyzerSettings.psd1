# PSScriptAnalyzer settings for pdb.ps1 and its tests.
#
# Picked up automatically by VS Code's PowerShell extension and passed
# explicitly by the wrapper-windows CI job, so a local run and a CI run agree
# about what counts as a finding.
#
# pdb.ps1 is an interactive command-line script, not a module meant to be
# imported and composed. Three of the default rules assume the opposite, and
# suppressing them here -- once, with the reason -- beats scattering
# [Diagnostics.CodeAnalysis.SuppressMessage] attributes through the script or,
# worse, letting the real findings drown in a hundred lines of noise.
@{
    ExcludeRules = @(
        # The script talks to a person at a terminal: prompts, progress, and a
        # "here is what to do next" block. Write-Output would put that text on
        # the pipeline, where it would be captured as a return value by any
        # caller and mixed into the data the functions actually return.
        'PSAvoidUsingWriteHost',

        # Set-EnvValue and New-Secret are private helpers inside one script, not
        # exported cmdlets. -WhatIf/-Confirm plumbing on them would be dead
        # weight: nothing can call them but this file, and `init` already
        # refuses to overwrite a secret that is set.
        'PSUseShouldProcessForStateChangingFunctions',

        # Read-EnvLines and Write-EnvLines each handle the whole file as an
        # array of lines. The plural is accurate; renaming them to -EnvLine
        # would describe them worse.
        'PSUseSingularNouns'
    )
}
