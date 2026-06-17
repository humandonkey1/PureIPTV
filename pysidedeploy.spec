[app]
title = PureIPTV
package_name = org.pure.iptv.premium
version = 1.0.0
entrypoint = main.py
input_file = main.py
include_files = main.py,main.qml,libmpv.so

[python]
# Критически важно для исправления ошибки split()
android_packages = requests,urllib3,idna,charset-normalizer,certifi,python-mpv
python_depends = requests,urllib3,idna,charset-normalizer,certifi,python-mpv

[qt]
# Принудительно включаем модули для линтера
modules = Core,Gui,Qml,Quick,Layouts,Multimedia

[android]
# ВОТ ОНО! Исправляет TypeError: ... not NoneType
mode = release
name = PureIPTV
permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
theme = @android:style/Theme.NoTitleBar.Fullscreen
arch = arm64-v8a
extra_libs = libmpv.so
