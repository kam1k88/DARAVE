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

; Закрытие работающей копии.
;
; По умолчанию Inno закрывает мешающие программы через Restart Manager:
; посылает приложению просьбу завершиться и ЖДЁТ ответа. DARAVE — это
; uvicorn без оконного цикла сообщений, отвечать на такую просьбу ему
; нечем. Диджей соглашался закрыть — и установщик вис насмерть, пока
; процесс не снимут руками.
;
; force означает: попросили вежливо, не ответил за отведённое время —
; закрыли принудительно. Для нас это безопасно: своё состояние программа
; держит в SQLite с WAL, а не в памяти до выхода.
CloseApplications=force
; Обратно её поднимать не надо: после обновления запуск предлагается
; галочкой на последней странице, и поднятая Restart Manager'ом СТАРАЯ
; копия дала бы второй процесс на том же порту.
RestartApplications=no
; Какие именно файлы считать «занятыми». Без фильтра Inno опрашивает всю
; папку назначения, а там 200 МБ библиотек.
CloseApplicationsFilter=*.exe

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
  if not Exec(ExpandConstant('{cmd}'), '/C netstat -ano -p TCP | findstr ":8765 " > nul',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Exit;
  if ResultCode <> 0 then
    Exit;                                  // порт свободен, спрашивать не о чем

  // Раньше здесь был вопрос «закройте и нажмите Повторить», а закрывать
  // предлагалось руками. Теперь закрываем сами — иначе человек жмёт
  // «да», Restart Manager ждёт ответа от процесса без окна, и всё висит.
  if MsgBox('DARAVE сейчас запущен (занят порт 8765).' + #13#10#13#10 +
            'Закрыть её и продолжить установку?' + #13#10 +
            'Несохранённого в ней ничего нет: библиотека, пул и наборы ' +
            'лежат в базе и переживут закрытие.',
            mbConfirmation, MB_YESNO) = IDNO then
  begin
    Result := False;
    Exit;
  end;

  // /T — вместе с дочерними процессами: скан, перемер темпа и разделение
  // на слои запускаются тем же DARAVE.exe с ключом --child, и без /T они
  // переживут родителя и продолжат держать файлы.
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /T /IM DARAVE.exe > nul 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Порт освобождается не мгновенно: сокет успевает побыть в TIME_WAIT,
  // а файлы — подержаться открытыми ещё мгновение после смерти процесса.
  Sleep(1500);
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
