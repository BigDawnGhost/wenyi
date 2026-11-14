; Safety policy for installer.nsi. The template includes this after MUI languages.
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!define WENYI_REPARSE_POINT 0x400

LangString WenyiWelcome ${LANG_ENGLISH} "Wenyi installs program files separately from your data.$\r$\n$\r$\nDefault program folder:$\r$\n%LOCALAPPDATA%\Programs\Wenyi Desktop$\r$\n$\r$\nDefault data folder:$\r$\n%LOCALAPPDATA%\Wenyi\Desktop$\r$\n$\r$\nThis is a breaking data-format change. Old settings and projects are not imported or migrated. Old data stays where it is."
LangString WenyiWelcome ${LANG_SIMPCHINESE} "Wenyi 的程序与数据分开存放。$\r$\n$\r$\n默认程序目录：$\r$\n%LOCALAPPDATA%\Programs\Wenyi Desktop$\r$\n$\r$\n默认数据目录：$\r$\n%LOCALAPPDATA%\Wenyi\Desktop$\r$\n$\r$\n本次包含不兼容的数据格式变更，不导入或迁移旧设置及项目，旧数据保留原位。"
LangString WenyiElevated ${LANG_ENGLISH} "Close this installer and run it normally, not as administrator. Wenyi installs for the current user only."
LangString WenyiElevated ${LANG_SIMPCHINESE} "请关闭安装器并以普通方式运行，不要使用管理员身份。Wenyi 仅为当前用户安装。"
LangString WenyiSetupBusy ${LANG_ENGLISH} "Another Wenyi installation or uninstallation is running. Finish it before retrying."
LangString WenyiSetupBusy ${LANG_SIMPCHINESE} "另一个 Wenyi 安装或卸载程序正在运行，请等待其结束后重试。"
LangString WenyiRunning ${LANG_ENGLISH} "Wenyi or its translation engine is still running. Quit Wenyi from the tray menu, wait for its tasks to stop, then Retry. Setup will not force-stop translations."
LangString WenyiRunning ${LANG_SIMPCHINESE} "Wenyi 或翻译引擎仍在运行。请从托盘菜单退出 Wenyi，等待任务停止后重试。安装器不会强制终止翻译。"
LangString WenyiProcessCheckFailed ${LANG_ENGLISH} "Windows could not verify that Wenyi and its engine have stopped. Close them and retry."
LangString WenyiProcessCheckFailed ${LANG_SIMPCHINESE} "Windows 无法确认 Wenyi 及其引擎是否已停止，请关闭它们后重试。"
LangString WenyiDirectory ${LANG_ENGLISH} "Choose an empty, writable program folder outside Windows, Program Files, user-data folders and legacy Wenyi directories. Drive roots, the user profile, junctions and symbolic links are not accepted."
LangString WenyiDirectory ${LANG_SIMPCHINESE} "请选择可写的空程序目录，不要放在 Windows、Program Files、用户数据或旧版 Wenyi 目录中。不接受磁盘根目录、用户目录本身、目录联接或符号链接。"
LangString WenyiDowngrade ${LANG_ENGLISH} "A newer Wenyi version is installed. Installing an older version is not supported."
LangString WenyiDowngrade ${LANG_SIMPCHINESE} "已安装更新版本的 Wenyi，不支持降级安装。"
LangString WenyiUninstallFailed ${LANG_ENGLISH} "The previous Wenyi program could not be removed. Its data has not been migrated or deleted. Close Wenyi and retry, or uninstall the old program while keeping its data."
LangString WenyiUninstallFailed ${LANG_SIMPCHINESE} "无法移除原有 Wenyi 程序，未迁移或删除其数据。请关闭 Wenyi 后重试，或先卸载旧程序并保留数据。"
LangString WenyiWebviewFailed ${LANG_ENGLISH} "WebView2 could not be installed. Install Microsoft Edge WebView2 Runtime and run setup again."
LangString WenyiWebviewFailed ${LANG_SIMPCHINESE} "无法安装 WebView2。请安装 Microsoft Edge WebView2 Runtime 后重新运行安装器。"
LangString WenyiDataTitle ${LANG_ENGLISH} "Keep or delete Desktop data"
LangString WenyiDataTitle ${LANG_SIMPCHINESE} "保留或删除桌面版数据"
LangString WenyiDataHint ${LANG_ENGLISH} "Both options are OFF by default. Only %LOCALAPPDATA%\Wenyi\Desktop is affected. Custom data directories, external project libraries and all legacy directories are always kept. Deleting settings also removes the project list; kept project folders are not automatically rediscovered."
LangString WenyiDataHint ${LANG_SIMPCHINESE} "默认不勾选。仅处理 %LOCALAPPDATA%\Wenyi\Desktop；自定义数据目录、外部项目库及所有旧版目录始终保留。删除设置也会删除项目列表，保留的项目文件夹不会被自动重新发现。"
LangString WenyiDeleteSettings ${LANG_ENGLISH} "Delete catalog, settings, logs and runtime (not project files)"
LangString WenyiDeleteSettings ${LANG_SIMPCHINESE} "删除目录库、设置、日志及运行时（不删除项目文件）"
LangString WenyiDeleteProjects ${LANG_ENGLISH} "Permanently delete default project files and upgrade backups"
LangString WenyiDeleteProjects ${LANG_SIMPCHINESE} "永久删除默认项目文件及升级备份"

