; Upgrade, install-location and uninstall behaviour for Wenyi Desktop.
;
; This file is included by tauri-bundler's installer.nsi, but ONLY when
; bundle > windows > nsis > installerHooks names it. Without that setting the
; template has no !include for it at all, the four !ifmacrodef checks are all
; false, and every macro here is skipped silently while the build still
; succeeds. The configuration and this file have to move together, which is
; what packages/core/tests/test_ci_gates.py asserts.
;
; Four properties of where this file lands drive most of what follows.
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
; 3. The installer and the uninstaller are two different programs, and a helper
;    macro that expands into a compiled function is emitted into whichever of
;    them includes it. A macro inserted from both sections would therefore
;    define that function twice and fail to link. Everything here is built from
;    instructions and compiler directives only, and the two Win32 calls go
;    through System::Call, which both programs have.
;
; 4. Every System::Call here reads its arguments from a register by name, so a
;    value a call needs has to be in R0 to R9. Two consequences shape the code
;    below: the installation-location check keeps $INSTDIR in a register and
;    reloads it after each call, and it trims a trailing separator inline
;    instead of calling a sub-macro, because a sub-macro would have to answer
;    through a named variable that no call can read.
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
;
; $PROFILE is the profile of the account running this installer, which is the
; right one only while the bundle stays in the currentUser install mode. Under
; perMachine, or when setup is launched from an elevated shell, it names the
; administrator instead: this would then delete a directory the user never owned
; and leave their real data behind. The bundle currently inherits currentUser
; from tauri-bundler's default; the batch that declares the mode explicitly is
; what keeps that from being an accident.
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

; The size of a path buffer. System::Call takes it as an argument to
; GetFullPathName, which needs the length of the buffer it may fill.
!define WENYI_PATH_BUFFER ${NSIS_MAX_STRLEN}

; File name of the writability probe. Its presence inside an existing directory
; is what decides whether a folder already belongs to something else, so the name
; has to be one no user or application would create by accident.
!define WENYI_INSTALL_PROBE ".wenyi-install-probe"
; Marker of a Wenyi installation. It is the uninstaller the template writes at
; the end of a completed install, so a directory carrying it is one this
; application already owns and an upgrade may proceed.
!define WENYI_UNINSTALLER "uninstall.exe"

; ---------------------------------------------------------------------------
; Installation-location refusals
; ---------------------------------------------------------------------------
;
; Register discipline, because NSIS has no local variables and System::Call reads
; its arguments from a register by name:
;
;   $R0 to $R4   scratch inside one helper, never live across a call
;   $R5          the answer of the helper that just ran
;   $R7          the installation directory, trimmed, for the whole decision
;   $R8          one user layer, trimmed, for as long as it is being compared
;   $R9          the refusal message, or empty
;
; A helper never writes above its own scratch set except through its RESULT
; parameter, and no caller passes a scratch register as an argument. Breaking
; either rule is silent: the compiled script still runs, and a call simply
; answers about whatever the argument held after it was overwritten.

; Answer "1" in RESULT when PATH is a drive root or a share root, "0" otherwise.
;
; shlwapi::PathIsRoot is the same predicate paths.rs applies when it refuses to
; give a volume root a sibling directory, so the two halves of the application
; agree about what a root is without either of them listing drives. Measured with
; NSIS 3.11 on Windows: "D:\" and "\\server\share" answer 1, while "D:",
; "C:\Program Files" and "\\server\share\folder" answer 0. The second of those
; is not an accident -- it is the spelling a drive root reaches this test in, which
; is why the bare drive is answered here rather than left to the library.
!macro wenyi_is_root PATH RESULT
  StrCpy $R0 "${PATH}"
  StrCpy ${RESULT} "0"
  StrLen $R2 "$R0"
  StrCpy $R1 "$R0" 1 -1
  ${If} $R2 == 2
  ${AndIf} $R1 == ":"
    ; "D:" is the current directory on drive D and "D:\" is the drive; NSIS
    ; normalises $INSTDIR to the shorter spelling, so a drive root arrives here
    ; without its separator and PathIsRoot would answer 0 for it. Measured: a
    ; plain variable holds "D:\" as three characters, and assigning the same
    ; text to $INSTDIR leaves "D:" as two.
    StrCpy ${RESULT} "1"
  ${Else}
    System::Call 'shlwapi::PathIsRootW(t R0) i .R1'
    ${If} $R1 <> 0
      StrCpy ${RESULT} "1"
    ${EndIf}
  ${EndIf}
!macroend

