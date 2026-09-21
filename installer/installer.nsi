; FRPPanelGUI NSIS 安装脚本
; 输出: FRPPanelGUI-Setup.exe (HKCU 安装, 无需管理员权限)

Unicode True
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APP_NAME "FRP Panel GUI"
!define APP_EXE "FRPPanelGUI.exe"
!define APP_VERSION "1.0.0"
!define APP_REGKEY "FRPPanelGUI"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "FRPPanelGUI-Setup.exe"
InstallDir "$LOCALAPPDATA\FRPPanelGUI"
InstallDirRegKey HKCU "Software\${APP_REGKEY}" "InstallDir"
RequestExecutionLevel user

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "frp-panel 客户端图形管理器"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "MIT License"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION "LaunchApp"
!define MUI_FINISHPAGE_LINK "项目主页: https://github.com/xRetia/frp-panel-gui"
!define MUI_FINISHPAGE_LINK_LOCATION "https://github.com/xRetia/frp-panel-gui"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SetOutPath "$INSTDIR"

    ; 主程序
    File "..\dist\FRPPanelGUI.exe"
    File /nonfatal "..\dist\frp.ico"

    ; 开始菜单
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"

    ; 桌面快捷方式
    CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

    ; 卸载信息(HKCU, 无需管理员)
    !define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_REGKEY}"
    WriteRegStr HKCU "Software\${APP_REGKEY}" "InstallDir" $INSTDIR
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "xRetia"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"

    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" $0

    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Function LaunchApp
    Exec '"$INSTDIR\${APP_EXE}"'
FunctionEnd

Section "Uninstall"
    ; 停止运行中的程序(含托盘)
    nsExec::Exec 'taskkill /IM "${APP_EXE}" /F'
    Pop $0
    Sleep 500

    Delete "$INSTDIR\${APP_EXE}"
    Delete "$INSTDIR\frp.ico"

    ; 清理自启注册表项(与程序使用的值名一致)
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_REGKEY}"

    Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
    RMDir "$SMPROGRAMS\${APP_NAME}"
    Delete "$DESKTOP\${APP_NAME}.lnk"

    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_REGKEY}"
    DeleteRegKey HKCU "Software\${APP_REGKEY}"

    ; 用户数据(config.json/客户端exe/日志)保留, 由用户自行决定是否删除
    IfFileExists "$INSTDIR\config.json" keep
    IfFileExists "$INSTDIR\frp-panel-client.exe" keep
    RMDir "$INSTDIR"
    keep:
SectionEnd