!macro WenyiFail MESSAGE
  MessageBox MB_OK|MB_ICONSTOP "${MESSAGE}" /SD IDOK
  SetErrorLevel 2
  Quit
!macroend

!macro WenyiRequireUser
  System::Call 'shell32::IsUserAnAdmin() i .r0'
  ${If} $0 != 0
    !insertmacro WenyiFail "$(WenyiElevated)"
  ${EndIf}
!macroend

!macro WenyiMutexFunction PREFIX
Function ${PREFIX}AcquireSetupMutex
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\org.wenyi.desktop.setup") p .r0 ?e'
  Pop $1
  StrCpy $SetupMutex $0
  ${If} $0 == 0
  ${OrIf} $1 == 183
    !insertmacro WenyiFail "$(WenyiSetupBusy)"
  ${EndIf}
FunctionEnd
!macroend
!insertmacro WenyiMutexFunction ""
!insertmacro WenyiMutexFunction "un."

; NSIS GetFullPathName requires the leaf to exist. Normalize new destinations
; without creating them, then expand short names in the nearest existing
; ancestor so an 8.3 spelling cannot bypass protected-directory comparisons.
; OUT is R7/R8; $0-$4 are scratch, as in the other path helpers.
!macro WenyiFullPath PATH OUT
  System::Call 'kernel32::GetFullPathNameW(w "${PATH}", i ${NSIS_MAX_STRLEN}, w .r2, p 0) i .r0'
  ${If} $0 == 0
  ${OrIf} $0 >= ${NSIS_MAX_STRLEN}
    !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n${PATH}"
  ${EndIf}
  StrCpy $3 ""
  ${Do}
    System::Call 'kernel32::GetLongPathNameW(w r2, w .r4, i ${NSIS_MAX_STRLEN}) i .r0'
    ${If} $0 >= ${NSIS_MAX_STRLEN}
      !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n${PATH}"
    ${ElseIf} $0 != 0
      ${Break}
    ${EndIf}
    ${GetFileName} "$2" $1
    StrCpy $3 "\$1$3"
    ${GetParent} "$2" $2
    ${If} $2 == ""
      !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n${PATH}"
    ${EndIf}
  ${Loop}
  StrLen $0 $4
  StrLen $1 $3
  IntOp $0 $0 + $1
  ${If} $0 >= ${NSIS_MAX_STRLEN}
    !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n${PATH}"
  ${EndIf}
  StrCpy ${OUT} "$4$3"
!macroend

; Compare whole components, not a prefix such as Apps and Apps2. R7/R8 are
; caller-owned, R0-R4 are scratch, R5 is the result.
!macro WenyiContains INNER OUTER
  StrCpy $0 "${INNER}"
  StrCpy $1 "${OUTER}"
  StrLen $2 $1
  StrCpy $3 $0 $2
  StrCpy $R5 0
  ${If} $3 == $1
    StrCpy $4 $0 1 $2
    ${If} $4 == ""
    ${OrIf} $4 == "\"
      StrCpy $R5 1
    ${EndIf}
  ${EndIf}
