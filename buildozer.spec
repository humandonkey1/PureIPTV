[app]
title = Pure IPTV Premium
package.name = pureiptv
package.domain = org.humandonkey.pureiptv
source.dir = .
source.include_exts = py,qml,png,db
version = 2.0.0

# Самые важные зависимости для твоего кода!
requirements = python3,pyside6,requests,python-mpv

orientation = portrait
fullscreen = 1
android.archs = arm64-v8a, armeabi-v7a

# Чистые доступы (Play Protect не придерется!)
android.permissions = INTERNET, ACCESS_NETWORK_STATE
