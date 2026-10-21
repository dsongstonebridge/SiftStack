# A Windows notification that STAYS on screen until it is clicked (Jeff, 2026-10-21).
# scenario="reminder" plus at least one button is what keeps a toast from timing out
# into the notification center. "Open VS Code" opens the SiftStack folder so a Claude
# session is one click away; "Dismiss" just closes it.
# Falls back to a plain message box if the toast API is unavailable.
#
#   powershell -File tools\toast_notify.ps1 -Title "..." -Body "..."
param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Body,
    [string]$OpenUri = "vscode://file/C:/Users/dsong/OneDrive/Desktop/SiftStack"
)
$ErrorActionPreference = "Stop"
function Esc([string]$s) { [System.Security.SecurityElement]::Escape($s) }
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $xml = @"
<toast scenario="reminder" activationType="protocol" launch="$(Esc $OpenUri)">
  <visual><binding template="ToastGeneric">
    <text>$(Esc $Title)</text>
    <text>$(Esc $Body)</text>
  </binding></visual>
  <actions>
    <action content="Open VS Code" activationType="protocol" arguments="$(Esc $OpenUri)"/>
    <action content="Dismiss" activationType="system" arguments="dismiss"/>
  </actions>
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    # Windows PowerShell's own registered app id, so no app registration is needed
    $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
        [Windows.UI.Notifications.ToastNotification]::new($doc))
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Body, $Title, "OK", "Information",
        "Button1", "DefaultDesktopOnly") | Out-Null
}