!macroend

!macro WenyiDisjoint ROOT
  !insertmacro WenyiFullPath "${ROOT}" $R8
  StrCpy $0 $R8 1 -1
  ${IfThen} $0 == "\" ${|} StrCpy $R8 $R8 -1 ${|}
  !insertmacro WenyiContains "$R7" "$R8"
  ${IfThen} $R5 == 1 ${|} !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n$INSTDIR" ${|}
  !insertmacro WenyiContains "$R8" "$R7"
  ${IfThen} $R5 == 1 ${|} !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n$INSTDIR" ${|}
!macroend

; Inspect every existing ancestor. Checking only the leaf misses an alias of a
; protected directory and makes later recursive cleanup unsafe.
!macro WenyiNoLinkedParents PATH
  !insertmacro WenyiFullPath "${PATH}" $R8
  ${Do}
    System::Call 'kernel32::GetFileAttributesW(w R8) i .r0'
    ${If} $0 != -1
      IntOp $0 $0 & ${WENYI_REPARSE_POINT}
      ${IfThen} $0 != 0 ${|} !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n$R8" ${|}
    ${EndIf}
    ${GetParent} "$R8" $0
    ${If} $0 == ""
    ${OrIf} $0 == $R8
      ${Break}
    ${EndIf}
    StrCpy $R8 $0
  ${Loop}
!macroend

!macro WenyiCheckDirectory
  !insertmacro WenyiFullPath "$INSTDIR" $R7
  StrCpy $0 $R7 1 -1
  ${IfThen} $0 == "\" ${|} StrCpy $R7 $R7 -1 ${|}
  StrLen $0 $R7
  System::Call 'shlwapi::PathIsRootW(w R7) i .r1'
  ${If} $0 < 3
  ${OrIf} $1 != 0
  ${OrIf} $R7 == $PROFILE
    !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n$INSTDIR"
  ${EndIf}
  !insertmacro WenyiNoLinkedParents "$INSTDIR"
  !insertmacro WenyiDisjoint "${DATA_ROOT}"
  !insertmacro WenyiDisjoint "$LOCALAPPDATA\Wenyi Desktop"
  !insertmacro WenyiDisjoint "$PROFILE\Wenyi"
  !insertmacro WenyiDisjoint "$PROFILE\.wenyi"
  !insertmacro WenyiDisjoint "$WINDIR"
  !insertmacro WenyiDisjoint "$PROGRAMFILES32"
  !insertmacro WenyiDisjoint "$PROGRAMFILES64"
  ; .onInit captured ownership while holding the setup mutex and excluded
  ; stale location preferences. Keep that identity across the old uninstaller,
  ; which deletes its registry entries but deliberately preserves extra files.
  ; Only that current-format directory may be nonempty, never a new target.
  ${If} ${FileExists} "$INSTDIR\*.*"
    ${If} $PreviousDirectory != $INSTDIR
    ${OrIf} $PreviousLayout != 2
      FindFirst $2 $3 "$INSTDIR\*"
      ${DoWhile} $3 != ""
        ${If} $3 != "."
        ${AndIf} $3 != ".."
          FindClose $2
          !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n$INSTDIR"
        ${EndIf}
        FindNext $2 $3
      ${Loop}
      FindClose $2
    ${EndIf}
  ${EndIf}
  ; A unique probe never overwrites an existing user file.
  CreateDirectory "$INSTDIR"
  ClearErrors
  GetTempFileName $0 "$INSTDIR"
  ${If} ${Errors}
    !insertmacro WenyiFail "$(WenyiDirectory)$\r$\n$INSTDIR"
  ${EndIf}
  Delete "$0"
!macroend

