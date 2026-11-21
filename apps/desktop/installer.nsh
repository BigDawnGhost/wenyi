; Safety policy for installer.nsi. The template includes this after MUI languages.
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!define WENYI_REPARSE_POINT 0x400

LangString WenyiWelcome ${LANG_ENGLISH} "Wenyi installs program files separately from your data. Each Windows account keeps its own data in %LOCALAPPDATA%\Wenyi\Desktop.$\r$\n$\r$\nOld settings and projects are not imported or migrated. Old data stays where it is."
LangString WenyiWelcome ${LANG_SIMPCHINESE} "Wenyi 的程序与数据分开存放。各 Windows 账户的数据分别保存在各自的 %LOCALAPPDATA%\Wenyi\Desktop。$\r$\n$\r$\n旧设置及项目不会自动导入或迁移，旧数据保留原位。"
LangString WenyiScopeTitle ${LANG_ENGLISH} "Installation scope"
LangString WenyiScopeTitle ${LANG_SIMPCHINESE} "安装范围"
LangString WenyiScopeHint ${LANG_ENGLISH} "Choose who can use this installation."
LangString WenyiScopeHint ${LANG_SIMPCHINESE} "选择哪些用户可以使用此安装。"
LangString WenyiScopeCurrent ${LANG_ENGLISH} "Install for the current user only"
LangString WenyiScopeCurrent ${LANG_SIMPCHINESE} "仅为当前用户安装"
LangString WenyiScopeAll ${LANG_ENGLISH} "Install for all users (requires administrator approval)"
LangString WenyiScopeAll ${LANG_SIMPCHINESE} "为所有用户安装（需要管理员批准）"
LangString WenyiMachineAdmin ${LANG_ENGLISH} "Installing or uninstalling Wenyi for all users requires administrator approval."
LangString WenyiMachineAdmin ${LANG_SIMPCHINESE} "为所有用户安装或卸载 Wenyi 需要管理员批准。"
LangString WenyiMachineDirectory ${LANG_ENGLISH} "All-users setup must use the fixed Program Files folder. Run the release installer to update an all-users installation."
LangString WenyiMachineDirectory ${LANG_SIMPCHINESE} "为所有用户安装时必须使用 Program Files 下的固定目录。请运行发行版安装器更新为所有用户安装的 Wenyi。"
LangString WenyiUserToken ${LANG_ENGLISH} "Windows could not start setup with standard permissions for this account. Start it normally, or choose the all-users installation."
LangString WenyiUserToken ${LANG_SIMPCHINESE} "Windows 无法以此账户的普通权限启动安装程序。请以普通方式运行，或选择为所有用户安装。"
LangString WenyiKeepBusyData ${LANG_ENGLISH} "Wenyi could not obtain exclusive access to your data. Nothing was deleted. Continue uninstalling the program only?"
LangString WenyiKeepBusyData ${LANG_SIMPCHINESE} "无法独占访问文译数据，未删除用户数据。是否仅卸载程序？"
LangString WenyiCleanupFailed ${LANG_ENGLISH} "Could not complete the selected data cleanup. Setup has not removed the program."
LangString WenyiCleanupFailed ${LANG_SIMPCHINESE} "未能完成所选的数据清理，程序尚未卸载。"
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

!macro WenyiFailWithStatus MESSAGE STATUS
  MessageBox MB_OK|MB_ICONSTOP "${MESSAGE}" /SD IDOK
  SetErrorLevel ${STATUS}
  Quit
!macroend

!macro WenyiFail MESSAGE
  !insertmacro WenyiFailWithStatus "${MESSAGE}" 2
!macroend

; The reduced child need not inherit the parent's handles. Report Win32
; errors via the process status too: 100000 + stage * 10000 + Win32 error.
!macro WenyiTokenFailure STAGE OPERATION ERROR
  IntOp $6 ${STAGE} * 10000
  IntOp $6 $6 + 100000
  IntOp $6 $6 + ${ERROR}
  !insertmacro WenyiFailWithStatus "$(WenyiUserToken) (${OPERATION}: ${ERROR})" $6
!macroend

