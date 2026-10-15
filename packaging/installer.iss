; Inno Setup script for Folio PDF – builds FolioPDF-Setup-<version>.exe
;
; Build (after "pyinstaller packaging\folio.spec"):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 packaging\installer.iss
; packaging\build_windows.bat does both steps.
;
; The installer and the installed program never use the network.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Folio PDF"
#define AppExe "FolioPDF.exe"
#define AppId "{{6F4E2A61-3C1B-4B7E-9D2F-5A8C0E1F7B34}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Folio
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} installation
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Install for the current user without admin rights, or for all users (asks).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=FolioPDF-Setup-{#AppVersion}
SetupIconFile=..\assets\folio.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "danish"; MessagesFile: "compiler:Languages\Danish.isl"

[CustomMessages]
danish.AssocPdf=Brug {#AppName} til at åbne PDF-filer
danish.FirewallBlock=Bloker {#AppName} i Windows Firewall (ekstra sikkerhed – programmet bruger aldrig nettet)
danish.Extra=Ekstra:
danish.LaunchApp=Start {#AppName}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "assocpdf"; Description: "{cm:AssocPdf}"; GroupDescription: "{cm:Extra}"; Flags: unchecked
Name: "firewall"; Description: "{cm:FirewallBlock}"; GroupDescription: "{cm:Extra}"; Flags: unchecked; Check: IsAdminInstallMode

[Files]
Source: "..\dist\FolioPDF\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; "Open with" registration (always) – lets Windows list Folio PDF for PDFs.
Root: HKA; Subkey: "Software\Classes\FolioPDF.Document"; ValueType: string; ValueName: ""; ValueData: "PDF-dokument"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\FolioPDF.Document\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"
Root: HKA; Subkey: "Software\Classes\FolioPDF.Document\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "FolioPDF.Document"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey
; Default program for .pdf (only when the user ticks the task; Windows 10/11
; may still ask the user to confirm the choice once).
Root: HKA; Subkey: "Software\Classes\.pdf"; ValueType: string; ValueName: ""; ValueData: "FolioPDF.Document"; Tasks: assocpdf; Flags: uninsdeletevalue

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName} - block out"" dir=out action=block program=""{app}\{#AppExe}"" enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName} - block in"" dir=in action=block program=""{app}\{#AppExe}"" enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName} - block out"""; Flags: runhidden; RunOnceId: "fwout"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName} - block in"""; Flags: runhidden; RunOnceId: "fwin"
