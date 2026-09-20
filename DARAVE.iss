; DARAVE.iss — установщик. Собирается через build.ps1, руками запускать не нужно.
;
; Что здесь важно и неочевидно:
;
;   PrivilegesRequired=lowest — ставим в папку пользователя, а не в Program
;   Files. Диджею не нужно просить админа, а программе не нужно UAC при
;   каждом обновлении. Данные всё равно живут в %LOCALAPPDATA%\DARAVE.
;
;   Порт 8765 открывается только на 127.0.0.1, поэтому никаких правил
;   брандмауэра не заводим: Windows не спросит.
;
;   При удалении база, библиотека и наборы НЕ удаляются. Человек сносит
;   программу, а не полгода своей работы; папку данных он сотрёт сам, если
;   захочет — путь показан в конце.

#define MyAppName "DARAVE"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.1"
#endif
#define MyAppPublisher "DARAVE"
#define MyAppURL "https://darave.ru"
#define MyAppExeName "DARAVE.exe"

[Setup]
AppId={{8E6B1C42-7A31-4C0E-9D55-DA5AVE000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer
OutputBaseFilename=DARAVE-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; Сборка на 200+ МБ жмётся минуты — но скачивать её потом будут тысячи раз.
LZMANumBlockThreads=4

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Вся папка сборки целиком: exe, _internal и ffmpeg рядом.
Source: "dist\DARAVE\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Только то, что положил установщик. Папку данных не трогаем намеренно.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\ffmpeg"

[Code]
// Программа слушает 127.0.0.1:8765. Если на нём уже кто-то сидит — почти
// всегда это сам DARAVE, запущенный до установки. Обновление поверх
// работающей копии даёт "файл занят" в середине распаковки, а выглядит
// это как сломанный установщик. Проверяем и просим закрыть.
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec(ExpandConstant('{cmd}'), '/C netstat -ano -p TCP | findstr ":8765 " > nul',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
    begin
      if MsgBox('Похоже, DARAVE сейчас запущен (занят порт 8765).' + #13#10 +
                'Закройте окно программы и нажмите «Повторить».' + #13#10#13#10 +
                'Продолжить установку всё равно?',
                mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Куда программа складывает свои данные. Спрашивают об этом всегда:
    // где база, куда делись наборы, что копировать при переезде.
    Log('Данные: ' + ExpandConstant('{localappdata}') + '\DARAVE');
  end;
end;
