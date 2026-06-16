[app]
title = PureIPTV Premium
package_name = org.pure.iptv.premium
version = 1.0.0
entrypoint = main.py
input_file = main.py
# Включаем нативную библиотеку
include_files = libmpv.so,main.py,main.qml

[python]
# Указываем зависимости
python_depends = requests,urllib3,idna,charset-normalizer,certifi,python-mpv
android_packages = requests,urllib3,idna,charset-normalizer,certifi,python-mpv

[qt]
extra_modules = QtGui,QtQml,QtCore,QtQuick,QtLayouts

[android]
name = PureIPTV
permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
theme = @android:style/Theme.NoTitleBar.Fullscreen
arch = arm64-v8a
# Просим положить либу в папку с нативным кодом
extra_libs = libmpv.so