; Tauri ships an x86 Unicode NSIS stub even for the x64 payload.
!if ${NSIS_PTR_SIZE} != 4
  !error "Review the Win32 process structure layouts before changing the NSIS stub architecture."
!endif

; Both privilege handoffs own the child handle and propagate its exit status.
!macro WenyiWaitAndQuit HANDLE
  HideWindow
  System::Call 'kernel32::WaitForSingleObject(p ${HANDLE}, i -1) i .r8'
  ${If} $8 == 0
    System::Call 'kernel32::GetExitCodeProcess(p ${HANDLE}, *i .r7) i .r8'
    ${IfThen} $8 == 0 ${|} StrCpy $7 2 ${|}
  ${Else}
    StrCpy $7 2
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p ${HANDLE})'
  SetErrorLevel $7
  Quit
!macroend

; $1 contains only switches selected here, never the caller's raw command line.
!macro WenyiElevateAndWait FILE
  StrCpy $0 "${FILE}"
  System::Call '*(&w6 "runas") p .r2'
  System::Call '*(&w${NSIS_MAX_STRLEN} r0) p .r6'
  System::Call '*(&w${NSIS_MAX_STRLEN} r1) p .r7'
  ; SHELLEXECUTEINFOW: NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI.
  System::Call '*(&l4, i 0x540, p $HWNDPARENT, p r2, p r6, p r7, p 0, i 1, p 0, p 0, p 0, p 0, i 0, p 0, p 0) p .r3'
  System::Call 'shell32::ShellExecuteExW(p r3) i .r4 ?e'
  Pop $8
  System::Call '*$3(&v56, p .r5)'
  System::Free $3
  System::Free $7
  System::Free $6
  System::Free $2
  ${If} $4 == 0
  ${AndIf} $8 == 1223
    SetErrorLevel 1223
    Quit
  ${EndIf}
  ${If} $4 == 0
  ${OrIf} $5 == 0
    !insertmacro WenyiFail "$(WenyiMachineAdmin)"
  ${EndIf}
  !insertmacro WenyiWaitAndQuit r5
!macroend

