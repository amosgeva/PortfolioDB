# PSScriptAnalyzer settings for pdb.ps1 and its tests.
#
# Picked up automatically by VS Code's PowerShell extension and passed
# explicitly by the wrapper-windows CI job, so a local run and a CI run agree
# about what counts as a finding.
#
# Exactly one rule is suppressed. Two others were, until Codacy flagged them on
# the first pull request and it turned out both were better fixed than excused:
# `Set-EnvValue` gained real SupportsShouldProcess (it does rewrite a secrets
# file, so the rule had a point), and the helpers were renamed -- -EnvLines to
# -EnvFile, which is both singular and more accurate, and New-Secret to
# Get-RandomSecret, which drops a state-changing verb from a function that
# changes no state. A linter finding is worth reading before it is silenced.
@{
    ExcludeRules = @(
        # The script talks to a person at a terminal: prompts, progress, and a
        # "here is what to do next" block. Write-Output would put that text on
        # the pipeline, where it would be captured as a return value by any
        # caller and mixed into the data the functions actually return.
        'PSAvoidUsingWriteHost'
    )
}
