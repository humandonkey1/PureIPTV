[app]
# Название, которое будет отображаться на экране смартфона
title = PureIPTV Premium

# Имя и домен пакета (Android склеит их в org.humandonkey.pureiptv)
package.name = pureiptv
package.domain = org.humandonkey

# Исходный код лежит в текущей папке
source.dir = .
source.include_exts = py,qml,png,db
version = 2.0.0

# Все необходимые библиотеки для работы твоего Premium-плеера
requirements = python3,pyside6,requests,python-mpv,sqlite3

# Настройки экрана смартфона
orientation = portrait
fullscreen = 1
android.archs = arm64-v8a, armeabi-v7a

# Минимальный набор чистых разрешений (Гугл протекторы будут молчать!)
android.permissions = INTERNET, ACCESS_NETWORK_STATE

# Говорим Бульдозеру соглашаться со всеми лицензиями Android SDK автоматически
android.accept_sdk_license = True
