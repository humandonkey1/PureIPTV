[app]
# Название приложения
title = PureIPTV Premium

# Уникальный ID
package_name = org.pure.iptv.premium

# Версия
version = 1.0.0

# Главный файл
entrypoint = main.py

# Какие файлы включить (только те, что РЕАЛЬНО лежат в репозитории)
include_files = main.py,main.qml

# Зависимости
python_depends = requests,urllib3,idna,charset-normalizer,certifi,python-mpv

[android]
# Разрешения
permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE

# Тема
theme = @android:style/Theme.NoTitleBar.Fullscreen

# Архитектура
arch = arm64-v8a
