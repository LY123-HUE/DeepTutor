; EduBuddy — click-to-install package (Inno Setup 7)
; Compile:  "C:\Program Files\Inno Setup 7\ISCC.exe" installer.iss
; Produces: dist\EduBuddySetup.exe  (installer, uninstaller, shortcuts)
;
; The setup ships:
;   EduBuddyDesktop.exe   — the native shell (windowed, no console)
;   runtime\               — offline deeptutor runtime, installed as a plain
;                            tree into {app}\runtime (embeddable python +
;                            deeptutor + portable node). No python/node/pip
;                            needed on the target machine; the shell resolves
;                            {app}\runtime automatically.
;   assets\icon.ico        — shortcuts + uninstaller icon
;
; End users double-click EduBuddySetup.exe -> Next/Next/Install -> the app
; launches on its own. Uninstall keeps ~\EduBuddy workspace (learning data).

#define MyAppName "EduBuddy"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "HKUDS"
#define MyAppExeName "EduBuddyDesktop.exe"

[Setup]
AppId={{8C3A8E6A-5D44-4A67-BD6E-7A1F3C4B0E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\EduBuddy
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=EduBuddySetup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; native shell + assets
Source: "..\dist\EduBuddyDesktop.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.ico";            DestDir: "{app}\assets"; Flags: ignoreversion
Source: "..\assets\icon.png";            DestDir: "{app}\assets"; Flags: ignoreversion
; offline runtime tree (embeddable python + deeptutor + portable node)
; NOTE: compiled from runtime-build\staging so the installed layout matches the
; portable package exactly ({app}\runtime\python\...,  {app}\runtime\node\...)
Source: "..\runtime-build\staging\*";    DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; keep user data (workspace lives in %USERPROFILE%\EduBuddy) — the
; uninstaller only removes the app dir (incl. the installed runtime copy).
Type: filesandordirs; Name: "{app}"