; Re-run as the SAME account before consulting the previous installation.
; A linked token is not necessarily a usable primary token, and built-in
; Administrator may not have one. Derive a normal-user token from this process
; instead of borrowing Explorer's (potentially different account's) identity.
!macro WenyiRunCurrentUser ACTION
  System::Call 'shell32::IsUserAnAdmin() i .r0'
  ${If} $0 != 0
    ${GetOptions} $CMDLINE "/WenyiReduced" $0
    ${IfNot} ${Errors}
      !insertmacro WenyiTokenFailure 0 "Verify reduced token" 0
    ${EndIf}
    ; The derived token needs QUERY | DUPLICATE | ASSIGN_PRIMARY to launch
    ; the process, and ADJUST_DEFAULT to lower its integrity level.
    System::Call 'advapi32::OpenProcessToken(p -1, i 0x8B, *p .r1) i .r0 ?e'
    Pop $9
    ${If} $0 == 0
      !insertmacro WenyiTokenFailure 1 "OpenProcessToken" $9
    ${EndIf}
    ; Remove privileges and mark BUILTIN\Administrators deny-only.
    System::Call 'advapi32::ConvertStringSidToSidW(w "S-1-5-32-544", *p .r3) i .r0 ?e'
    Pop $9
    ${If} $0 == 0
      !insertmacro WenyiTokenFailure 2 "Administrators SID" $9
    ${EndIf}
    System::Call '*(p r3, i 0) p .r4'
    System::Call 'advapi32::CreateRestrictedToken(p r1, i 1, i 1, p r4, i 0, p 0, i 0, p 0, *p .r2) i .r0 ?e'
    Pop $9
    System::Free $4
    System::Call 'kernel32::LocalFree(p r3)'
    System::Call 'kernel32::CloseHandle(p r1)'
    ${If} $0 == 0
      !insertmacro WenyiTokenFailure 3 "CreateRestrictedToken" $9
    ${EndIf}
    System::Call 'advapi32::ConvertStringSidToSidW(w "S-1-16-8192", *p .r3) i .r0 ?e'
    Pop $9
    ${If} $0 == 0
      !insertmacro WenyiTokenFailure 4 "Medium integrity SID" $9
    ${EndIf}
    System::Call '*(p r3, i 0x20) p .r4'
    System::Call 'advapi32::GetLengthSid(p r3) i .r0'
    IntOp $0 $0 + 8
    System::Call 'advapi32::SetTokenInformation(p r2, i 25, p r4, i r0) i .r0 ?e'
    Pop $9
    System::Free $4
    System::Call 'kernel32::LocalFree(p r3)'
    ${If} $0 == 0
      !insertmacro WenyiTokenFailure 5 "SetTokenInformation" $9
    ${EndIf}
    ${GetParameters} $3
    !if "${ACTION}" == "install"
      StrCpy $4 "$EXEPATH"
      StrCpy $3 '"$4" /WenyiReduced /CurrentUser /LANG=$LANGUAGE $3'
      ${IfThen} $RequestedDirectory != "" ${|} StrCpy $3 "$3 /D=$RequestedDirectory" ${|}
    !else
      ; Use the installed executable, not the admin-only temp clone. NSIS
      ; creates its own standard-user temp clone for interactive self-removal.
      StrCpy $4 "$INSTDIR\uninstall.exe"
      StrCpy $3 '"$4" /WenyiReduced $3'
      ${IfThen} $EXEPATH == $4 ${|} StrCpy $3 "$3 _?=$INSTDIR" ${|}
    !endif
    StrLen $0 $3
    IntOp $0 $0 + 1
    ${If} $0 >= ${NSIS_MAX_STRLEN}
      !insertmacro WenyiTokenFailure 6 "Command line length" 0
    ${EndIf}
    ; STARTUPINFOW (cb and zeroed fields), PROCESS_INFORMATION.
    System::Call '*(&l4, &v64) p .r5'
    System::Call '*(p 0, p 0, i 0, i 0) p .r6'
    System::Call 'advapi32::CreateProcessAsUserW(p r2, w r4, w r3, p 0, p 0, i 0, i 0, p 0, w "$SYSDIR", p r5, p r6) i .r0 ?e'
    Pop $9
    ${If} $0 == 0
      ; Some sessions allow token assignment through Secondary Logon instead.
      System::Call 'advapi32::CreateProcessWithTokenW(p r2, i 0, w r4, w r3, i 0, p 0, w "$SYSDIR", p r5, p r6) i .r0 ?e'
      Pop $8
    ${EndIf}
    System::Call 'kernel32::CloseHandle(p r2)'
    System::Call '*$6(p .r1, p .r2)'
    System::Free $5
    System::Free $6
    ${If} $0 == 0
      !insertmacro WenyiTokenFailure 7 "CreateProcessAsUser / CreateProcessWithTokenW" $8
    ${EndIf}
    System::Call 'kernel32::CloseHandle(p r2)'
    !insertmacro WenyiWaitAndQuit r1
  ${EndIf}
!macroend

Function ChooseScope
  ${If} $PassiveMode == 1
  ${OrIf} $ScopeFromCommand == 1
  ${OrIf} $UpdateMode == 1
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "$(WenyiScopeTitle)" "$(WenyiScopeHint)"
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    !insertmacro WenyiFail "$(WenyiUninstallFailed)"
  ${EndIf}
  ${NSD_CreateRadioButton} 0 12u 100% 24u "$(WenyiScopeCurrent)"
  Pop $1
  ${NSD_CreateRadioButton} 0 48u 100% 24u "$(WenyiScopeAll)"
  Pop $AllUsersRadio
  ; Prefer the one existing scope for manual reinstalls. If both exist,
  ; deliberately default to the current user, just as for a fresh install.
  ReadRegStr $0 HKCU "${UNINSTKEY}" "UninstallString"
  ReadRegStr $2 HKLM "${MANUPRODUCTKEY}" ""
  ${If} $0 == ""
  ${AndIf} $2 == "${MACHINE_DIR}"
    StrCpy $AllUsers 1
  ${EndIf}
  ${If} $AllUsers == 1
    ${NSD_Check} $AllUsersRadio
  ${Else}
    ${NSD_Check} $1
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function ChooseScopeLeave
  ${NSD_GetState} $AllUsersRadio $AllUsers
  Call LoadInstallScope
