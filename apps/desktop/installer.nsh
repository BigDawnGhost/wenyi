
; Upgrade and uninstall behaviour for Wenyi Desktop.
;
; This file is included by tauri-bundler's installer.nsi, but ONLY when
; bundle > windows > nsis > installerHooks names it. Without that setting the
; template has no !include for it at all, the four !ifmacrodef checks are all
; false, and every macro here is skipped silently while the build still
; succeeds. The configuration and this file have to move together, which is
; what packages/core/tests/test_ci_gates.py asserts.
;
; Two properties of where this file lands drive most of what follows.
;
; 1. It is included near the top of installer.nsi, BEFORE the block that
;    declares PRODUCTNAME, BUNDLEID and MANUPRODUCTKEY. Those defines do not
;    exist yet, so referring to them here is at best an empty expansion and at
;    worst a compile error -- and either way the guard would name the wrong
;    registry key. The path is written out below and pinned against INSTALL_KEY
;    in src/paths.rs by a test, so the two cannot drift apart in silence.
;
; 2. NSIS_HOOK_POSTUNINSTALL is inserted OUTSIDE the "delete app data" branch of
;    the Uninstall section. That branch does not filter this macro, so the macro
;    re-tests the checkbox and the update mode itself. Trusting the outer branch
;    is what would delete a user's work on an uninstall that never asked.
;
; A third property shapes the code below. The installer and the uninstaller are
; two different programs, and a helper macro that expands into a compiled
; function is emitted into whichever of them includes it. A macro inserted from
; both sections would therefore define that function twice and fail to link.
; Everything here is built from instructions and compiler directives only, and
; the single Win32 call goes through System::Call, which both programs have.

!include "LogicLib.nsh"

; FILE_ATTRIBUTE_REPARSE_POINT.
!define WENYI_REPARSE_POINT 0x400

; Must match INSTALL_KEY in apps/desktop/src/paths.rs. The installer writes this
; value before any of this runs, so during an upgrade it still names the
; directory the previous version was installed into.
!define WENYI_INSTALL_KEY "Software\wenyi\Wenyi Desktop"
; Layer two, under the user profile. It is deliberately not $LOCALAPPDATA: the
; original isolation review had that wrong, and the wrong value deletes the
; disposable runtime layer while leaving the user's real data in place.
!define WENYI_USER_ROOT "$PROFILE\Wenyi"
; One directory per completed pre-upgrade backup, written by src/updates.rs.
!define WENYI_BACKUP_ROOT "$PROFILE\Wenyi\backup"
; Written last by src/migration/backup.rs, so its presence is what separates a
; finished backup from a copy that was interrupted.
!define WENYI_BACKUP_MARKER "MANIFEST.json"
; Layer one's desktop entry. Only that entry is removed: the same parent also
; holds the CLI and Web caches, and uninstalling Desktop must not take them.
!define WENYI_RUNTIME_ROOT "$PROFILE\.wenyi\desktop"
; Project directories the application keeps beside the installation directory.
!define WENYI_DATA_ROOT "$INSTDIR\..\Wenyi Desktop Data"
; Written by the application when the user keeps projects outside the default
; directory: one absolute path per line. Nothing writes it yet -- choosing a
; project location is a later batch -- but the uninstaller reads it already so
; that the file cannot become a silent no-op once a writer appears.
!define WENYI_LIBRARY_ROOTS "$INSTDIR\..\Wenyi Desktop Data\.library-roots"

; Remove one directory without following a link out of it.
;
; RMDir /r recurses through a junction, so aiming it at a directory a user has
; replaced with a junction to another drive would delete that drive's contents.
; A reparse point is removed as the link it is, and nothing below it is touched.
;
; Measured with NSIS 3.11 on Windows: GetFileAttributes answers
; "DIRECTORY|REPARSE_POINT" for a junction, "DIRECTORY" for a plain directory,
; and an empty attribute string for a path that does not exist.
;
; Usable from both sections, so it stays free of compiled helpers.
; $R0 and $R1 are scratch.
!macro wenyi_remove_tree TARGET
  ${If} ${FileExists} "${TARGET}\*.*"
    StrCpy $R1 "${TARGET}"
    System::Call 'kernel32::GetFileAttributes(t R1) i .R0'
    IntOp $R0 $R0 & ${WENYI_REPARSE_POINT}
    ${If} $R0 = 0
      RMDir /r "${TARGET}"
    ${Else}
      ; Only the link itself. What it points at is not ours to remove.
      RMDir "${TARGET}"
    ${EndIf}
  ${EndIf}
!macroend

