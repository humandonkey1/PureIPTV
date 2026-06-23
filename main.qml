import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs as QtDialogs

ApplicationWindow {
    id: window
    width: 1200
    height: 800
    visible: true
    title: "Pure IPTV Player"

    Material.theme: Material.Dark
    Material.accent: "#00E676" // Неоновый зеленый акцент

    property string murlPath: ""
    property var selCh: null
    property string activeCategory: "Все каналы"
    property string searchQuery: ""
    property var currentFilteredList: [] // Храним текущий отфильтрованный список для Next/Prev кнопок
    property int currentChIndex: -1
    property string currentAspect: "no" // no, 16:9, 4:3, stretch
    property string targetVpnCountry: "Глобальный" // Название страны для Smart VPN диалога предупреждения

    // =======================================================
    // АДАПТИВНАЯ КЛИЕНТСКАЯ ВЕРСТКА ПОД ВСЕ КЛАССЫ УСТРОЙСТВ
    // (ТВ, Смартфон, ПК, Планшет)
    // =======================================================
    property string deviceType: {
        var isMobile = (Qt.platform.os === "android" || Qt.platform.os === "ios")
        var diagonal = Math.sqrt(Screen.width * Screen.width + Screen.height * Screen.height) / Screen.pixelDensity / 25.4
        
        // 1. Умное определение Смарт ТВ / ТВ-приставки
        if (isMobile && (diagonal > 20 || Screen.width >= 1920 && Screen.height >= 1080 && !Screen.hasTouchScreen)) {
            return "TV"
        }
        
        // 2. Планшеты и Смартфоны
        if (isMobile) {
            return diagonal >= 7.0 ? "Tablet" : "Phone"
        }
        
        // 3. Персональный компьютер (по умолчанию)
        return "PC"
    }

    // Ручное форсирование режима (F1 - Смартфон, F2 - Планшет, F3 - ПК, F4 - ТВ) для отладки
    property string forcedDeviceType: ""
    readonly property string currentDevice: forcedDeviceType !== "" ? forcedDeviceType : deviceType

    // Коэффициент масштабирования элементов
    readonly property real scaleFactor: {
        if (currentDevice === "TV") return 1.45
        if (currentDevice === "Tablet") return 1.2
        if (currentDevice === "Phone") return 0.95
        return 1.0 // PC
    }

    // Адаптивные шрифты
    readonly property int fontSizeHeader: Math.round(22 * scaleFactor)
    readonly property int fontSizeTitle: Math.round(16 * scaleFactor)
    readonly property int fontSizeBody: Math.round(14 * scaleFactor)
    readonly property int fontSizeSub: Math.round(12 * scaleFactor)
    
    // Адаптивная видимость панелей на основном экране
    readonly property bool showCategoriesSidebar: currentDevice === "PC" || currentDevice === "Tablet" || currentDevice === "TV"
    readonly property bool showEpgSidebar: currentDevice === "PC" || (currentDevice === "TV" && width > 1200)

    // Динамический размер иконки канала под разные типы устройств (F1-F4)
    readonly property int channelIconSize: {
        if (currentDevice === "TV") return 80
        if (currentDevice === "Tablet") return 55
        if (currentDevice === "Phone") return 40
        return 60 // PC
    }

    // Автоматический запуск в максимизированном режиме (Maximize) для ПК/Планшетов, и во весь экран (FullScreen) на ТВ!
    visibility: currentDevice === "TV" ? Window.FullScreen : Window.Maximized

    color: "#0A0B10"

    // Горячие клавиши отладки адаптивности
    Shortcut { sequence: "F1"; onActivated: window.forcedDeviceType = "Phone" }
    Shortcut { sequence: "F2"; onActivated: window.forcedDeviceType = "Tablet" }
    Shortcut { sequence: "F3"; onActivated: window.forcedDeviceType = "PC" }
    Shortcut { sequence: "F4"; onActivated: window.forcedDeviceType = "TV" }

    background: Rectangle {
        color: "#0A0B10"
        
        // Красивый градиент в стиле OTT Navigator для главного меню
        Rectangle {
            anchors.fill: parent
            opacity: 0.15
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#00E676" }
                GradientStop { position: 1.0; color: "transparent" }
            }
        }
    }

    // НАДЁЖНЫЙ ФАЙЛОВЫЙ ДИАЛОГ (с префиксом во избежание конфликтов имен)
    QtDialogs.FileDialog {
        id: filePicker
        title: "Выберите M3U файл"
        onAccepted: {
            window.murlPath = selectedFile.toString().replace("file:///", "").replace("file://", "")
        }
    }

    // ДИАЛОГ ВЫВОДА ОШИБОК СЕТИ/ПАРСИНГА
    Dialog {
        id: errorDialog
        title: "⚠️ Ошибка загрузки"
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Ok
        width: Math.min(380, window.width - 40) // Адаптивная ширина под телефоны и ПК

        background: Rectangle {
            color: "#151622"
            border.color: "#FF5252"
            border.width: 1
            radius: 12
        }

        Label {
            id: errorDialogText
            text: ""
            color: "white"
            font.pixelSize: window.fontSizeBody
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            anchors.left: parent.left
            anchors.right: parent.right
        }
    }

    // ДИАЛОГ ПОДТВЕРЖДЕНИЯ УДАЛЕНИЯ ПЛЕЙЛИСТА
    Dialog {
        id: deleteConfirmDialog
        title: "🗑️ Удаление плейлиста"
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        width: Math.min(380, window.width - 40) // Адаптивная ширина под телефоны и ПК

        property int targetId: -1
        property string targetName: ""

        background: Rectangle {
            color: "#151622"
            border.color: "#FF5252"
            border.width: 1
            radius: 12
        }

        Label {
            text: "Вы действительно хотите удалить плейлист '" + deleteConfirmDialog.targetName + "'?"
            color: "white"
            font.pixelSize: window.fontSizeBody
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            anchors.left: parent.left
            anchors.right: parent.right
        }

        onAccepted: {
            if (targetId !== -1) {
                backend.deletePlaylist(targetId)
            }
        }
    }

    StackView {
        id: stack
        anchors.fill: parent
        initialItem: dashboardPage
    }

    // Вспомогательная функция для форматирования времени из секунд в MM:SS
    function formatTime(seconds) {
        if (isNaN(seconds) || seconds < 0) return "00:00"
        var totalSecs = Math.floor(seconds)
        var secs = totalSecs % 60
        var mins = Math.floor(totalSecs / 60) % 60
        var hours = Math.floor(totalSecs / 3600)
        
        var secsStr = secs < 10 ? "0" + secs : secs
        var minsStr = mins < 10 ? "0" + mins : mins
        
        if (hours > 0) {
            return hours + ":" + minsStr + ":" + secsStr
        } else {
            return minsStr + ":" + secsStr
        }
    }

    // ==========================================
    // 1. ДАШБОРД (СПИСОК ПЛЕЙЛИСТОВ)
    // ==========================================
    Component {
        id: dashboardPage
        Page {
            objectName: "dashboardPage"
            background: Rectangle { color: "transparent" }

            header: ToolBar {
                background: Rectangle { color: "#11121E" }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 20
                    anchors.rightMargin: 20
                    Label {
                        text: "PURE IPTV PLAYER"
                        font.pixelSize: window.fontSizeHeader
                        font.bold: true
                        color: "#00E676"
                        Layout.fillWidth: true
                    }
                    IconButton {
                        text: "＋"
                        onClicked: stack.push(addPlaylistPage)
                        ToolTip.visible: hovered
                        ToolTip.text: "Добавить новый плейлист"
                    }
                }
            }

            // ПУСТОЙ СТАТУС (Если нет плейлистов) - ТЕПЕРЬ СТРОГО ПО ЦЕНТРУ!
            Column {
                anchors.centerIn: parent
                spacing: 25
                width: Math.min(500, parent.width - 40)
                visible: backend.playlists.length === 0

                Rectangle {
                    width: 120 * window.scaleFactor
                    height: 120 * window.scaleFactor
                    radius: width / 2
                    color: "#151829"
                    border.color: "#00E676"
                    border.width: 2
                    anchors.horizontalCenter: parent.horizontalCenter
                    
                    Label {
                        text: "📺"
                        font.pixelSize: 55 * window.scaleFactor
                        anchors.centerIn: parent
                    }
                }

                Label {
                    text: "Нет добавленных плейлистов"
                    font.pixelSize: window.fontSizeHeader
                    font.bold: true
                    color: "white"
                    anchors.horizontalCenter: parent.horizontalCenter
                    horizontalAlignment: Text.AlignHCenter
                }

                Label {
                    text: "Нажмите на кнопку ниже или на «плюсик» сверху,\nчтобы добавить ваш первый M3U, Xtream или Stalker плейлист.\n\n[Совет: F1-F4 на ПК меняют режимы Смартфон/Планшет/ПК/ТВ]"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: window.fontSizeSub
                    color: "#8E92B2"
                    anchors.horizontalCenter: parent.horizontalCenter
                }

                Button {
                    text: "  ДОБАВИТЬ ПЛЕЙЛИСТ  "
                    font.bold: true
                    font.pixelSize: window.fontSizeTitle
                    highlighted: true
                    Material.accent: "#00E676"
                    anchors.horizontalCenter: parent.horizontalCenter
                    height: 55 * window.scaleFactor
                    onClicked: stack.push(addPlaylistPage)
                }
            }

            // СПИСОК ПЛЕЙЛИСТОВ (Отображается только когда они есть)
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 30
                spacing: 20
                visible: backend.playlists.length > 0

                Label {
                    text: "Ваши плейлисты (Используйте стрелочки и Enter для навигации)"
                    font.pixelSize: window.fontSizeTitle
                    font.bold: true
                    color: "#8E92B2"
                    Layout.fillWidth: true
                }

                GridView {
                    id: plistGrid
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    cellWidth: window.isWide ? width / 3 : width / 2
                    cellHeight: 140 * window.scaleFactor
                    clip: true
                    model: backend.playlists
                    focus: true // Захватываем фокус по умолчанию для ТВ пульта

                    delegate: Item {
                        width: plistGrid.cellWidth - 15
                        height: 120 * window.scaleFactor

                        Rectangle {
                            anchors.fill: parent
                            radius: 16
                            // Полная поддержка фокуса для ТВ пульта и мыши на ПК
                            color: (plistGrid.currentIndex === index && plistGrid.activeFocus) ? "#1F213A" : (playlistMouseArea.hovered ? "#1C1D30" : "#131424")
                            border.color: (plistGrid.currentIndex === index && plistGrid.activeFocus) ? "#00E676" : (playlistMouseArea.hovered ? "#00E676" : "#23253B")
                            border.width: (plistGrid.currentIndex === index && plistGrid.activeFocus) ? 2 : 1

                            Behavior on color { ColorAnimation { duration: 150 } }

                            // Помещаем MouseArea первым, чтобы RowLayout (с кнопкой удаления) был над ним
                            MouseArea {
                                id: playlistMouseArea
                                anchors.fill: parent
                                hoverEnabled: true
                                onClicked: {
                                    plistGrid.currentIndex = index
                                    enterPlaylist()
                                }
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 15
                                spacing: 15

                                Rectangle {
                                    width: 50 * window.scaleFactor
                                    height: 50 * window.scaleFactor
                                    radius: width / 2
                                    color: "#23253B"
                                    Label {
                                        text: modelData.proto === "M3U" ? "📝" : (modelData.proto === "XTREAM" ? "⚡" : "🧬")
                                        font.pixelSize: 24 * window.scaleFactor
                                        anchors.centerIn: parent
                                    }
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 4
                                    Label {
                                        text: modelData.name
                                        font.bold: true
                                        font.pixelSize: window.fontSizeTitle
                                        color: "white"
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                    Label {
                                        text: modelData.proto + " • " + (modelData.host.length > 25 ? modelData.host.substring(0, 25) + "..." : modelData.host)
                                        font.pixelSize: window.fontSizeSub
                                        color: "#8E92B2"
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                }

                                IconButton {
                                    text: "🗑️"
                                    Material.accent: "#FF5252"
                                    z: 10 // Клик не провалится под карточку
                                    onClicked: {
                                        deleteConfirmDialog.targetId = modelData.id
                                        deleteConfirmDialog.targetName = modelData.name
                                        deleteConfirmDialog.open()
                                    }
                                }
                            }
                        }

                        // Логика захода в плейлист с пульта (кнопка Enter/OK)
                        function enterPlaylist() {
                            backend.loadPlaylist(modelData.id)
                            window.activeCategory = "Все каналы"
                            window.searchQuery = ""
                            stack.push(mainPage)
                        }

                        Keys.onReturnPressed: enterPlaylist()
                        Keys.onEnterPressed: enterPlaylist()
                    }
                }
            }
        }
    }

    // ==========================================
    // 2. СТРАНИЦА ДОБАВЛЕНИЯ ПЛЕЙЛИСТА
    // ==========================================
    Component {
        id: addPlaylistPage
        Page {
            objectName: "addPlaylistPage"
            background: Rectangle { color: "transparent" }

            Connections {
                target: backend
                function onLoadFinished() {
                    loadingOverlay.visible = false
                    stack.pop() // Возвращаемся на дашборд
                }
                function onLoadFailed(errorMsg) {
                    loadingOverlay.visible = false
                    errorDialogText.text = "Не удалось загрузить плейлист.\nПричина: " + errorMsg
                    errorDialog.open()
                }
            }

            header: ToolBar {
                background: Rectangle { color: "#11121E" }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 15
                    Button {
                        text: "← Назад"
                        flat: true
                        onClicked: stack.pop()
                    }
                    Label {
                        text: "Добавление плейлиста"
                        font.bold: true
                        font.pixelSize: window.fontSizeTitle
                        Layout.fillWidth: true
                    }
                }
            }

            ScrollView {
                anchors.fill: parent
                contentWidth: availableWidth

                ColumnLayout {
                    width: Math.min(600, parent.width - 40)
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: 40
                    spacing: 25

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Label { text: "Название плейлиста"; font.bold: true; color: "#8E92B2"; font.pixelSize: window.fontSizeBody }
                        TextField {
                            id: pnameInput
                            placeholderText: "Например: Мой Провайдер"
                            Layout.fillWidth: true
                            color: "white"
                            font.pixelSize: window.fontSizeBody
                        }
                    }

                    TabBar {
                        id: ptabs
                        Layout.fillWidth: true
                        TabButton { text: "M3U Link / File"; font.pixelSize: window.fontSizeBody }
                        TabButton { text: "Xtream Codes"; font.pixelSize: window.fontSizeBody }
                        TabButton { text: "Stalker Portal"; font.pixelSize: window.fontSizeBody }
                    }

                    StackLayout {
                        currentIndex: ptabs.currentIndex
                        Layout.fillWidth: true
                        Layout.preferredHeight: 180

                        // M3U Tab
                        ColumnLayout {
                            spacing: 15
                            RowLayout {
                                Layout.fillWidth: true
                                TextField {
                                    id: murlInput
                                    placeholderText: "URL-ссылка или путь к файлу"
                                    Layout.fillWidth: true
                                    color: "white"
                                    text: window.murlPath
                                    font.pixelSize: window.fontSizeBody
                                    onTextChanged: window.murlPath = text
                                }
                                Button {
                                    text: "📂 Файл"
                                    onClicked: filePicker.open()
                                }
                            }
                        }

                        // Xtream Tab
                        ColumnLayout {
                            spacing: 12
                            TextField { id: xhInput; placeholderText: "Адрес сервера (Хост)"; Layout.fillWidth: true; font.pixelSize: window.fontSizeBody }
                            RowLayout {
                                Layout.fillWidth: true
                                TextField { id: xuInput; placeholderText: "Логин"; Layout.fillWidth: true; font.pixelSize: window.fontSizeBody }
                                TextField { id: xpInput; placeholderText: "Пароль"; echoMode: TextInput.Password; Layout.fillWidth: true; font.pixelSize: window.fontSizeBody }
                            }
                        }

                        // Stalker Tab
                        ColumnLayout {
                            spacing: 12
                            TextField { id: shInput; placeholderText: "Адрес портала (Хост)"; Layout.fillWidth: true; font.pixelSize: window.fontSizeBody }
                            TextField { id: smInput; placeholderText: "MAC-адрес"; Layout.fillWidth: true; font.pixelSize: window.fontSizeBody }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Label { text: "Телепрограмма XMLTV (опционально)"; font.bold: true; color: "#8E92B2"; font.pixelSize: window.fontSizeBody }
                        TextField {
                            id: pepgInput
                            placeholderText: "http://example.com/epg.xml.gz"
                            Layout.fillWidth: true
                            color: "white"
                            font.pixelSize: window.fontSizeBody
                        }
                    }

                    Button {
                        text: "ПОДКЛЮЧИТЬ И СОХРАНИТЬ"
                        Layout.fillWidth: true
                        height: 60 * window.scaleFactor
                        font.bold: true
                        font.pixelSize: window.fontSizeTitle
                        highlighted: true
                        enabled: pnameInput.text.trim().length > 0 && 
                                 (ptabs.currentIndex === 0 ? murlInput.text.trim().length > 0 : 
                                  ptabs.currentIndex === 1 ? xhInput.text.trim().length > 0 : 
                                  shInput.text.trim().length > 0)

                        onClicked: {
                            var proto = "M3U"
                            var host = murlInput.text
                            if (ptabs.currentIndex === 1) {
                                proto = "XTREAM"
                                host = xhInput.text
                            } else if (ptabs.currentIndex === 2) {
                                proto = "STALKER"
                                host = shInput.text
                            }

                            loadingOverlay.visible = true
                            backend.addPlaylist(
                                pnameInput.text, 
                                proto, 
                                host, 
                                pepgInput.text, 
                                xuInput.text, 
                                xpInput.text, 
                                smInput.text
                            )
                        }
                    }
                }
            }

            Rectangle {
                id: loadingOverlay
                anchors.fill: parent
                color: "#F205060A"
                visible: false

                Column {
                    anchors.centerIn: parent
                    spacing: 20
                    width: parent.width * 0.8

                    BusyIndicator {
                        running: loadingOverlay.visible
                        anchors.horizontalCenter: parent.horizontalCenter
                        implicitWidth: 80
                        implicitHeight: 80
                    }

                    Label {
                        text: backend.status
                        font.pixelSize: window.fontSizeTitle
                        font.bold: true
                        color: "white"
                        anchors.horizontalCenter: parent.horizontalCenter
                        horizontalAlignment: Text.AlignHCenter
                    }

                    Button {
                        text: "ОТМЕНИТЬ ПОДКЛЮЧЕНИЕ"
                        flat: true
                        font.bold: true
                        Material.accent: "#FF5252"
                        anchors.horizontalCenter: parent.horizontalCenter
                        onClicked: {
                            backend.cancelConnection()
                            loadingOverlay.visible = false
                        }
                    }
                }
            }
        }
    }

    // ==========================================
    // 3. ОСНОВНАЯ СТРАНИЦА (КАНАЛЫ + КАТЕГОРИИ)
    // ==========================================
    Component {
        id: mainPage
        Page {
            id: mainPageInstance
            objectName: "mainPage"
            background: Rectangle { color: "transparent" }

            header: ToolBar {
                background: Rectangle { color: "#11121E" }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 15
                    anchors.rightMargin: 15

                    Button {
                        text: "← Плейлисты"
                        flat: true
                        font.pixelSize: window.fontSizeBody
                        onClicked: stack.pop()
                    }

                    // Кнопка Категории (Только на Смартфонах)
                    Button {
                        text: "📁 Категории"
                        flat: true
                        visible: !window.showCategoriesSidebar
                        font.pixelSize: window.fontSizeBody
                        onClicked: catDrawer.open()
                    }

                    Label {
                        text: backend.current_playlist_name
                        font.bold: true
                        font.pixelSize: window.fontSizeTitle
                        Layout.fillWidth: true
                    }

                    TextField {
                        id: searchBar
                        placeholderText: "Поиск канала..."
                        implicitWidth: 260 * window.scaleFactor
                        color: "white"
                        font.pixelSize: window.fontSizeBody
                        text: window.searchQuery
                        onTextChanged: {
                            window.searchQuery = text
                            refreshChannels()
                        }
                    }
                }
            }

            // Функция живого обновления каналов
            function refreshChannels() {
                var list = backend.getFilteredChannels(window.activeCategory, window.searchQuery)
                window.currentFilteredList = list
                clist.model = list
            }

            Component.onCompleted: {
                refreshChannels()
                clist.forceActiveFocus() // Навешиваем фокус на список каналов при входе
            }

            RowLayout {
                anchors.fill: parent
                spacing: 0

                // 3.1. Левый Сайдбар: Список Категорий (Скрыт на телефонах)
                Rectangle {
                    visible: window.showCategoriesSidebar
                    Layout.fillHeight: true
                    Layout.preferredWidth: Math.round(240 * window.scaleFactor)
                    color: "#0E0F19"

                    ListView {
                        id: catList
                        anchors.fill: parent
                        anchors.margins: 10
                        clip: true
                        model: backend.categories
                        focus: false

                        // Навигация ТВ Пульта (Вправо -> Переводит на каналы)
                        KeyNavigation.right: clist

                        delegate: ItemDelegate {
                            width: catList.width - 20
                            height: 50 * window.scaleFactor
                            
                            background: Rectangle {
                                color: (catList.currentIndex === index && catList.activeFocus) ? "#1F213A" : (window.activeCategory === modelData ? "#151622" : "transparent")
                                border.color: (catList.currentIndex === index && catList.activeFocus) ? "#00E676" : "transparent"
                                border.width: 1.5
                                radius: 10
                            }

                            contentItem: Label {
                                text: modelData
                                font.bold: window.activeCategory === modelData
                                font.pixelSize: window.fontSizeBody
                                color: window.activeCategory === modelData ? "#00E676" : "white"
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }

                            function selectCategory() {
                                catList.currentIndex = index
                                window.activeCategory = modelData
                                mainPageInstance.refreshChannels()
                            }

                            onClicked: selectCategory()
                            Keys.onReturnPressed: selectCategory()
                            Keys.onEnterPressed: selectCategory()
                        }
                    }
                }

                // Разделитель
                Rectangle {
                    visible: window.showCategoriesSidebar
                    Layout.fillHeight: true
                    width: 1
                    color: "#23253B"
                }

                // 3.2. Центр: Список Каналов (Адаптивный фокус ТВ-пульта)
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 0

                    ListView {
                        id: clist
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        focus: true // Занимает фокус ввода по умолчанию

                        // Навигация D-Pad (Влево -> Категории, Вправо -> Программа)
                        KeyNavigation.left: window.showCategoriesSidebar ? catList : null
                        KeyNavigation.right: window.showEpgSidebar ? elist : null

                        delegate: ItemDelegate {
                            width: clist.width
                            height: window.channelIconSize + Math.round(24 * window.scaleFactor)

                            background: Rectangle {
                                // Идеальный ТВ фокус подсветка
                                color: (clist.currentIndex === index && clist.activeFocus) ? "#1C1D30" : (window.selCh === modelData ? "#131424" : "transparent")
                                border.color: (clist.currentIndex === index && clist.activeFocus) ? "#00E676" : (hovered ? "#00E676" : "transparent")
                                border.width: 1.5
                                radius: 12
                            }

                            RowLayout {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.leftMargin: Math.round(15 * window.scaleFactor)
                                anchors.rightMargin: Math.round(15 * window.scaleFactor)
                                spacing: Math.round(15 * window.scaleFactor)

                                // Логотип канала (Подстраивается под размер устройства F1-F4)
                                Rectangle {
                                    width: window.channelIconSize
                                    height: window.channelIconSize
                                    color: "#05060A"
                                    radius: 8
                                    clip: true
                                    Layout.alignment: Qt.AlignVCenter
                                    Image {
                                        id: chanLogo
                                        anchors.fill: parent
                                        source: modelData.logo || ""
                                        fillMode: Image.PreserveAspectFit
                                        asynchronous: true
                                        // Показываем картинку только если она есть и успешно загружена
                                        visible: modelData.logo && status === Image.Ready
                                    }
                                    Label {
                                        text: "📺"
                                        font.pixelSize: Math.round(window.channelIconSize * 0.45)
                                        anchors.centerIn: parent
                                        // Если логотипа нет или произошла ошибка загрузки (404, 400 и т.д.) - показываем ТВ-заглушку!
                                        visible: !modelData.logo || chanLogo.status !== Image.Ready
                                    }
                                }

                                // Имя и текущая телепередача (EPG)
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.alignment: Qt.AlignVCenter
                                    spacing: 4
                                    Label {
                                        text: modelData.name
                                        font.bold: true
                                        font.pixelSize: window.fontSizeTitle
                                        color: "white"
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                    Label {
                                        text: backend.getCurrentEPG(modelData.id)
                                        font.pixelSize: window.isWide ? 12 : 11
                                        color: "#8E92B2"
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                }

                                // Кнопка ИЗБРАННОГО (Звезда)
                                IconButton {
                                    id: favBtn
                                    text: backend.isFavorite(modelData.id) ? "★" : "☆"
                                    Material.accent: backend.isFavorite(modelData.id) ? "#FFD54F" : "#8E92B2"
                                    Layout.alignment: Qt.AlignVCenter
                                    onClicked: {
                                        clist.currentIndex = index
                                        backend.toggleFavorite(modelData.id)
                                        mainPageInstance.refreshChannels()
                                    }
                                }
                            }

                            function selectChannel() {
                                clist.currentIndex = index
                                window.selCh = modelData
                                window.currentChIndex = index
                                backend.updateEPG(modelData.id)
                                backend.play(modelData.url, modelData.name, modelData.group, "")
                                stack.push(playerPage)
                            }

                            onClicked: selectChannel()
                            Keys.onReturnPressed: selectChannel()
                            Keys.onEnterPressed: selectChannel()
                        }
                    }
                }

                // Разделитель
                Rectangle {
                    visible: window.showEpgSidebar
                    Layout.fillHeight: true
                    width: 1
                    color: "#23253B"
                }

                // 3.3. Правый Сайдбар: Программа передач (EPG)
                Rectangle {
                    visible: window.showEpgSidebar
                    Layout.fillHeight: true
                    Layout.preferredWidth: Math.round(320 * window.scaleFactor)
                    color: "#0C0D15"

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 0

                        Rectangle {
                            Layout.fillWidth: true
                            height: 55 * window.scaleFactor
                            color: "#11121E"
                            Label {
                                anchors.centerIn: parent
                                text: "ТЕЛЕПРОГРАММА"
                                font.bold: true
                                color: "#00E676"
                                font.pixelSize: window.fontSizeTitle
                            }
                        }

                        ListView {
                            id: elist
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            model: backend ? backend.epgModel : null
                            clip: true
                            focus: false
                            
                            // Навигация: Влево -> Возвращает на каналы
                            KeyNavigation.left: clist

                            delegate: ItemDelegate {
                                width: elist.width
                                height: 80 * window.scaleFactor

                                background: Rectangle {
                                    color: (elist.currentIndex === index && elist.activeFocus) ? "#1C1D30" : "transparent"
                                    border.color: (elist.currentIndex === index && elist.activeFocus) ? "#00E676" : "transparent"
                                    border.width: 1.5
                                    radius: 8
                                }

                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 10
                                    Label {
                                        text: model.displayTime
                                        color: "#00E676"
                                        font.bold: true
                                        font.pixelSize: window.fontSizeSub
                                    }
                                    Label {
                                        text: model.displayTitle
                                        color: "white"
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                        font.pixelSize: window.fontSizeBody
                                    }
                                }

                                function selectEpgItem() {
                                    elist.currentIndex = index
                                    var archUrl = backend.getArchiveUrl(window.selCh.url, model.startRaw)
                                    window.selCh = {
                                        "id": window.selCh.id,
                                        "name": window.selCh.name,
                                        "logo": window.selCh.logo,
                                        "group": window.selCh.group,
                                        "url": archUrl
                                    }
                                    backend.play(archUrl, window.selCh.name, window.selCh.group, "")
                                    stack.push(playerPage)
                                }

                                onClicked: selectEpgItem()
                                Keys.onReturnPressed: selectEpgItem()
                                Keys.onEnterPressed: selectEpgItem()
                            }
                        }
                    }
                }
            }

            // Drawer для категорий (на смартфонах) теперь полностью изолирован внутри mainPage,
            // решая проблему Component Scope Error и гарантируя мгновенное закрытие без зависаний!
            Drawer {
                id: catDrawer
                width: Math.min(300, parent.width * 0.8)
                height: parent.height
                edge: Qt.LeftEdge
                background: Rectangle { color: "#0E0F19"; radius: 10 }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 20
                    spacing: 15

                    Label {
                        text: "КАТЕГОРИИ"
                        font.bold: true
                        font.pixelSize: window.fontSizeHeader
                        color: "#00E676"
                        Layout.alignment: Qt.AlignHCenter
                    }

                    ListView {
                        id: catDrawerList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: backend.categories

                        delegate: ItemDelegate {
                            width: catDrawerList.width
                            height: 50 * window.scaleFactor
                            
                            background: Rectangle {
                                color: window.activeCategory === modelData ? "#1F213A" : "transparent"
                                border.color: window.activeCategory === modelData ? "#00E676" : "transparent"
                                border.width: 1
                                radius: 10
                            }

                            contentItem: Label {
                                text: modelData
                                font.bold: window.activeCategory === modelData
                                font.pixelSize: window.fontSizeBody
                                color: window.activeCategory === modelData ? "#00E676" : "white"
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }

                            onClicked: {
                                window.activeCategory = modelData
                                mainPageInstance.refreshChannels()
                                catDrawer.close()
                            }
                        }
                    }
                }
            }
        }
    }

    // ==========================================
    // 4. СТРАНИЦА ВИДЕОПЛЕЕРА (ФОН ДЛЯ MPV)
    // ==========================================
    Component {
        id: playerPage
        Page {
            id: proot
            objectName: "playerPage"
            background: Rectangle { color: "black" }

            // Показываем заставку канала во время загрузки видео (Никаких "...")
            ColumnLayout {
                anchors.centerIn: parent
                spacing: 20
                visible: busyIndicator.visible

                Rectangle {
                    width: 120 * window.scaleFactor
                    height: 120 * window.scaleFactor
                    color: "#151622"
                    radius: 16
                    Layout.alignment: Qt.AlignHCenter
                    
                    Image {
                        id: playerLogo
                        anchors.fill: parent
                        source: window.selCh && window.selCh.logo ? window.selCh.logo : ""
                        fillMode: Image.PreserveAspectFit
                        visible: window.selCh && window.selCh.logo && status === Image.Ready
                    }
                    Label {
                        text: "📺"
                        font.pixelSize: 55 * window.scaleFactor
                        anchors.centerIn: parent
                        visible: !window.selCh || !window.selCh.logo || playerLogo.status !== Image.Ready
                    }
                }

                Label {
                    text: window.selCh ? window.selCh.name : "Загрузка трансляции..."
                    font.bold: true
                    font.pixelSize: window.fontSizeHeader
                    color: "white"
                    Layout.alignment: Qt.AlignHCenter
                }

                Label {
                    text: backend.isBuffering ? "Слабый сигнал. Идёт буферизация (" + backend.bufferingProgress + "%)..." : "Пожалуйста, подождите. Инициализируем видеопоток MPV..."
                    font.pixelSize: window.fontSizeBody
                    color: backend.isBuffering ? "#FFC107" : "#8E92B2"
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            BusyIndicator { 
                id: busyIndicator
                anchors.centerIn: parent
                anchors.verticalCenterOffset: 130 * window.scaleFactor
                width: 80 * window.scaleFactor
                height: 80 * window.scaleFactor
                running: true 
                visible: true
            }
            
            Timer {
                id: hideBusyTimer
                interval: 5000
                onTriggered: {
                    busyIndicator.visible = false
                    busyIndicator.running = false
                }
            }

            Connections {
                target: backend
                function onPlayingChanged(playing) {
                    if (playing && !backend.isBuffering) {
                        busyIndicator.visible = false
                        busyIndicator.running = false
                        hideBusyTimer.stop()
                    } else {
                        busyIndicator.visible = true
                        busyIndicator.running = true
                    }
                }
                function onBufferingChanged() {
                    if (backend.isBuffering) {
                        busyIndicator.visible = true
                        busyIndicator.running = true
                    } else {
                        busyIndicator.visible = false
                        busyIndicator.running = false
                    }
                }
            }

            onVisibleChanged: {
                if (visible) {
                    busyIndicator.visible = true
                    busyIndicator.running = true
                    hideBusyTimer.start()
                }
            }
        }
    }

    // ==========================================
    // 5. ОВЕРЛЕЙНОЕ ПРОЗРАЧНОЕ ОКНО ДЛЯ OSD
    // ==========================================
    // Это революционное решение решает "Airspace" проблему на Windows:
    // Оно выводит оверлей с кнопками в отдельном прозрачном окне прямо поверх MPV!
    // Все кнопки интерактивны, видны и управляют MPV бэкэндом.
    Window {
        id: playerOsdWindow
        // Скрываем оверлей, когда главное окно теряет фокус (window.active === false),
        // полностью исключая наложение кнопок поверх сторонних приложений!
        visible: window.visible && (Qt.application.state === Qt.ApplicationActive) && (stack.currentItem && stack.currentItem.objectName === "playerPage")
        color: "transparent"
        // Флаг Qt.Dialog гарантирует, что оверлей привязан к главному окну как диалог,
        // всегда находится НАД ним, но послушно уходит на задний план вместе с ним.
        flags: Qt.FramelessWindowHint | Qt.Dialog
        
        // Синхронизируем положение и геометрию с главным окном
        x: window.x
        y: window.y
        width: window.width
        height: window.height

        // Контейнер OSD управления
        Rectangle {
            id: prootOsd
            anchors.fill: parent
            color: "transparent"
            focus: true // Переносим фокус ввода на графический элемент Item

            // ========================================================
            // НАТИВНАЯ КЛАВИАТУРНАЯ И D-PAD НАВИГАЦИЯ ДЛЯ ТВ ПУЛЬТА!
            // (Идеально подстраивается под пульты без мыши)
            // ========================================================
            Keys.onUpPressed: {
                backend.volume = Math.min(100, backend.volume + 5)
                showOsdTemporarily()
            }
            Keys.onDownPressed: {
                backend.volume = Math.max(0, backend.volume - 5)
                showOsdTemporarily()
            }
            Keys.onLeftPressed: {
                prootOsd.playPrevChannel()
                showOsdTemporarily()
            }
            Keys.onRightPressed: {
                prootOsd.playNextChannel()
                showOsdTemporarily()
            }
            Keys.onReturnPressed: {
                backend.togglePause()
                showOsdTemporarily()
            }
            Keys.onEnterPressed: {
                backend.togglePause()
                showOsdTemporarily()
            }
            // Клавиша R для переподключения
            Keys.onPressed: {
                if (event.key === Qt.Key_R) {
                    // Перезапускаем текущий канал
                    if (window.selCh) {
                        backend.play(window.selCh.url, window.selCh.name, window.selCh.group, "")
                    }
                    event.accepted = true
                }
            }

            function showOsdTemporarily() {
                topOsdBar.visible = true
                bottomOsdBar.visible = true
                osdTimer.restart()
            }

            function playNextChannel() {
                if (window.currentFilteredList.length > 0 && window.currentChIndex !== -1) {
                    var nextIndex = (window.currentChIndex + 1) % window.currentFilteredList.length
                    window.currentChIndex = nextIndex
                    var nextCh = window.currentFilteredList[nextIndex]
                    window.selCh = nextCh
                    backend.updateEPG(nextCh.id)
                    backend.play(nextCh.url, nextCh.name, nextCh.group, "")
                }
            }

            function playPrevChannel() {
                if (window.currentFilteredList.length > 0 && window.currentChIndex !== -1) {
                    var prevIndex = window.currentChIndex - 1
                    if (prevIndex < 0) prevIndex = window.currentFilteredList.length - 1
                    window.currentChIndex = prevIndex
                    var prevCh = window.currentFilteredList[prevIndex]
                    window.selCh = prevCh
                    backend.updateEPG(prevCh.id)
                    backend.play(prevCh.url, prevCh.name, prevCh.group, "")
                }
            }

            // Отслеживаем движение мыши над видео, чтобы прятать/показывать OSD
            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onClicked: {
                    osdTimer.restart()
                    topOsdBar.visible = !topOsdBar.visible
                    bottomOsdBar.visible = !bottomOsdBar.visible
                }
                onPositionChanged: {
                    topOsdBar.visible = true
                    bottomOsdBar.visible = true
                    osdTimer.restart()
                }
            }

            // Таймер автоскрытия OSD через 4 секунды
            Timer {
                id: osdTimer
                interval: 4000
                running: true
                onTriggered: {
                    topOsdBar.visible = false
                    bottomOsdBar.visible = false
                }
            }

            // 5.1. ВЕРХНИЙ OSDБАР (Назад, Заголовок, Качество, Сигнал)
            Rectangle {
                id: topOsdBar
                anchors.top: parent.top
                width: parent.width
                height: 75 * window.scaleFactor
                color: "#CC05060A"
                visible: true

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 20
                    anchors.rightMargin: 20

                    Button {
                        text: "← КАНАЛЫ"
                        highlighted: true
                        font.pixelSize: window.fontSizeBody
                        onClicked: {
                            backend.stop()
                            stack.pop()
                        }
                    }

                    Item { Layout.fillWidth: true }

                    // Индикатор качества соединения
                    RowLayout {
                        spacing: 8
                        Layout.alignment: Qt.AlignVCenter
                        
                        // Сигнал WiFi
                        Label {
                            text: {
                                var q = backend.connectionQuality
                                if (q === "excellent") return "📶📶📶"
                                if (q === "good") return "📶📶"
                                if (q === "fair") return "📶"
                                if (q === "poor") return "📶⚠️"
                                return "📡"
                            }
                            font.pixelSize: 18 * window.scaleFactor
                            color: {
                                var q = backend.connectionQuality
                                if (q === "excellent" || q === "good") return "#00E676"
                                if (q === "fair") return "#FFC107"
                                return "#FF5252"
                            }
                        }
                        
                        // Качество видео
                        Label {
                            text: {
                                var q = backend.currentQuality
                                if (q === "ultra") return "4K"
                                if (q === "high") return "1080p"
                                if (q === "medium") return "720p"
                                if (q === "low") return "480p"
                                if (q === "minimal") return "360p"
                                return "AUTO"
                            }
                            font.pixelSize: 12 * window.scaleFactor
                            color: "#8E92B2"
                        }
                    }

                    Item { Layout.fillWidth: true }

                    Label {
                        text: window.selCh ? window.selCh.name : "Загрузка..."
                        font.bold: true
                        font.pixelSize: window.fontSizeHeader
                        color: "#00E676"
                    }

                    Item { Layout.fillWidth: true }

                    // Выбор качества видео
                    Button {
                        id: qualityBtn
                        text: {
                            var q = backend.currentQuality
                            if (q === "ultra") return "📺 4K"
                            if (q === "high") return "📺 1080p"
                            if (q === "medium") return "📺 720p"
                            if (q === "low") return "📺 480p"
                            if (q === "minimal") return "📺 360p"
                            return "📺 AUTO"
                        }
                        font.pixelSize: window.fontSizeBody
                        onClicked: {
                            qualityMenu.open()
                        }
                        ToolTip.visible: hovered
                        ToolTip.text: "Выбор качества видео (Текущее: " + text + ")"
                    }
                    
                    // Формат экрана
                    Button {
                        text: "📐"
                        font.pixelSize: window.fontSizeBody
                        onClicked: {
                            if (window.currentAspect === "no") {
                                window.currentAspect = "16:9"
                            } else if (window.currentAspect === "16:9") {
                                window.currentAspect = "4:3"
                            } else if (window.currentAspect === "4:3") {
                                window.currentAspect = "stretch"
                            } else {
                                window.currentAspect = "no"
                            }
                            backend.setAspectRatio(window.currentAspect)
                        }
                        ToolTip.visible: hovered
                        ToolTip.text: "Формат экрана"
                    }
                }
                
                // Меню выбора качества
                Menu {
                    id: qualityMenu
                    title: "Качество"
                    
                    MenuItem {
                        text: "🔄 Автоматически (Рекомендуется)"
                        onClicked: backend.setQuality("auto")
                        visible: true
                    }
                    MenuItem {
                        text: "📺 4K Ultra HD"
                        onClicked: backend.setQuality("ultra")
                        visible: Array.from(backend.availableQualities).indexOf("ultra") !== -1
                    }
                    MenuItem {
                        text: "📺 1080p Full HD"
                        onClicked: backend.setQuality("high")
                        visible: Array.from(backend.availableQualities).indexOf("high") !== -1
                    }
                    MenuItem {
                        text: "📺 720p HD"
                        onClicked: backend.setQuality("medium")
                        visible: Array.from(backend.availableQualities).indexOf("medium") !== -1
                    }
                    MenuItem {
                        text: "📺 480p (Экономия трафика)"
                        onClicked: backend.setQuality("low")
                        visible: Array.from(backend.availableQualities).indexOf("low") !== -1
                    }
                    MenuItem {
                        text: "📺 360p (Минимум трафика)"
                        onClicked: backend.setQuality("minimal")
                        visible: Array.from(backend.availableQualities).indexOf("minimal") !== -1
                    }
                }
            }

            // 5.2. НИЖНИЙ OSDБАР (Управление, Программа, Буферизация)
            Rectangle {
                id: bottomOsdBar
                anchors.bottom: parent.bottom
                width: parent.width
                height: 180 * window.scaleFactor
                color: "#CC05060A"
                visible: true

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 15
                    spacing: 10

                    // Статус воспроизведения и буферизация
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 15
                        
                        // LIVE индикатор
                        Rectangle {
                            id: liveBadge
                            width: 60 * window.scaleFactor
                            height: 22 * window.scaleFactor
                            color: (backend.isPaused || backend.isBuffering) ? "#303240" : "#E53935"
                            radius: 4 * window.scaleFactor
                            visible: backend.duration === 0
                            
                            RowLayout {
                                anchors.centerIn: parent
                                spacing: 4
                                Rectangle {
                                    width: 6 * window.scaleFactor
                                    height: 6 * window.scaleFactor
                                    radius: 3 * window.scaleFactor
                                    color: (backend.isPaused || backend.isBuffering) ? "#8E92B2" : "#FFFFFF"
                                    
                                    SequentialAnimation on color {
                                        loops: Animation.Infinite
                                        running: !backend.isPaused && !backend.isBuffering && backend.duration === 0
                                        ColorAnimation { to: "transparent"; duration: 800 }
                                        ColorAnimation { to: "#FFFFFF"; duration: 800 }
                                    }
                                }
                                Label {
                                    text: "LIVE"
                                    color: "white"
                                    font.bold: true
                                    font.pixelSize: 10 * window.scaleFactor
                                }
                            }
                        }
                        
                        // Буферизация индикатор
                        Rectangle {
                            id: bufferingBadge
                            width: 140 * window.scaleFactor
                            height: 22 * window.scaleFactor
                            color: "#1B5E20"
                            radius: 4 * window.scaleFactor
                            visible: backend.isBuffering
                            
                            RowLayout {
                                anchors.centerIn: parent
                                spacing: 6
                                Label {
                                    text: "📡 БУФЕРИЗАЦИЯ"
                                    color: "#A5D6A7"
                                    font.bold: true
                                    font.pixelSize: 10 * window.scaleFactor
                                }
                                Label {
                                    text: backend.bufferingProgress + "%"
                                    color: "#FFFFFF"
                                    font.bold: true
                                    font.pixelSize: 11 * window.scaleFactor
                                }
                            }
                        }
                        
                        // Статус
                        Label {
                            text: backend.status
                            font.pixelSize: 11 * window.scaleFactor
                            color: "#8E92B2"
                            elide: Text.ElideRight
                            Layout.maximumWidth: 200 * window.scaleFactor
                        }
                    }

                    // Текущая телепрограмма EPG
                    Label {
                        text: window.selCh ? backend.getCurrentEPG(window.selCh.id) : "Телепрограмма недоступна"
                        font.pixelSize: window.fontSizeTitle - 2
                        color: "white"
                        font.bold: true
                        Layout.alignment: Qt.AlignHCenter
                        elide: Text.ElideRight
                        Layout.maximumWidth: parent.width * 0.7
                    }

                    // Ползунок времени (Seek Bar)
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 15

                        Label {
                            text: formatTime(backend.position)
                            color: "#8E92B2"
                            font.pixelSize: window.fontSizeSub
                        }

                        Slider {
                            id: progressSlider
                            Layout.fillWidth: true
                            from: 0
                            to: backend.duration > 0 ? backend.duration : 100
                            value: backend.position
                            onMoved: {
                                backend.position = value
                            }
                        }

                        Label {
                            text: formatTime(backend.duration)
                            color: "#8E92B2"
                            font.pixelSize: window.fontSizeSub
                        }
                    }

                    // Кнопки управления воспроизведением
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 20

                        // Назад на 1 канал
                        IconButton {
                            text: "⏮️"
                            onClicked: prootOsd.playPrevChannel()
                            ToolTip.visible: hovered
                            ToolTip.text: "Предыдущий (←)"
                        }

                        // Плей / Пауза
                        IconButton {
                            text: backend.isPaused ? "▶️" : "⏸️"
                            Material.accent: "#00E676"
                            highlighted: true
                            onClicked: {
                                backend.togglePause()
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: backend.isPaused ? "Воспроизведение" : "Пауза"
                        }

                        // Вперед на 1 канал
                        IconButton {
                            text: "⏭️"
                            onClicked: prootOsd.playNextChannel()
                            ToolTip.visible: hovered
                            ToolTip.text: "Следующий (→)"
                        }

                        Item { Layout.fillWidth: true }

                        // Индикатор соединения
                        Label {
                            text: backend.connectionQuality === "excellent" ? "🟢" : 
                                  backend.connectionQuality === "good" ? "🟢" :
                                  backend.connectionQuality === "fair" ? "🟡" : "🔴"
                            font.pixelSize: 20 * window.scaleFactor
                        }

                        // Громкость
                        RowLayout {
                            spacing: 8
                            Label {
                                text: backend.volume === 0 ? "🔇" : "🔊"
                                font.pixelSize: window.fontSizeTitle
                            }
                            Slider {
                                id: volSlider
                                from: 0
                                to: 100
                                value: backend.volume
                                implicitWidth: 100 * window.scaleFactor
                                onMoved: {
                                    backend.volume = value
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // Шаблон для красивых современных иконок-кнопок (с защитой от сжатия в "...")
    component IconButton : Button {
        id: iconBtn
        implicitWidth: 46 * window.scaleFactor
        implicitHeight: 46 * window.scaleFactor
        flat: true
        padding: 0
        
        contentItem: Text {
            text: iconBtn.text
            font.pixelSize: 18 * window.scaleFactor
            color: iconBtn.Material.accent
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideNone // Запрещаем любое сокращение в "..."!
        }
        
        background: Rectangle {
            color: iconBtn.hovered ? "#23253B" : "transparent"
            radius: width / 2
            border.color: iconBtn.pressed ? "#00E676" : "transparent"
            border.width: 1
        }
    }
}