FunctionEnd

!macro WenyiMutexFunction PREFIX
Function ${PREFIX}AcquireSetupMutex
  ${If} $AllUsers == 1
    StrCpy $2 "Global\org.wenyi.desktop.machine.setup"
  ${Else}
    StrCpy $2 "Local\org.wenyi.desktop.setup"
  ${EndIf}
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w r2) p .r0 ?e'
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
  !insertmacro WenyiDisjoint "$WINDIR"
  ${If} $AllUsers == 1
    ${If} $INSTDIR != "${MACHINE_DIR}"
      !insertmacro WenyiFail "$(WenyiMachineDirectory)"
    ${EndIf}
  ${Else}
    !insertmacro WenyiDisjoint "${DATA_ROOT}"
    !insertmacro WenyiDisjoint "$LOCALAPPDATA\Wenyi Desktop"
    !insertmacro WenyiDisjoint "$PROFILE\Wenyi"
    !insertmacro WenyiDisjoint "$PROFILE\.wenyi"
    !insertmacro WenyiDisjoint "$PROGRAMFILES32"
    !insertmacro WenyiDisjoint "$PROGRAMFILES64"
  ${EndIf}
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
!macro WenyiRegisterExisting FILE
  System::Call 'kernel32::GetFileAttributesW(w "${FILE}") i .r0 ?e'
  Pop $1
  ${If} $0 != -1
    ${If} $R8 == ""
      !insertmacro RestartManager_StartSession $R8
      ${If} $R8 == ""
        !insertmacro WenyiFailWithStatus "$(WenyiProcessCheckFailed)" 9
      ${EndIf}
    ${EndIf}
    !insertmacro RestartManager_RegisterFile $R8 "${FILE}"
    ${If} $0 != 0
      !insertmacro RestartManager_EndSession $R8
      !insertmacro WenyiFailWithStatus "$(WenyiProcessCheckFailed)" 9
    ${EndIf}
  ${ElseIf} $1 != 2
  ${AndIf} $1 != 3
    ${If} $R8 != ""
      !insertmacro RestartManager_EndSession $R8
    ${EndIf}
    !insertmacro WenyiFailWithStatus "$(WenyiProcessCheckFailed)" 9
  ${EndIf}
!macroend

!macro WenyiRequireStopped DIRECTORY
  !define WENYI_CHECK_ID ${__LINE__}
  wenyi_check_${WENYI_CHECK_ID}:
  StrCpy $R8 ""
  !insertmacro WenyiRegisterExisting "${DIRECTORY}\${MAINBINARYNAME}.exe"
  !insertmacro WenyiRegisterExisting "${DIRECTORY}\sidecar\wenyi-engine\wenyi-engine.exe"
  ${If} $PreviousBinary != "${MAINBINARYNAME}.exe"
    !insertmacro WenyiRegisterExisting "${DIRECTORY}\$PreviousBinary"
  ${EndIf}
  ${If} $R8 == ""
    Goto wenyi_checked_${WENYI_CHECK_ID}
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
    !insertmacro WenyiFailWithStatus "$(WenyiProcessCheckFailed)" 9
  ${EndIf}
  wenyi_checked_${WENYI_CHECK_ID}:
  !undef WENYI_CHECK_ID
!macroend

Function un.DataPage
  ${If} $PassiveMode == 1
  ${OrIf} $UpdateMode == 1
  ${OrIf} $AllUsers == 1
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

Function un.CleanUserData
  StrCpy $1 "projects"
  ${If} $DeleteSettings == 1
    StrCpy $1 "settings"
    ${IfThen} $DeleteProjects == 1 ${|} StrCpy $1 "both" ${|}
  ${EndIf}
  ClearErrors
  ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --uninstall-user-data $1' $0
  ${IfNot} ${Errors}
    ${If} $0 == 0
      Return
    ${ElseIf} $0 == 3
      MessageBox MB_YESNO|MB_DEFBUTTON2|MB_ICONEXCLAMATION "$(WenyiKeepBusyData)" /SD IDNO IDYES keep_data
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
  !insertmacro WenyiFail "$(WenyiCleanupFailed)"
  keep_data:
FunctionEnd
