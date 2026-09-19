# Что пошло не так и как поправить

## Коротко

1. `git remote add origin` отказался: origin уже был и указывал на
   `kam1k88/DARAVE`. Поэтому `git push` уехал **в приватный репозиторий с
   исходниками** — это, кстати, правильное для него место, всё в порядке.
2. `gh release create --repo kam1k88/darave-releases` упал с
   `Invalid target_commitish`, потому что **репозиторий пустой**: в нём нет
   ни одного коммита, значит нет и ветки, на которую можно повесить тег.
3. Дальше команда без `--repo` создала релиз **в приватном DARAVE**. Файл
   лежит там, и скачать его никто, кроме вас, не сможет.

Секреты проверил: `darave_secrets.json` и `_config.php` **не попали ни в
индекс, ни в историю**. Аудио, база и логи тоже не отслеживаются.

## Шаг 1. Оживить пустой репозиторий

```powershell
cd C:\Users\kam1k88
git clone https://github.com/kam1k88/darave-releases.git
cd darave-releases
# положить сюда README.md (приложен)
git add README.md
git commit -m "README"
git push
```

## Шаг 2. Выложить релиз туда, где надо

Имя файла **без номера версии** — тогда ссылка `latest/download/...`
никогда не протухнет и сайт не придётся править при каждом выпуске.

```powershell
cd C:\Users\kam1k88\darave-releases
Copy-Item C:\Users\kam1k88\DARAVE\installer\DARAVE-1.0.0-setup.exe .\DARAVE-setup.exe

gh release create v1.0.0 .\DARAVE-setup.exe `
  --repo kam1k88/darave-releases `
  --title "DARAVE 1.0.0" `
  --notes "Первая публичная сборка.

SHA-256: 5439f95504623372d5d1ff011d7809c1abfb072ea07c97242e03fc00be717676

Windows покажет предупреждение SmartScreen — подписи пока нет.
Нажмите «Подробнее» -> «Выполнить в любом случае»."
```

## Шаг 3. Убрать релиз из приватного репозитория

Там он бесполезен, а 173 МБ в репозитории с исходниками — мусор.

```powershell
gh release delete v1.0.0 --repo kam1k88/DARAVE --cleanup-tag --yes
```

## Шаг 4. Ссылка для сайта

```
https://github.com/kam1k88/darave-releases/releases/latest/download/DARAVE-setup.exe
```

Проверить, что отдаётся:

```powershell
curl.exe -IL "https://github.com/kam1k88/darave-releases/releases/latest/download/DARAVE-setup.exe"
```

Ждём в конце `200` и `content-length: 181372500`.

## На будущее

Проверяйте, куда указывает origin, прежде чем пушить:

```powershell
git remote -v
```

А `gh release create` **всегда с `--repo`** — без него он берёт репозиторий
из текущей папки, и это не всегда тот, который вы имели в виду.
