[app]
# Название на экране телефона и твой легендарный пакет-ослик!
title = PureIPTV Premium
package_name = pureiptv
package_domain = org.humandonkey

# Главный файл входа
entrypoint = main.py

# Зависимости твоего премиум бэкенда
modules = requests,python-mpv

[buildozer]
# Говорим Бульдозеру компилировать именно под мобильный Android
mode = apk
arch = arm64-v8a
