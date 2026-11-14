; Wenyi's current-user-only NSIS template.
; Uses tauri-bundler 2.12.1's template variables and supplied utils.nsh.
; Keep the CLI pin and this contract in sync when upgrading Tauri.
Unicode true
ManifestDPIAware true
ManifestDPIAwareness PerMonitorV2
RequestExecutionLevel user
!if "{{compression}}" == "none"
  SetCompress off
!else
  SetCompressor /SOLID "{{compression}}"
!endif
{{#if signed_plugins_path}}
!addplugindir "{{signed_plugins_path}}"
{{/if}}
!addplugindir "{{additional_plugins_path}}"
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "WordFunc.nsh"
!include "Win\COM.nsh"
!include "Win\Propkey.nsh"
!include "Win\RestartManager.nsh"
!include "utils.nsh"

!define PRODUCTNAME "{{product_name}}"
!define MAINBINARYNAME "{{main_binary_name}}"
!define VERSION "{{version}}"
!define BUNDLEID "{{bundle_id}}"
!define ARCH "{{arch}}"
!define INSTALLMODE "{{install_mode}}"
!define MANUKEY "Software\\{{manufacturer}}"
!define MANUPRODUCTKEY "${MANUKEY}\${PRODUCTNAME}"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCTNAME}"
!define DATA_ROOT "$LOCALAPPDATA\Wenyi\Desktop"
!define WEBVIEW2APPGUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define PLACEHOLDER "placeholder\${PRODUCTNAME}"
!if "${INSTALLMODE}" != "currentUser"
  !error "Wenyi Desktop supports currentUser installation only."
!endif
!if "{{allow_downgrades}}" != "false"
  !error "Wenyi Desktop must not allow downgrades."
!endif
Name "${PRODUCTNAME}"
BrandingText "{{copyright}}"
OutFile "{{out_file}}"
InstallDir "${PLACEHOLDER}"
VIProductVersion "{{version_with_build}}"
VIAddVersionKey "ProductName" "${PRODUCTNAME}"
VIAddVersionKey "FileDescription" "${PRODUCTNAME}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "{{copyright}}"
!if "{{installer_icon}}" != ""
  !define MUI_ICON "{{installer_icon}}"
!endif
!if "{{uninstaller_icon}}" != ""
  !define MUI_UNICON "{{uninstaller_icon}}"
!endif
!if "{{uninstaller_sign_cmd}}" != ""
  !uninstfinalize '{{uninstaller_sign_cmd}}'
!endif

Var PassiveMode
Var UpdateMode
Var NoShortcutMode
Var SetupMutex
Var PreviousDirectory
Var PreviousLayout
Var PreviousBinary
Var DeleteSettings
Var DeleteProjects
Var SettingsCheckbox
Var ProjectsCheckbox

!define MUI_LANGDLL_REGISTRY_ROOT HKCU
!define MUI_LANGDLL_REGISTRY_KEY "${MANUPRODUCTKEY}"
!define MUI_LANGDLL_REGISTRY_VALUENAME "Installer Language"
!define MUI_WELCOMEPAGE_TEXT "$(WenyiWelcome)"
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!insertmacro MUI_PAGE_WELCOME
!if "{{license}}" != ""
  !define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
  !insertmacro MUI_PAGE_LICENSE "{{license}}"
!endif
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_NOAUTOCLOSE
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION RunMainBinary
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!insertmacro MUI_PAGE_FINISH

!define MUI_PAGE_CUSTOMFUNCTION_PRE un.SkipIfPassive
!insertmacro MUI_UNPAGE_CONFIRM
UninstPage custom un.DataPage un.DataPageLeave
!insertmacro MUI_UNPAGE_INSTFILES
{{#each languages}}
!insertmacro MUI_LANGUAGE "{{this}}"
{{/each}}
!insertmacro MUI_RESERVEFILE_LANGDLL
!include "{{installer_hooks}}"

Function .onInit
  !insertmacro SetContext
  ${GetOptions} $CMDLINE "/P" $PassiveMode
  ${IfNot} ${Errors}
    StrCpy $PassiveMode 1
  ${EndIf}
  ${GetOptions} $CMDLINE "/UPDATE" $UpdateMode
  ${IfNot} ${Errors}
    StrCpy $UpdateMode 1
  ${EndIf}
  ${GetOptions} $CMDLINE "/NS" $NoShortcutMode
  ${IfNot} ${Errors}
    StrCpy $NoShortcutMode 1
  ${EndIf}
  ; NSIS removes /D= from $CMDLINE and discards invalid values (drive roots).
  ; Validate the raw request instead of silently installing elsewhere.
  System::Call 'kernel32::GetCommandLineW() w .r0'
  ${GetOptions} $0 "/D=" $0
  ${IfNot} ${Errors}
    StrCpy $INSTDIR $0
  ${EndIf}
  !if "{{display_language_selector}}" == "true"
    ${IfNot} ${Silent}
    ${AndIf} $PassiveMode != 1
      !insertmacro MUI_LANGDLL_DISPLAY
    ${EndIf}
  !endif
  !insertmacro WenyiRequireUser
  Call AcquireSetupMutex
  ReadRegStr $PreviousDirectory HKCU "${MANUPRODUCTKEY}" ""
  ReadRegDWORD $PreviousLayout HKCU "${UNINSTKEY}" "DataLayout"
  ReadRegStr $PreviousBinary HKCU "${UNINSTKEY}" "MainBinaryName"
  ${If} $PreviousBinary == ""
    StrCpy $PreviousBinary "${MAINBINARYNAME}.exe"
  ${EndIf}
  ReadRegStr $0 HKCU "${UNINSTKEY}" "UninstallString"
  ${If} $0 == ""
    ; A saved location preference alone is not a registered program.
    StrCpy $PreviousDirectory ""
  ${ElseIfNot} ${FileExists} "$PreviousDirectory\uninstall.exe"
    !insertmacro WenyiFail "$(WenyiUninstallFailed)"
  ${EndIf}
  ${If} $INSTDIR == "${PLACEHOLDER}"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
    ${If} $PreviousLayout == 2
    ${AndIf} $PreviousDirectory != ""
      StrCpy $INSTDIR $PreviousDirectory
    ${EndIf}
  ${ElseIf} $UpdateMode == 1
  ${AndIf} $PreviousLayout != 2
  ${AndIf} $INSTDIR == $PreviousDirectory
    ; The old updater supplies /D=<old program path>. It must not restore the
    ; mixed program/data layout just because it selected that path for us.
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
  ${EndIf}
FunctionEnd

Function CheckDirectory
  !insertmacro WenyiCheckDirectory
FunctionEnd

; All preflight checks precede WebView2, uninstall, registry and payload writes.
Section Preflight
  Call CheckDirectory
  ReadRegStr $0 HKCU "${UNINSTKEY}" "DisplayVersion"
  ${If} $0 != ""
    nsis_tauri_utils::SemverCompare "${VERSION}" $0
    Pop $0
    ${If} $0 == -1
      !insertmacro WenyiFail "$(WenyiDowngrade)"
    ${EndIf}
  ${EndIf}
  !insertmacro WenyiRequireStopped "$INSTDIR"
  ${If} $PreviousDirectory != ""
  ${AndIf} ${FileExists} "$PreviousDirectory\uninstall.exe"
    !insertmacro WenyiRequireStopped "$PreviousDirectory"
  ${EndIf}
SectionEnd

Section WebView2
  ${If} ${RunningX64}
    ReadRegStr $4 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${Else}
    ReadRegStr $4 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${EndIf}
  ${If} $4 == ""
    ReadRegStr $4 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${EndIf}
  ${If} $4 != ""
    !if "{{minimum_webview2_version}}" != ""
      ${VersionCompare} "{{minimum_webview2_version}}" "$4" $0
      ${If} $0 != 1
        Goto webview_done
      ${EndIf}
    !else
      Goto webview_done
    !endif
  ${EndIf}
  ; A process-private temp directory also isolates the dependency installer.
  InitPluginsDir
  !if "{{install_webview2_mode}}" == "downloadBootstrapper"
    NSISdl::download "https://go.microsoft.com/fwlink/p/?LinkId=2124703" "$PLUGINSDIR\webview2.exe"
    Pop $0
    ${If} $0 != "success"
      !insertmacro WenyiFail "$(WenyiWebviewFailed)"
    ${EndIf}
  !else if "{{install_webview2_mode}}" == "embedBootstrapper"
    File "/oname=$PLUGINSDIR\webview2.exe" "{{webview2_bootstrapper_path}}"
  !else if "{{install_webview2_mode}}" == "offlineInstaller"
    File "/oname=$PLUGINSDIR\webview2.exe" "{{webview2_installer_path}}"
  !else
    !insertmacro WenyiFail "$(WenyiWebviewFailed)"
  !endif
  ExecWait '"$PLUGINSDIR\webview2.exe" {{webview2_installer_args}} /install' $0
  ${If} $0 != 0
  ${AndIf} $0 != 3010
    !insertmacro WenyiFail "$(WenyiWebviewFailed)"
  ${EndIf}
  webview_done:
SectionEnd

; Do not remove a usable program until its replacement's dependency is ready.
Section ReplacePrevious
  ${If} $PreviousDirectory != ""
  ${AndIf} ${FileExists} "$PreviousDirectory\uninstall.exe"
    !insertmacro WenyiNoLinkedParents "$PreviousDirectory"
    !insertmacro WenyiRequireStopped "$PreviousDirectory"
    ; A modern uninstaller owns the same mutex. Handoff without concurrent
    ; writers; reacquisition failure cancels this setup instead of racing.
    ${If} $PreviousLayout == 2
      System::Call 'kernel32::CloseHandle(p $SetupMutex)'
      StrCpy $SetupMutex 0
    ${EndIf}
    ClearErrors
    ExecWait '"$PreviousDirectory\uninstall.exe" /S /UPDATE _?=$PreviousDirectory' $0
    ${If} ${Errors}
      StrCpy $0 2
    ${EndIf}
    Push $0
    ${If} $PreviousLayout == 2
      Call AcquireSetupMutex
    ${EndIf}
    Pop $0
    ${If} $0 != 0
      !insertmacro WenyiFail "$(WenyiUninstallFailed)"
    ${EndIf}
    ReadRegStr $0 HKCU "${UNINSTKEY}" "UninstallString"
    ${If} $0 != ""
      !insertmacro WenyiFail "$(WenyiUninstallFailed)"
    ${EndIf}
    ; Only known program files, never recursively remove a legacy mixed folder.
    Delete "$PreviousDirectory\uninstall.exe"
    RMDir "$PreviousDirectory"
  ${EndIf}
SectionEnd

Section Install
  ; Recheck after dependency installation / old-program removal.
  Call CheckDirectory
  {{#each resources_dirs}}
  !insertmacro WenyiNoLinkedParents "$INSTDIR\\{{this}}"
  {{/each}}
  !insertmacro WenyiRequireStopped "$INSTDIR"
  SetOutPath "$INSTDIR"
  File "{{main_binary_path}}"
  {{#each resources_dirs}}
  CreateDirectory "$INSTDIR\\{{this}}"
  {{/each}}
  {{#each resources}}
  File /a "/oname={{this.[1]}}" "{{no-escape @key}}"
  {{/each}}
  {{#each binaries}}
  File /a "/oname={{this}}" "{{no-escape @key}}"
  {{/each}}
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${MANUPRODUCTKEY}" "" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINSTKEY}" "DataLayout" 2
  WriteRegStr HKCU "${UNINSTKEY}" "MainBinaryName" "${MAINBINARYNAME}.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\""
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "{{manufacturer}}"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$\"$INSTDIR$\""
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" "{{estimated_size}}"
  WriteRegStr HKCU "${UNINSTKEY}" "URLInfoAbout" "{{homepage}}"
  ; Retarget shortcuts even on updates that move the program directory.
  ${If} $NoShortcutMode != 1
    CreateShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "$SMPROGRAMS\${PRODUCTNAME}.lnk"
    CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "$DESKTOP\${PRODUCTNAME}.lnk"
  ${EndIf}
  ${If} $PassiveMode == 1
    SetAutoClose true
  ${EndIf}
SectionEnd

Function .onInstSuccess
  ${If} $PassiveMode == 1
  ${OrIf} ${Silent}
    ${GetOptions} $CMDLINE "/R" $0
    ${IfNot} ${Errors}
      ${GetOptions} $CMDLINE "/ARGS" $0
      nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" "$0"
    ${EndIf}
  ${EndIf}
FunctionEnd
Function RunMainBinary
  nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" ""
FunctionEnd
Function SkipIfPassive
  ${IfThen} $PassiveMode == 1 ${|} Abort ${|}
FunctionEnd
Function un.SkipIfPassive
  ${IfThen} $PassiveMode == 1 ${|} Abort ${|}
FunctionEnd

Function un.onInit
  !insertmacro SetContext
  !insertmacro MUI_UNGETLANGUAGE
  !insertmacro WenyiRequireUser
  Call un.AcquireSetupMutex
  StrCpy $PreviousBinary "${MAINBINARYNAME}.exe"
  ${GetOptions} $CMDLINE "/P" $PassiveMode
  ${IfNot} ${Errors}
    StrCpy $PassiveMode 1
  ${EndIf}
  ${GetOptions} $CMDLINE "/UPDATE" $UpdateMode
  ${IfNot} ${Errors}
    StrCpy $UpdateMode 1
  ${EndIf}
FunctionEnd

Section Uninstall
  !insertmacro WenyiNoLinkedParents "$INSTDIR"
  {{#each resources_dirs}}
  !insertmacro WenyiNoLinkedParents "$INSTDIR\\{{this}}"
  {{/each}}
  !insertmacro WenyiRequireStopped "$INSTDIR"
  ; Checks and cleanup occur before unregistering a program, so refusal can be
  ; retried through Add/Remove Programs. Silent/update modes never select data.
  ${If} $UpdateMode != 1
    ${If} $DeleteSettings == 1
    ${OrIf} $DeleteProjects == 1
      !insertmacro WenyiNoLinkedParents "${DATA_ROOT}"
    ${EndIf}
    ${If} $DeleteSettings == 1
      Delete "${DATA_ROOT}\catalog.sqlite3"
      Delete "${DATA_ROOT}\catalog.sqlite3-wal"
      Delete "${DATA_ROOT}\catalog.sqlite3-shm"
      !insertmacro WenyiRemoveTree "${DATA_ROOT}\logs"
      !insertmacro WenyiRemoveTree "${DATA_ROOT}\locks"
      !insertmacro WenyiRemoveTree "${DATA_ROOT}\runtime"
    ${EndIf}
    ${If} $DeleteProjects == 1
      !insertmacro WenyiRemoveTree "${DATA_ROOT}\projects"
      !insertmacro WenyiRemoveTree "${DATA_ROOT}\backups"
    ${EndIf}
    RMDir "${DATA_ROOT}"
  ${EndIf}
  Delete "$INSTDIR\${MAINBINARYNAME}.exe"
  {{#each resources}}
  Delete "$INSTDIR\\{{this.[1]}}"
  {{/each}}
  {{#each binaries}}
  Delete "$INSTDIR\\{{this}}"
  {{/each}}
  Delete "$INSTDIR\uninstall.exe"
  {{#each resources_ancestors}}
  RMDir "$INSTDIR\\{{this}}"
  {{/each}}
  RMDir "$INSTDIR"
  ${If} $UpdateMode != 1
    !insertmacro DeleteAppUserModelId
    !insertmacro IsShortcutTarget "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${IfThen} $0 == 1 ${|} Delete "$SMPROGRAMS\${PRODUCTNAME}.lnk" ${|}
    !insertmacro IsShortcutTarget "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${IfThen} $0 == 1 ${|} Delete "$DESKTOP\${PRODUCTNAME}.lnk" ${|}
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${PRODUCTNAME}"
  ${EndIf}
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "${MANUPRODUCTKEY}"
  DeleteRegKey /ifempty HKCU "${MANUKEY}"
  ${If} $PassiveMode == 1
  ${OrIf} $UpdateMode == 1
    SetAutoClose true
  ${EndIf}
SectionEnd