; Answer "1" in RESULT when INNER is OUTER or below it, "0" otherwise.
;
; The test is the start of INNER against OUTER, followed by the character at the
; boundary. A bare prefix test is not enough: layer three's default directory is
; "Wenyi Desktop Data", whose first characters spell the installation's "Wenyi
; Desktop", and a prefix test would call those two nested and refuse the layout
; every default install produces. Measured: the two names share a 13-character
; prefix and the longer one continues with "D", not with a separator, which is
; exactly what this test looks at.
;
; Equality counts as nested, matching the native assertion: an installation that
; is layer two itself is the case where one uninstall removes both the program and
; the settings, which is the outcome the rule exists to prevent.
;
; StrCmp is case-insensitive, which is the comparison the Windows filesystem
; applies. Neither argument may carry a trailing separator.
!macro wenyi_contains INNER OUTER RESULT
  StrCpy $R0 "${OUTER}"
  StrCpy $R1 "${INNER}"
  StrLen $R2 "$R0"
  StrLen $R3 "$R1"
  StrCpy ${RESULT} "0"
  ${If} $R3 >= $R2
    StrCpy $R4 "$R1" $R2
    ${If} $R4 == "$R0"
      ${If} $R3 == $R2
        StrCpy ${RESULT} "1"
      ${Else}
        StrCpy $R4 "$R1" 1 $R2
        ${If} $R4 == "\"
          StrCpy ${RESULT} "1"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; Answer PATH in RESULT with a trailing separator removed.
;
; The containment test compares the tail of one name with another name, so a
; trailing separator would hide the boundary that test looks for. A drive root is
; the exception: "D:" names the current directory on drive D and "D:\" names the
; drive, so there the separator is the whole of what the path says.
!macro wenyi_trim_separator PATH RESULT
  StrCpy $R0 "${PATH}"
  StrCpy $R1 "$R0" 1 -1
  ${If} $R1 == "\"
    StrCpy $R0 "$R0" -1
    StrCpy $R1 "$R0" 1 -1
    ${If} $R1 == ":"
      StrCpy $R0 "${PATH}"
    ${EndIf}
  ${EndIf}
  StrCpy ${RESULT} "$R0"
!macroend

; Answer "1" in RESULT when this installation may write into PATH, "0" otherwise.
;
; CreateDirectory is the first write, so running it answers the same question for
; a directory that does not exist yet. Measured under a restricted token on this
; machine: creating a directory below C:\Program Files leaves no directory behind
; and the probe below then fails, while the same two steps succeed in a folder of
; the user's own. A directory this check created is removed again, but only when
; this check created it, and RMDir without /r refuses a directory holding
; anything, so a folder the user already had is left exactly as it was.
;
; The probe is a file, because FileOpen and Delete both answer through the error
; flag. Measured: FileOpen into a directory that does not exist sets that flag,
; and Delete of a file that is not there clears it, so deletion needs no result.
!macro wenyi_install_dir_writable PATH RESULT
  StrCpy $R0 "${PATH}"
  ${If} ${FileExists} "$R0\*.*"
    StrCpy $R1 "1"
  ${Else}
    StrCpy $R1 "0"
  ${EndIf}
  CreateDirectory "$R0"
  ClearErrors
  FileOpen $R2 "$R0\${WENYI_INSTALL_PROBE}" w
  StrCpy ${RESULT} "0"
  ${IfNot} ${Errors}
    FileClose $R2
    Delete "$R0\${WENYI_INSTALL_PROBE}"
    StrCpy ${RESULT} "1"
  ${EndIf}
  ${If} $R1 == "0"
    RMDir "$R0"
  ${EndIf}
!macroend

; Answer "1" in RESULT when PATH already holds a Wenyi installation.
;
; The installation record is the authority: the installer writes it before any of
; this runs and keeps it across upgrades. The uninstaller beside the executable is
; the second spelling of the same answer, for an installation whose record was
; removed by hand.
!macro wenyi_is_own_installation PATH RESULT
  StrCpy ${RESULT} "0"
  ${If} ${FileExists} "${PATH}\${WENYI_UNINSTALLER}"
    StrCpy ${RESULT} "1"
  ${Else}
    ReadRegStr $R0 HKCU "${WENYI_INSTALL_KEY}" ""
    ${If} $R0 != ""
    ${AndIf} $R0 == "${PATH}"
      StrCpy ${RESULT} "1"
    ${EndIf}
  ${EndIf}
!macroend

; Answer "1" in RESULT when PATH exists, is not this application's, and already
; carries content. An empty directory is a placeholder this install may take
; over; a directory holding another application's files is not.
;
; This is the weakest of the four checks and is written to say so. It refuses
; rather than merging, because a version of this application that predates the
; project-location batch had no way to own a directory other than the one its
; record names, so a file under any other name was put there by something else.
;
; An upgrade never reaches this check with content in the way: its own directory
; answers "1" to wenyi_is_own_installation above.
!macro wenyi_target_is_occupied PATH RESULT
  StrCpy ${RESULT} "0"
  ${If} ${FileExists} "${PATH}\*.*"
    !insertmacro wenyi_is_own_installation "${PATH}" $R2
    ${If} $R2 != "1"
      FindFirst $R0 $R1 "${PATH}\*.*"
      ${Do}
        ${If} $R1 == ""
          ${Break}
        ${EndIf}
        ${If} $R1 != "."
        ${AndIf} $R1 != ".."
          StrCpy ${RESULT} "1"
          ${Break}
        ${EndIf}
        ClearErrors
        FindNext $R0 $R1
        ${If} ${Errors}
          ${Break}
        ${EndIf}
      ${Loop}
      FindClose $R0
    ${EndIf}
  ${EndIf}
