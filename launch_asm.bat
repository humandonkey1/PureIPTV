@echo off
TITLE ASM-LEVEL OPTIMIZER (STRICT DLL MODE)

:: 1. Указываем DLL-ке где искать её 'мозги'
set MPV_HOME=%~dp0mpv_config

:: 2. Qt Quick (Интерфейс): Переходим на Ассемблерный минимализм
:: Убираем многопоточный рендеринг интерфейса (огромная экономия CPU)
set QSG_RENDER_LOOP=basic
:: Снижаем точность расчетов графики (незаметно для глаза, легче для GPU)
set QSG_LOW_PRECISION_FLOAT=1
:: Принудительный DX11 без лишних прослоек
set QSG_RHI_BACKEND=d3d11
:: Запрещаем Qt использовать дискретную графику, если есть встроенная (экономия энергии)
set QSG_RHI_PREFER_LOW_POWER_GPU=1

:: 3. Оптимизация Python (чистый байт-код, без мусора)
set PYTHONOPTIMIZE=2
set PYTHONDONTWRITEBYTECODE=1

:: 4. Системные флаги для FFmpeg (внутри MPV)
:: Отключаем многопоточность там, где она только плодит задержки
set FF_THREAD_TYPE=slice

:: 5. Запуск плеера в "тихом" режиме
:: pythonw запускает процесс без черного окна консоли (минус еще немного нагрузки)
start /low pythonw main.py

exit
