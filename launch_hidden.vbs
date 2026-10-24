' Shared launcher used by the private run_hidden*.vbs profiles.
' Each profile supplies the FTP endpoint and both directories explicitly.

Sub LaunchHidden(host, port, user, password, claudeDirectory, codexDirectory, logName)
    LaunchHiddenConnection "ftp", host, port, user, password, "", "", _
        claudeDirectory, codexDirectory, logName
End Sub

Sub LaunchHiddenSftp(host, port, user, keyFile, knownHosts, claudeDirectory, codexDirectory, logName)
    LaunchHiddenConnection "sftp", host, port, user, "", keyFile, knownHosts, _
        claudeDirectory, codexDirectory, logName
End Sub

Sub LaunchHiddenConnection(protocol, host, port, user, password, keyFile, knownHosts, claudeDirectory, codexDirectory, logName)
    Dim shell, fso, env, directory, arguments, command, i
    Set shell = CreateObject("WScript.Shell")
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set env = shell.Environment("Process")
    env("CLACOGUI_LOG_PROFILE") = fso.GetBaseName(WScript.ScriptFullName)

    env("CLACOGUI_PROTOCOL") = protocol
    env("CLACOGUI_HOST") = host
    env("CLACOGUI_PORT") = CStr(port)
    env("CLACOGUI_USER") = user
    env("CLACOGUI_FTP_PASSWORD") = password
    env("CLACOGUI_SSH_KEY") = keyFile
    env("CLACOGUI_SSH_KNOWN_HOSTS") = knownHosts
    env("CLACOGUI_CLAUDE_DIR") = claudeDirectory
    env("CLACOGUI_CODEX_DIR") = codexDirectory
    ' Prevent inherited legacy settings from supplying another profile's folder.
    env("CLACOGUI_WSL_HOME") = ""
    env("CLACOGUI_WSL_CODEX_HOME") = ""

    directory = fso.GetParentFolderName(WScript.ScriptFullName)
    arguments = ""
    For i = 0 To WScript.Arguments.Count - 1
        arguments = arguments & " " & LauncherQuote(WScript.Arguments(i))
    Next

    command = LauncherQuote(directory & "\run.cmd") & arguments & _
        " >> " & LauncherQuote(directory & "\" & logName) & " 2>&1"
    shell.Run "cmd /d /s /c " & LauncherQuote(command), 0, False
End Sub

Function LauncherQuote(value)
    LauncherQuote = Chr(34) & value & Chr(34)
End Function