!macroend

; Decide whether the installation directory may be used, and describe the first
; reason it may not in RESULT. Empty means admitted.
;
; The subject is always $INSTDIR, never a parameter: that is the directory the
; installer is about to write into and the one the refusal has to name, and a
; second spelling of "the directory being installed" is how a check and the write
; it guards come to disagree.
;
; The order is the order of the reasons a person can act on. A volume root comes
; first because a drive root is writable and the write probe alone would admit
; it. The overlap checks come before writability and occupancy because "inside
; your own profile" is a reason that can be fixed and "this folder has files in
; it" is not.
;
; The two layers are resolved by the same defines the uninstaller uses, which is
; what makes this check agree with the directories that hook would later remove.
;
; The loop is how a macro returns early: NSIS's Return would leave the whole
; Install section, and each Break here leaves only this decision.
!macro wenyi_refuse_install_location RESULT
  StrCpy ${RESULT} ""
  !insertmacro wenyi_trim_separator "$INSTDIR" $R7

  ${Do}
    !insertmacro wenyi_is_root $R7 $R5
    ${If} $R5 == "1"
      StrCpy ${RESULT} "Wenyi cannot be installed at a drive or share root. Choose a folder that has a name of its own, such as D:\Apps\Wenyi Desktop. Wenyi keeps your projects in a folder beside its own program folder, and a drive root has nothing beside it."
      ${Break}
    ${EndIf}

    !insertmacro wenyi_trim_separator "${WENYI_RUNTIME_ROOT}" $R8
    !insertmacro wenyi_contains $R7 $R8 $R5
    ${If} $R5 == "1"
      StrCpy ${RESULT} "Wenyi cannot be installed inside %USERPROFILE%\.wenyi, which holds the runtime that Wenyi deletes and rebuilds. Choose a folder of its own."
      ${Break}
    ${EndIf}
    !insertmacro wenyi_contains $R8 $R7 $R5
    ${If} $R5 == "1"
      StrCpy ${RESULT} "That folder contains %USERPROFILE%\.wenyi, the runtime that Wenyi deletes and rebuilds. Installing Wenyi there would put its program files inside a folder that gets cleared. Choose a folder of its own."
      ${Break}
    ${EndIf}

    !insertmacro wenyi_trim_separator "${WENYI_USER_ROOT}" $R8
    !insertmacro wenyi_contains $R7 $R8 $R5
    ${If} $R5 == "1"
      StrCpy ${RESULT} "Wenyi cannot be installed inside %USERPROFILE%\Wenyi, which holds your settings, your project list and the backups of your work. Uninstalling with the delete option ticked would remove the installation along with them. Choose a folder of its own."
      ${Break}
    ${EndIf}
    !insertmacro wenyi_contains $R8 $R7 $R5
    ${If} $R5 == "1"
      StrCpy ${RESULT} "That folder contains %USERPROFILE%\Wenyi, which holds your settings, your project list and the backups of your work. Installing Wenyi there would put its program files inside the one folder an uninstall can remove. Choose a folder of its own."
      ${Break}
    ${EndIf}

    !insertmacro wenyi_install_dir_writable $R7 $R5
    ${If} $R5 != "1"
      StrCpy ${RESULT} "Windows did not allow Wenyi to write to that folder. Under C:\Program Files and other protected folders, setup would have to run as an administrator, and Wenyi does not ask for that: its own data would then be written to the administrator's profile instead of yours. Choose a folder inside your own profile."
      ${Break}
    ${EndIf}

    !insertmacro wenyi_target_is_occupied $R7 $R5
    ${If} $R5 == "1"
      StrCpy ${RESULT} "That folder already holds files from something else. Installing Wenyi there would place its programs among them and make both harder to remove. Choose an empty folder or a new one."
      ${Break}
    ${EndIf}

    ; Reaching this line means no check refused the directory. The break is what
    ; ends the loop on the admitted path; without it the loop would repeat the
    ; same five checks forever, and setup would hang on a valid location.
    ${Break}
  ${Loop}
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
  ; The four installation-location refusals of the isolation design run first
  ; and leave the backup refusal below untouched. They apply to an upgrade as
  ; well as to a first install, because $INSTDIR survives an upgrade: an
  ; installation an older version made at a drive root would otherwise stay
  ; there for good, with no sibling for its projects to live beside.
  ;
  ; A refusal has to show the directory it refused. The reason is written for
  ; someone who cannot see a directory layout, and a reason that does not name
  ; the folder leaves them guessing which of several paths setup is about to use.
  !insertmacro wenyi_refuse_install_location $R9
  ${If} $R9 != ""
    ; Abort stops the section before its first File instruction, so a refused
    ; location leaves the machine exactly as it was found. Everything below this
    ; point copies files, which is why the check exists here at all: the hook
    ; file is read before tauri-bundler's directory page is declared, so there is
    ; no page callback this file could attach the same check to.
    Abort "$R9$\r$\n$\r$\nSetup was about to use:$\r$\n$INSTDIR$\r$\n$\r$\nNothing has been changed."
  ${EndIf}

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