; Restart Manager checks the actual installed files, including the sidecar.
; Never force-kill a translation or silently continue after a failed check.
!macro WenyiRequireStopped DIRECTORY
  !define WENYI_CHECK_ID ${__LINE__}
  wenyi_check_${WENYI_CHECK_ID}:
  !insertmacro RestartManager_StartSession $R8
  ${If} $R8 == ""
    !insertmacro WenyiFail "$(WenyiProcessCheckFailed)"
  ${EndIf}
  !insertmacro RestartManager_RegisterFile $R8 "${DIRECTORY}\${MAINBINARYNAME}.exe"
  ${If} $0 != 0
    !insertmacro RestartManager_EndSession $R8
    !insertmacro WenyiFail "$(WenyiProcessCheckFailed)"
  ${EndIf}
  !insertmacro RestartManager_RegisterFile $R8 "${DIRECTORY}\sidecar\wenyi-engine\wenyi-engine.exe"
  ${If} $0 != 0
    !insertmacro RestartManager_EndSession $R8
    !insertmacro WenyiFail "$(WenyiProcessCheckFailed)"
  ${EndIf}
  ${If} $PreviousBinary != "${MAINBINARYNAME}.exe"
    !insertmacro RestartManager_RegisterFile $R8 "${DIRECTORY}\$PreviousBinary"
    ${If} $0 != 0
      !insertmacro RestartManager_EndSession $R8
      !insertmacro WenyiFail "$(WenyiProcessCheckFailed)"
    ${EndIf}
  ${EndIf}
  System::Call 'rstrtmgr::RmGetList(p R8, *i .r1, *i .r2, p 0, *i .r3) i .r0'
  Push $0
  !insertmacro RestartManager_EndSession $R8
  Pop $0
  ${If} $0 == 234
    ${If} ${Silent}
    ${OrIf} $PassiveMode == 1
      !insertmacro WenyiFail "$(WenyiRunning)"
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(WenyiRunning)" IDRETRY wenyi_check_${WENYI_CHECK_ID}
    SetErrorLevel 2
    Quit
  ${ElseIf} $0 != 0
    !insertmacro WenyiFail "$(WenyiProcessCheckFailed)"
  ${EndIf}
  !undef WENYI_CHECK_ID
!macroend

Function un.DataPage
  ${If} $PassiveMode == 1
  ${OrIf} $UpdateMode == 1
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "$(WenyiDataTitle)" ""
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 70u "$(WenyiDataHint)"
  Pop $0
  ${NSD_CreateCheckbox} 0 80u 100% 24u "$(WenyiDeleteSettings)"
  Pop $SettingsCheckbox
  ${NSD_SetState} $SettingsCheckbox $DeleteSettings
  ${NSD_CreateCheckbox} 0 112u 100% 24u "$(WenyiDeleteProjects)"
  Pop $ProjectsCheckbox
  ${NSD_SetState} $ProjectsCheckbox $DeleteProjects
  nsDialogs::Show
FunctionEnd
Function un.DataPageLeave
  ${NSD_GetState} $SettingsCheckbox $DeleteSettings
  ${NSD_GetState} $ProjectsCheckbox $DeleteProjects
FunctionEnd

!macro WenyiRemoveTree PATH
  Push "${PATH}"
  Call un.RemoveTree
!macroend
; Recursion examines EACH entry. RMDir /r would follow a nested junction even
; when the root itself is ordinary. Links are removed, their targets are kept.
Function un.RemoveTree
  Exch $0
  Push $1
  Push $2
  Push $3
  System::Call 'kernel32::GetFileAttributesW(w r0) i .r1'
  ${If} $1 != -1
    IntOp $2 $1 & 0x10
    IntOp $3 $1 & ${WENYI_REPARSE_POINT}
    ${If} $2 == 0
      Delete "$0"
    ${ElseIf} $3 != 0
      RMDir "$0"
    ${Else}
      FindFirst $1 $2 "$0\*"
      ${DoWhile} $2 != ""
        ${If} $2 != "."
        ${AndIf} $2 != ".."
          Push "$0\$2"
          Call un.RemoveTree
        ${EndIf}
        FindNext $1 $2
      ${Loop}
      FindClose $1
      RMDir "$0"
    ${EndIf}
  ${EndIf}
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd
