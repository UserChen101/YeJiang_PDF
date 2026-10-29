; 页匠 PageSmith 安装包脚本（Inno Setup 6）
;
; 构建：
;   1. 先用 PyInstaller 打出 dist\PageSmith
;   2. 运行 ISCC.exe installer\PageSmith.iss
; 产物：dist\PageSmithSetup-<版本>.exe
;
; 说明
; ----
; * 显示名用中文「页匠」，安装目录与 exe 一律用 ASCII 的 PageSmith ——
;   中文路径在部分解压工具、批处理、以及某些安全软件的规则里会出问题。
; * PrivilegesRequiredOverridesAllowed：允许安装时选择"为所有用户/仅当前用户"，
;   非管理员也能安装到 %LOCALAPPDATA%\Programs\PageSmith。
; * AppId 固定不变 —— 换版本升级覆盖安装时靠它识别旧版本。
; * 文件存为 UTF-8 with BOM：Inno Setup 6 靠 BOM 判断编码，没有 BOM 时
;   中文会被按 ANSI 解读而变成乱码。

#define MyAppName "页匠"
#define MyAppNameEn "PageSmith"
#define MyAppVersion "1.0.4"
#define MyAppPublisher "页匠"
#define MyAppExeName "PageSmith.exe"

[Setup]
AppId={{5E6E9006-B945-4A5A-B023-E9ADE7F011D8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppNameEn} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 默认「仅当前用户」安装：不弹 UAC，装到 %LOCALAPPDATA%\Programs\PageSmith；
; 需要装给全机器时可在命令行加 /ALLUSERS 走管理员模式。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
OutputDir=..\dist
OutputBaseFilename=PageSmithSetup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppNameEn}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; WebView2 运行时在 Win11 自带，Win10 可能缺，交给用户按提示装，不做静默部署
MinVersion=10.0
LicenseFile=license.rtf

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "pdfassoc"; Description: "把页匠加入「默认 PDF 打开工具」候选列表"; GroupDescription: "PDF 文件关联："; Flags: unchecked

[Files]
Source: "..\dist\PageSmith\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; 注册文件关联候选（全部 HKCU，无需管理员）：
; * ProgId "PageSmith.pdf" 定义"用页匠打开 PDF"；
; * Capabilities + RegisteredApplications 让页匠出现在 设置→默认应用 的应用列表；
; * Windows 对 UserChoice 有哈希保护，安装器不能也不应静默抢默认 ——
;   用户装完在 设置→默认应用 里点一下即可，或直接在软件「更多」菜单里设。
[Registry]
Root: HKCU; Subkey: "Software\Classes\PageSmith.pdf"; ValueType: string; ValueName: ""; ValueData: "页匠 PageSmith PDF 文档"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\Classes\PageSmith.pdf"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "页匠 PageSmith PDF 文档"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\Classes\PageSmith.pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\Classes\PageSmith.pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\PageSmith\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "页匠 PageSmith"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\PageSmith\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "轻量级 PDF 全能工具箱：阅读批注、文字编辑、格式转换、OCR、水印、压缩加密。"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\PageSmith\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "PageSmith.pdf"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "PageSmith"; ValueData: "Software\PageSmith\Capabilities"; Flags: uninsdeletevalue; Tasks: pdfassoc

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清掉 WebView2 缓存目录（日志保留在 .pagesmith\logs，便于回溯问题）
Type: filesandordirs; Name: "{%USERPROFILE}\.pagesmith\webview"
