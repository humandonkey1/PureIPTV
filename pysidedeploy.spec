[app]
title = PureIPTV Premium
package_name = org.pure.iptv.premium
version = 1.0.0
entrypoint = main.py
input_file = main.py
# Включаем все файлы, включая либу mpv
include_files = main.py,main.qml,libmpv.so

[python]
# Зависимости для Android (python-mpv требует libmpv.so)
android_packages = requests,urllib3,idna,charset-normalizer,certifi,python-mpv
python_depends = requests,urllib3,idna,charset-normalizer,certifi,python-mpv

[qt]
# Принудительно подключаем модули
modules = Core,Gui,Qml,Quick,Layouts,Multimedia

[android]
# КРИТИЧНО: mode должен быть здесь для исправления TypeError
mode = release
name = PureIPTV
permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
theme = @android:style/Theme.NoTitleBar.Fullscreen
arch = arm64-v8a
# Указываем путь к нашей либе
extra_libs = libmpv.so