; Strip trailing spaces, CR and LF from one variable.
;
; FileRead keeps the line ending, and a path with a stray CR at the end names no
; directory at all, so every line read from a roots file goes through this.
;
; The last character is compared by its code -- space 0x20, CR 0x0D, LF 0x0A --
; because a literal line ending written inside a LogicLib comparison does not
; survive the compiler with the meaning it appears to have.
; $R2 to $R5 are scratch.
!macro wenyi_trim_tail LINE
  ${Do}
    ${If} ${LINE} == ""
      ${Break}
    ${EndIf}
    StrCpy $R4 ${LINE} 1 -1
    StrCpy $R5 0
    ${If} $R4 == " "
      StrCpy $R5 1
    ${Else}
      IntOp $R2 $R4 + 0
      ${If} $R2 = 13
        StrCpy $R5 1
      ${ElseIf} $R2 = 10
        StrCpy $R5 1
      ${EndIf}
    ${EndIf}
    ${If} $R5 = 1
      StrCpy ${LINE} ${LINE} -1
    ${Else}
      ${Break}
    ${EndIf}
  ${Loop}
!macroend

; Whether a completed backup exists under layer two.
;
; Answers "1" when a directory directly below the backup root carries a
; manifest, and "0" otherwise. A directory without one is an interrupted copy,
; which is exactly why the manifest is written last.
!macro wenyi_has_backup RESULT
  StrCpy ${RESULT} "0"
  ${If} ${FileExists} "${WENYI_BACKUP_ROOT}\*.*"
    FindFirst $R6 $R7 "${WENYI_BACKUP_ROOT}\*.*"
    ${Do}
      ${If} $R7 == ""
        ${Break}
      ${EndIf}
      ${If} $R7 != "."
      ${AndIf} $R7 != ".."
      ${AndIf} ${FileExists} "${WENYI_BACKUP_ROOT}\$R7\${WENYI_BACKUP_MARKER}"
        StrCpy ${RESULT} "1"
        ${Break}
      ${EndIf}
      ClearErrors
      FindNext $R6 $R7
      ${If} ${Errors}
        ${Break}
      ${EndIf}
    ${Loop}
    FindClose $R6
  ${EndIf}
!macroend

; Runs before a single file is copied, and refuses an upgrade with no backup.
;
; The in-app updater reaches this installer only after src/updates.rs has
; snapshotted the data directory, and it passes /UPDATE. Someone who
; double-clicks a downloaded setup.exe reaches the same Install section with no
; snapshot behind them and no /UPDATE, and the files this section is about to
; overwrite are the only copy of their work. This is the last place that can
; still stop that.
;
; The /UPDATE case is exempt on purpose. There the backup is enforced by the code
; that launched the installer, including the rule that a failed snapshot aborts
; the upgrade; refusing here as well would only stop an installation old enough
; to predate that code from ever updating to this one.
;
; "Already installed" comes from the installation record rather than from
; /UPDATE, because a manual double-click on a machine that already has Wenyi
; would otherwise look like a first install.
!macro NSIS_HOOK_PREINSTALL
  ${If} $UpdateMode != "1"
    ReadRegStr $R5 HKCU "${WENYI_INSTALL_KEY}" ""
    ${If} $R5 != ""
      !insertmacro wenyi_has_backup $R8
      ${If} $R8 != "1"
        ; Abort stops the section before its first File instruction, so the
        ; installed copy is exactly the one that was there before.
        ; The message has to leave a way forward: an installation that is too
        ; broken to start can still be reinstalled after uninstalling it, and an
        ; uninstall without the "delete the application data" box keeps the data.
        Abort "Wenyi is already installed, but there is no backup of its data, so this installer will not overwrite it.$\r$\n$\r$\nUpdate from inside the installed Wenyi, or uninstall it first (leave the data box clear) and then run this installer again.$\r$\n$\r$\nNothing has been changed."
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; Runs after the uninstaller has removed the application's own files.
;
; Four kinds of directory can be removed here, and by default none of them is:
; the application's own files are all the stock uninstaller removes.
;
; Nothing is removed unless the user ticked "delete the application data" and
; this is not an upgrade. Both conditions are re-tested here because this macro
; runs outside the branch that tested them. A silent uninstall leaves the
; checkbox variable empty, so the comparison is written against "1".
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState == "1"
  ${AndIf} $UpdateMode != "1"
    ; The roots file is read first, because it lives inside a directory that is
    ; removed further down.
    ${If} ${FileExists} "${WENYI_LIBRARY_ROOTS}"
      FileOpen $R9 "${WENYI_LIBRARY_ROOTS}" r
      ${If} $R9 != ""
        ${Do}
          ClearErrors
          FileRead $R9 $R7
          ${If} ${Errors}
            ${Break}
          ${EndIf}
          !insertmacro wenyi_trim_tail $R7
          ${If} $R7 != ""
            !insertmacro wenyi_remove_tree "$R7"
          ${EndIf}
        ${Loop}
        FileClose $R9
      ${EndIf}
    ${EndIf}
    !insertmacro wenyi_remove_tree "${WENYI_DATA_ROOT}"
    !insertmacro wenyi_remove_tree "${WENYI_USER_ROOT}"
    !insertmacro wenyi_remove_tree "${WENYI_RUNTIME_ROOT}"
  ${EndIf}
!macroend