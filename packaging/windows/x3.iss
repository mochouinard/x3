; Inno Setup script for the Windows installer of x3.
;
;   pyinstaller --noconfirm packaging\pyinstaller\x3.spec
;   iscc /DAppVersion=0.1.0 packaging\windows\x3.iss
;
; Output: dist\x3-<version>-win64-setup.exe
;
; Per-user: installs into %LOCALAPPDATA%\Programs\x3 with no administrator
; rights, so x3 can update itself (x3/ui/updates.py runs this setup with
; /SILENT /RELAUNCH) on a PC where the user is not an administrator. The
; license is shown in English, or in French on a French-language Windows.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=x.y.z
#endif

[Setup]
AppId={{6F1B7E0C-3A52-4E7B-9C1D-2B8F4A7D5E31}
AppName=x3
AppVersion={#AppVersion}
AppVerName=x3 {#AppVersion}
AppPublisher=Marc O. Chouinard
AppPublisherURL=https://github.com/mochouinard/x3
AppSupportURL=https://github.com/mochouinard/x3/issues
DefaultDirName={localappdata}\Programs\x3
DisableDirPage=yes
DefaultGroupName=x3
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=x3-{#AppVersion}-win64-setup
SetupIconFile=..\..\x3\ui\resources\appicon.ico
UninstallDisplayIcon={app}\x3.exe
UninstallDisplayName=x3
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; An update replaces a running x3: close it first, and do not start the old
; one again afterwards -- [Run] starts the new one.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"; LicenseFile: "..\..\LICENSE"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"; LicenseFile: "..\..\LICENSE.fr.md"

[InstallDelete]
; A new version brings its own Qt and Python: clear the old bundle so no stale
; library outlives the version it belonged to. Settings live elsewhere
; (%APPDATA%\x3, %LOCALAPPDATA%\x3) and are kept.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\x3\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\x3"; Filename: "{app}\x3.exe"

[Run]
; Interactive install: offer to start x3 at the end.
Filename: "{app}\x3.exe"; Description: "{cm:LaunchProgram,x3}"; Flags: nowait postinstall skipifsilent
; Silent update from inside x3: start it again.
Filename: "{app}\x3.exe"; Flags: nowait; Check: Relaunch

[UninstallRun]
; CloseApplications is for setup only: uninstalled with x3 open, x3.exe and
; its libraries stayed behind, locked. Close it first -- and x3ctl, which an
; assistant's MCP client may be running. Runs before any file is deleted.
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im x3.exe"; Flags: runhidden waituntilterminated; RunOnceId: "CloseX3"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im x3ctl.exe"; Flags: runhidden waituntilterminated; RunOnceId: "CloseX3ctl"

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (Pos('/RELAUNCH', UpperCase(GetCmdTail)) > 0);
end;
