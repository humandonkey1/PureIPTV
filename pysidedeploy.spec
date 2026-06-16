[app]
# Название в меню телефона
title = PureIPTV Premium

# Уникальный ID пакета
package_name = org.pure.iptv.premium

# Версия
version = 1.0.0

# Главный файл запуска
entrypoint = main.py

# Файлы, которые нужно включить в APK
include_files = main.py,main.qml,premium_vault.db

# Зависимости Python
python_depends = requests,urllib3,idna,charset-normalizer,certifi,python-mpv

# Платформа и архитектура
target = android
arch = arm64-v8a

[android]
# Разрешения (Интернет обязателен для IPTV)
permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE

# Иконка (если будет файл icon.png в папке)
# icon = icon.png

# Тема (полноэкранный режим без полосок)
theme = @android:style/Theme.NoTitleBar.Fullscreen

# Дополнительные системные библиотеки (здесь можно указать путь к libmpv.so если он есть)
# extra_libs = libmpv.so
