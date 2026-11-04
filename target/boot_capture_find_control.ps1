param(
    [Parameter(Mandatory)][int]$TargetProcessId,
    [Parameter(Mandatory)][string]$NamePattern,
    [string]$Kind = '',
    [string]$Value,
    [switch]$EnsureOn
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$condition = [Windows.Automation.PropertyCondition]::new(
    [Windows.Automation.AutomationElement]::ProcessIdProperty, $TargetProcessId)
$elements = [Windows.Automation.AutomationElement]::RootElement.FindAll(
    [Windows.Automation.TreeScope]::Descendants, $condition)
foreach ($element in $elements) {
    $current = $element.Current
    if ($current.IsOffscreen -or -not $current.IsEnabled) { continue }
    if ($Kind -and $current.ControlType.ProgrammaticName -ne "ControlType.$Kind") { continue }
    $matchesName = if ($NamePattern.StartsWith('@')) {
        $current.AutomationId -eq $NamePattern.Substring(1)
    } else {
        ($current.Name -replace '[_&]', '') -match $NamePattern
    }
    if (-not $matchesName) { continue }
    if ($EnsureOn) {
        $toggle = $element.GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern)
        if ($toggle.Current.ToggleState -ne [Windows.Automation.ToggleState]::On) { $toggle.Toggle() }
    }
    if ($PSBoundParameters.ContainsKey('Value')) {
        $pattern = $element.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
        $pattern.SetValue($Value)
    }
    $rect = $current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
    @{ name = $current.Name; x = $rect.X + $rect.Width / 2; y = $rect.Y + $rect.Height / 2 } |
        ConvertTo-Json -Compress
    exit 0
}
throw "No visible $Kind control matching $NamePattern in process $TargetProcessId"
