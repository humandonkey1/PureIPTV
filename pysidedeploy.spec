[app]
title = PureIPTV Premium
package_name = org.pure.iptv.premium
version = 1.0.0
entrypoint = main.py
input_file = main.py
include_files = main.py,main.qml

[python]
# Новая версия сборщика ищет зависимости здесь
python_depends = requests,urllib3,idna,charset-normalizer,certifi,python-mpv
android_packages = requests,urllib3,idna,charset-normalizer,certifi,python-mpv

[android]
permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
theme = @android:style/Theme.NoTitleBar.Fullscreen
arch = arm64-v8a
