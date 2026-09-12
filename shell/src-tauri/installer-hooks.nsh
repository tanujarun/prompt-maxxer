; Hooks into Tauri's NSIS installer template.

; Installing over an existing copy - which is what every update does - starts
; from an empty engine folder. Updates copy files over the old ones without
; uninstalling first, so a library the new engine no longer ships would
; otherwise linger beside it. The app has already stopped the engine.
!macro NSIS_HOOK_PREINSTALL
  ${If} ${FileExists} "$INSTDIR\engine\prompt-maxxer-engine.exe"
    RMDir /r "$INSTDIR\engine"
  ${EndIf}
!macroend

; Uninstalling removes what the app downloaded for itself and nothing else.
;
; The GPU libraries are the careful case. Prompt Maxxer downloads NVIDIA's
; cuBLAS and cuDNN into a private folder of its own and puts only that copy on
; its library path, so removing it cannot affect anything else on the machine:
; a CUDA toolkit install, the driver's own libraries, another application's
; copies, or a folder someone pointed the app at with PROMPT_MAXXER_CUDA_DIR.
; Even inside its own folder it deletes only the sets it created, each marked
; by the installed.json the app writes when a download completes, plus
; abandoned staging folders. Anything else found there is left alone, and the
; folder itself is only removed if it ends up empty.
;
; Two more things are deliberately left behind: speech models, which live in
; the shared Hugging Face cache other tools also use, and the WebView2 runtime,
; which belongs to Windows and to every other app that renders web content.
;
; Updates run the old uninstaller in update mode, which must keep all of it.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    StrCpy $R7 "$LOCALAPPDATA\Prompt Maxxer\gpu-runtime"
    FindFirst $R8 $R9 "$R7\*"
    pm_gpu_loop:
      StrCmp $R9 "" pm_gpu_done
      StrCmp $R9 "." pm_gpu_next
      StrCmp $R9 ".." pm_gpu_next
      ; A completed download, marked by the app.
      ${If} ${FileExists} "$R7\$R9\installed.json"
        DetailPrint "Removing GPU libraries downloaded by ${PRODUCTNAME}: $R9"
        RMDir /r "$R7\$R9"
      ${Else}
        ; Or an interrupted one: "<version tag>.staging".
        StrCpy $R6 $R9 8 -8
        ${If} $R6 == ".staging"
          RMDir /r "$R7\$R9"
        ${EndIf}
      ${EndIf}
    pm_gpu_next:
      FindNext $R8 $R9
      Goto pm_gpu_loop
    pm_gpu_done:
    FindClose $R8
    RMDir "$R7"

    ; An update this copy downloaded and never got to install.
    Delete "$LOCALAPPDATA\Prompt Maxxer\updates\staged.json"
    Delete "$LOCALAPPDATA\Prompt Maxxer\updates\Prompt-Maxxer_*_x64-setup.exe"
    Delete "$LOCALAPPDATA\Prompt Maxxer\updates\Prompt-Maxxer_*_x64-setup.exe.part"
    RMDir "$LOCALAPPDATA\Prompt Maxxer\updates"
    RMDir "$LOCALAPPDATA\Prompt Maxxer"

    ; "Delete the application data" also clears settings and logs.
    ${If} $DeleteAppDataCheckboxState = 1
      RMDir /r "$APPDATA\Prompt Maxxer"
    ${EndIf}
  ${EndIf}
!macroend
