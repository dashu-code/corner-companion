' 开机自启用：不弹窗口地跑 _boot.cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("WScript.Shell")
ws.Run """" & here & "\_boot.cmd""", 0, False
