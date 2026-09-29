param(
    [string]$Title = "ZCode 活动提醒",
    [string]$Message = ""
)

$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName("text")
$null = $texts.Item(0).AppendChild($xml.CreateTextNode($Title))
$null = $texts.Item(1).AppendChild($xml.CreateTextNode($Message))

$toast = New-Object Windows.UI.Notifications.ToastNotification($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("ZCode.ActivityMonitor").Show($toast)
