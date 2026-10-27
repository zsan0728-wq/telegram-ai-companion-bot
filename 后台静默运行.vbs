Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("Wscript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = folder
ws.Run "pythonw.exe """ & folder & "\bot.py""", 0, False
