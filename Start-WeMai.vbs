Option Explicit

Dim shell, fileSystem, root, environment, bootstrapPath, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
root = fileSystem.GetParentFolderName(WScript.ScriptFullName)
bootstrapPath = fileSystem.BuildPath(root, "bootstrap.py")
shell.CurrentDirectory = root

Set environment = shell.Environment("PROCESS")
environment("WEMAI_LAUNCHED_WINDOW") = "1"

On Error Resume Next
Err.Clear
command = "py.exe -3 -X utf8 " & Quote(bootstrapPath)
shell.Run command, 1, False
If Err.Number = 0 Then
    WScript.Quit 0
End If

Err.Clear
command = "python.exe -X utf8 " & Quote(bootstrapPath)
shell.Run command, 1, False
If Err.Number = 0 Then
    WScript.Quit 0
End If
On Error GoTo 0

MsgBox "Python 3.9 or newer was not found. Install 64-bit Python and add it to PATH.", 16, "WeMai"
WScript.Quit 1

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
