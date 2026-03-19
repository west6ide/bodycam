# Deploy On New Laptop

Эта инструкция для нового ноутбука Windows, где нет Python, нет среды разработки и нужно просто запустить готовое приложение с автозапуском.

## Что подготовить заранее

На текущем рабочем ноутбуке или компьютере у вас уже должен быть собранный `exe`.

Нужные файлы:

- `dist\BodycamUploader\BodycamUploader.exe`
- вся папка `dist\BodycamUploader`
- `install_autostart.ps1`
- `config.example.json`

Важно: переносить нужно не только `BodycamUploader.exe`, а всю папку `dist\BodycamUploader`, потому что PyInstaller кладет рядом внутренние зависимости.

## Что установить на новом ноутбуке

Python не нужен.

Нужно только:

- Windows
- PowerShell
- доступ к сети до вашего backend

Обычно этого уже достаточно.

## Шаг 1. Скопировать папку приложения

На новом ноутбуке создайте, например, такую папку:

```powershell
C:\BodycamUploader
```

Скопируйте туда:

- папку `BodycamUploader` из `dist`
- файл `install_autostart.ps1`
- файл `config.example.json`

В итоге должно получиться примерно так:

```text
C:\BodycamUploader\
  install_autostart.ps1
  config.example.json
  BodycamUploader\
    BodycamUploader.exe
    _internal\
    ...
```

## Шаг 2. Запустить приложение первый раз вручную

Откройте PowerShell в папке:

```powershell
cd C:\BodycamUploader\BodycamUploader
.\BodycamUploader.exe
```

После первого запуска приложение создаст рабочую папку в профиле пользователя:

```text
%USERPROFILE%\BodycamUploader
```

Там появится файл:

```text
%USERPROFILE%\BodycamUploader\config.json
```

## Шаг 3. Настроить config.json

Откройте файл:

```text
%USERPROFILE%\BodycamUploader\config.json
```

Заполните минимум эти поля:

- `server_url`
- `api_token`
- `camera_label_keywords`
- `camera_folder_names`
- `microphone_device_name`
- `language_hint`
- `output_language`

Если хотите, можно сначала взять содержимое из `config.example.json` и перенести нужные значения в `config.json`.

## Шаг 4. Проверить ручной запуск

Снова запустите:

```powershell
cd C:\BodycamUploader\BodycamUploader
.\BodycamUploader.exe
```

Проверьте:

- приложение открывается без ошибки
- при подключении камеры оно видит устройство
- находятся аудиофайлы
- создается отправка

Если это не работает вручную, автозапуск тоже не поможет. Сначала нужно добиться нормального ручного запуска.

## Шаг 5. Настроить автозапуск при входе в Windows

Перейдите в папку, где лежит `install_autostart.ps1`:

```powershell
cd C:\BodycamUploader
```

Запустите:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -AppDir "C:\BodycamUploader"
```

Почему `-AppDir "C:\BodycamUploader"`:

- скрипт ожидает, что внутри `AppDir` будет путь `dist\BodycamUploader\BodycamUploader.exe`
- если вы разложили файлы иначе, путь нужно либо подстроить, либо поправить сам скрипт

## Важный момент по структуре папок

Текущий `install_autostart.ps1` ищет `exe` здесь:

```text
<AppDir>\dist\BodycamUploader\BodycamUploader.exe
```

Значит для использования скрипта без изменений удобнее разложить файлы так:

```text
C:\BodycamUploader\
  install_autostart.ps1
  dist\
    BodycamUploader\
      BodycamUploader.exe
      _internal\
      ...
```

Это самый простой вариант.

Тогда команды будут такие:

```powershell
cd C:\BodycamUploader
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -AppDir "C:\BodycamUploader"
```

## Рекомендуемая итоговая структура

Лучше использовать именно такую структуру:

```text
C:\BodycamUploader\
  install_autostart.ps1
  config.example.json
  dist\
    BodycamUploader\
      BodycamUploader.exe
      _internal\
      ...
```

## Шаг 6. Проверить, что задача создана

В PowerShell:

```powershell
Get-ScheduledTask -TaskName BodycamUploader
```

Если задача есть, автозапуск зарегистрирован.

## Шаг 7. Проверить реальный автозапуск

Автозапуск срабатывает при входе пользователя в Windows, а не в момент подключения камеры.

Проверка:

1. Выйдите из Windows аккаунта.
2. Зайдите снова.
3. Откройте PowerShell.
4. Выполните:

```powershell
Get-Process BodycamUploader -ErrorAction SilentlyContinue
```

Если процесс найден, приложение стартовало автоматически.

## Что происходит дальше

После автозапуска приложение само работает в фоне и периодически проверяет подключение камеры.

То есть схема такая:

1. Включили ноутбук.
2. Вошли в Windows.
3. Приложение стартовало автоматически.
4. Потом вы подключаете камеру.
5. Приложение ее обнаруживает и начинает обработку.

Автозапуск не означает запуск "по событию подключения камеры". Он означает запуск при логине пользователя.

## Если приложение не стартует после включения ноутбука

Проверьте по порядку:

1. Задача существует:

```powershell
Get-ScheduledTask -TaskName BodycamUploader
```

2. Процесс запущен:

```powershell
Get-Process BodycamUploader -ErrorAction SilentlyContinue
```

3. `exe` реально существует по ожидаемому пути:

```powershell
Test-Path C:\BodycamUploader\dist\BodycamUploader\BodycamUploader.exe
```

4. Приложение запускается вручную:

```powershell
Start-Process "C:\BodycamUploader\dist\BodycamUploader\BodycamUploader.exe"
```

## Если надо удалить автозапуск

```powershell
schtasks /Delete /TN BodycamUploader /F
```

## Если надо заменить приложение новой версией

Порядок такой:

1. Остановить приложение:

```powershell
Get-Process BodycamUploader -ErrorAction SilentlyContinue | Stop-Process -Force
```

2. Заменить папку:

```text
C:\BodycamUploader\dist\BodycamUploader
```

3. Запустить вручную один раз для проверки:

```powershell
Start-Process "C:\BodycamUploader\dist\BodycamUploader\BodycamUploader.exe"
```

Автозапуск заново создавать не нужно, если путь к `exe` не менялся.

## Короткий вариант без лишнего

Если совсем коротко, то на новом ноутбуке:

1. Скопировать папку `dist\BodycamUploader` в `C:\BodycamUploader\dist\BodycamUploader`
2. Скопировать `install_autostart.ps1` в `C:\BodycamUploader`
3. Запустить `C:\BodycamUploader\dist\BodycamUploader\BodycamUploader.exe`
4. Настроить `%USERPROFILE%\BodycamUploader\config.json`
5. Выполнить:

```powershell
powershell -ExecutionPolicy Bypass -File C:\BodycamUploader\install_autostart.ps1 -AppDir "C:\BodycamUploader"
```

6. Перелогиниться в Windows и проверить, что процесс `BodycamUploader` появился

