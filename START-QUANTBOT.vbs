Option Explicit
Dim shell, files, folder, executable, entry, part, prefix
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
executable = folder & "\.venv\Scripts\pythonw.exe"
prefix = ""
If Not files.FileExists(executable) Then
    executable = ""
    For Each part In Split(shell.Environment("PROCESS")("PATH"), ";")
        part = Replace(part, """", "")
        If Len(part) > 0 Then
            If files.FileExists(part & "\pyw.exe") Then
                executable = part & "\pyw.exe"
                prefix = " -3"
                Exit For
            ElseIf files.FileExists(part & "\pythonw.exe") Then
                executable = part & "\pythonw.exe"
                Exit For
            End If
        End If
    Next
End If
If Len(executable) = 0 Then
    MsgBox "Python was not found. Try START-QUANTBOT.cmd. Python 3.10+ is required.", 48, "QuantBot"
Else
    shell.CurrentDirectory = folder
    shell.Run """" & executable & """" & prefix & " """ & folder & "\windows_gui.py""", 0, False
End If
