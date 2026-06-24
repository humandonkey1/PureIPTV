import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs as QtDialogs
// Аврора? Теперь это Говнора. И да, я вдохновлялся OTT Navigator, TiviMate и IPTVnator.
// =========================================================================
//  PURE IPTV PLAYER  —  «Говнора» redesign
//  Премиальный тёмный OTT-интерфейс: глубина, стекло, неон, плавность.
//  Архитектура бэкенда (main.py) не тронута — сохранён весь контракт API.
// =========================================================================

ApplicationWindow {
    id: window
    width: 1200
    height: 800
    visible: true
    title: "Pure IPTV — Говнора"

    // ----------------------- ТЕМА (дизайн-токены) --------------------------
    readonly property color c_bg:        "#07080E"
    readonly property color c_bgDeep:    "#050609"
    readonly property color c_surface:   "#10121D"
    readonly property color c_surface2:  "#161929"
    readonly property color c_surface3:  "#1E2236"
    readonly property color c_border:    "#2A2F47"
    readonly property color c_borderSoft:"#1A1D2C"
    readonly property color c_text:      "#F1F3FB"
    readonly property color c_text2:     "#A2A9C6"
    readonly property color c_text3:     "#626A8E"
    readonly property color c_accent:    "#25E6A4"   // мятный изумруд
    readonly property color c_accent2:   "#00E676"   // неон
    readonly property color c_accentD:   "#0C9E6B"
    readonly property color c_live:      "#FF3B5C"
    readonly property color c_danger:    "#FF5670"
    readonly property color c_warn:      "#FFC24D"
    readonly property color c_info:      "#5B9DFF"
    readonly property color c_gold:      "#FFD24D"

    Material.theme: Material.Dark
    Material.foreground: c_text
    Material.accent: c_accent
    Material.background: c_bg

    // -----------------------------------------------------------------------
    property string murlPath: ""
    property var selCh: null
    property string activeCategory: "Все каналы"
    property string searchQuery: ""
    property var currentFilteredList: []
    property int currentChIndex: -1
    property string currentAspect: "no"
    property string targetVpnCountry: "Глобальный"

    // ============== АДАПТИВНАЯ ВЁРСТКА (ТВ / Планшет / Смартфон / ПК) ======
    property string deviceType: {
        var isMobile = (Qt.platform.os === "android" || Qt.platform.os === "ios")
        var diagonal = Math.sqrt(Screen.width * Screen.width + Screen.height * Screen.height) / Screen.pixelDensity / 25.4
        if (isMobile && (diagonal > 20 || Screen.width >= 1920 && Screen.height >= 1080 && !Screen.hasTouchScreen)) return "TV"
        if (isMobile) return diagonal >= 7.0 ? "Tablet" : "Phone"
        return "PC"
    }
    property string forcedDeviceType: ""
    readonly property string currentDevice: forcedDeviceType !== "" ? forcedDeviceType : deviceType

    readonly property real scaleFactor: {
        if (currentDevice === "TV") return 1.45
        if (currentDevice === "Tablet") return 1.2
        if (currentDevice === "Phone") return 0.95
        return 1.0
    }

    readonly property bool isWide: width >= 900
    readonly property bool isUltraWide: width >= 1400

    readonly property int fsHeader: Math.round(22 * scaleFactor)
    readonly property int fsTitle:  Math.round(16 * scaleFactor)
    readonly property int fsBody:   Math.round(14 * scaleFactor)
    readonly property int fsSub:    Math.round(12 * scaleFactor)

    readonly property bool showCategoriesSidebar: currentDevice === "PC" || currentDevice === "Tablet" || currentDevice === "TV"
    readonly property bool showEpgSidebar: (currentDevice === "PC" && isUltraWide) || (currentDevice === "TV" && width > 1200)

    readonly property int channelIconSize: {
        if (currentDevice === "TV") return 84
        if (currentDevice === "Tablet") return 58
        if (currentDevice === "Phone") return 42
        return 64
    }

    visibility: currentDevice === "TV" ? Window.FullScreen : Window.Maximized
    color: c_bg

    // ----------------------- ОТЛАДОЧНЫЕ ГОРЯЧИЕ КЛАВИШИ --------------------
    Shortcut { sequence: "F1"; onActivated: window.forcedDeviceType = "Phone" }
    Shortcut { sequence: "F2"; onActivated: window.forcedDeviceType = "Tablet" }
    Shortcut { sequence: "F3"; onActivated: window.forcedDeviceType = "PC" }
    Shortcut { sequence: "F4"; onActivated: window.forcedDeviceType = "TV" }

    // ----------------------- АТМОСФЕРНЫЙ ФОН --------------------------------
    background: Item {
        Rectangle { anchors.fill: parent; color: c_bgDeep }

        // мягкое неоновое свечение сверху-слева
        Rectangle {
            anchors.fill: parent; opacity: 0.10
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0.0; color: c_accent }
                GradientStop { position: 0.45; color: "transparent" }
            }
        }
        // холодный блюр снизу-справа
        Rectangle {
            anchors.fill: parent; opacity: 0.07
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 1.0; color: c_info }
                GradientStop { position: 0.55; color: "transparent" }
            }
        }
        // тонкая сетка-виньетка
        Rectangle {
            anchors.fill: parent; color: "transparent"
            opacity: 0.5
            Rectangle {
                anchors.fill: parent; color: c_bg; opacity: 0.0
            }
        }
    }

    // ----------------------- ФАЙЛОВЫЙ ДИАЛОГ --------------------------------
    QtDialogs.FileDialog {
        id: filePicker
        title: "Выберите M3U файл"
        onAccepted: window.murlPath = selectedFile.toString().replace("file:///", "").replace("file://", "")
    }

    // ----------------------- ДИАЛОГ ОШИБКИ ---------------------------------
    Dialog {
        id: errorDialog
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Ok
        width: Math.min(440, window.width - 40)
        padding: 0

        background: Rectangle {
            color: c_surface2; radius: 18
            border.color: c_danger; border.width: 1
            Rectangle { anchors.fill: parent; color: c_danger; opacity: 0.06; radius: 18 }
        }
        header: Item {
            width: parent.width; height: 64
            Rectangle { anchors.fill: parent; color: "transparent"; radius: 18
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 18; color: c_surface2 }
            }
            RowLayout {
                anchors.centerIn: parent; spacing: 10
                Rectangle { width: 30; height: 30; radius: 15; color: c_danger; opacity: 0.18
                    Label { anchors.centerIn: parent; text: "⚠"; font.pixelSize: 16 } }
                Label { text: "Не удалось загрузить"; color: c_text; font.bold: true; font.pixelSize: fsTitle }
            }
        }
        contentItem: Label {
            id: errorDialogText
            text: ""
            color: c_text2; font.pixelSize: fsBody
            horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
            leftPadding: 24; rightPadding: 24; topPadding: 18; bottomPadding: 18
        }
    }

    // ----------------------- ДИАЛОГ УДАЛЕНИЯ -------------------------------
    Dialog {
        id: deleteConfirmDialog
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        width: Math.min(440, window.width - 40)
        padding: 0
        property int targetId: -1
        property string targetName: ""

        background: Rectangle {
            color: c_surface2; radius: 18; border.color: c_danger; border.width: 1
        }
        header: Item {
            width: parent.width; height: 64
            RowLayout {
                anchors.centerIn: parent; spacing: 10
                Rectangle { width: 30; height: 30; radius: 15; color: c_danger; opacity: 0.18
                    Label { anchors.centerIn: parent; text: "🗑"; font.pixelSize: 15 } }
                Label { text: "Удаление плейлиста"; color: c_text; font.bold: true; font.pixelSize: fsTitle }
            }
        }
        contentItem: Label {
            text: "Удалить плейлист «" + deleteConfirmDialog.targetName + "»?\nЭто действие нельзя отменить."
            color: c_text2; font.pixelSize: fsBody
            horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
            leftPadding: 24; rightPadding: 24; topPadding: 16; bottomPadding: 22
        }
        onAccepted: if (targetId !== -1) backend.deletePlaylist(targetId)
    }

    // ----------------------- НАВИГАЦИЯ -------------------------------------
    StackView {
        id: stack
        anchors.fill: parent
        initialItem: dashboardPage
        replaceEnter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 260 } }
        replaceExit: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 200 } }
        pushEnter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 260 } }
        popEnter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 260 } }
    }

    function formatTime(seconds) {
        if (isNaN(seconds) || seconds < 0) return "00:00"
        var t = Math.floor(seconds), s = t % 60, m = Math.floor(t / 60) % 60, h = Math.floor(t / 3600)
        var ss = (s < 10 ? "0" : "") + s, mm = (m < 10 ? "0" : "") + m
        return h > 0 ? (h + ":" + mm + ":" + ss) : (mm + ":" + ss)
    }

    // =======================================================================
    // 1. ДАШБОРД
    // =======================================================================
    Component {
        id: dashboardPage
        Page {
            objectName: "dashboardPage"
            background: Rectangle { color: "transparent" }

            // --- HERO-ПАНЕЛЬ ---
            header: ToolBar {
                height: 220
                background: Item {
                    Rectangle { anchors.fill: parent; color: "transparent" }
                    Rectangle {
                        anchors.fill: parent
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0; color: Qt.rgba(0.145, 0.902, 0.643, 0.22) }
                            GradientStop { position: 0.5; color: Qt.rgba(0.357, 0.616, 1.0, 0.10) }
                            GradientStop { position: 1.0; color: "transparent" }
                        }
                    }
                    Rectangle { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.right: parent.right; height: 1; color: c_border; opacity: 0.7 }
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 34; anchors.rightMargin: 34
                    anchors.topMargin: 26; anchors.bottomMargin: 22
                    spacing: 6

                    RowLayout {
                        Layout.fillWidth: true; spacing: 16
                        // логотип-капсула
                        Rectangle {
                            width: 52; height: 52; radius: 16
                            gradient: Gradient {
                                orientation: Gradient.Vertical
                                GradientStop { position: 0.0; color: c_accent }
                                GradientStop { position: 1.0; color: c_accentD }
                            }
                            Rectangle { anchors.fill: parent; anchors.margins: -8; radius: 24; color: c_accent; opacity: 0.18; z: -1 }
                            Label { anchors.centerIn: parent; text: "▶"; color: c_bgDeep; font.bold: true; font.pixelSize: 22 }
                        }
                        ColumnLayout {
                            spacing: 2
                            Label {
                                text: "PURE IPTV"
                                color: c_text; font.bold: true; font.pixelSize: fsHeader + 8
                                font.letterSpacing: 1.5
                            }
                            Label {
                                text: "Aurora Edition • Смотрите телевизор безупречно"
                                color: c_text2; font.pixelSize: fsSub
                            }
                        }
                        Item { Layout.fillWidth: true }

                        // кнопка «добавить»
                        Rectangle {
                            width: addBtnTxt.implicitWidth + 60; height: 46; radius: 23
                            gradient: Gradient {
                                orientation: Gradient.Horizontal
                                GradientStop { position: 0.0; color: c_accent }
                                GradientStop { position: 1.0; color: c_accent2 }
                            }
                            Rectangle { anchors.fill: parent; anchors.margins: -10; radius: 33; color: c_accent; opacity: addBtnMa.containsMouse ? 0.30 : 0.12 }
                            Behavior on opacity { NumberAnimation { duration: 180 } }
                            scale: addBtnMa.containsPress ? 0.97 : 1.0
                            Behavior on scale { NumberAnimation { duration: 120 } }
                            MouseArea {
                                id: addBtnMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: stack.push(addPlaylistPage)
                            }
                            RowLayout {
                                anchors.centerIn: parent; spacing: 9
                                Label { text: "+"; color: c_bgDeep; font.bold: true; font.pixelSize: 22 }
                                Label { id: addBtnTxt; text: "Новый плейлист"; color: c_bgDeep; font.bold: true; font.pixelSize: fsBody }
                            }
                        }
                    }

                    // стат-полоса
                    RowLayout {
                        Layout.fillWidth: true; spacing: 22; Layout.topMargin: 8
                        Repeater {
                            model: [
                                { k: backend.playlists.length, v: "Плейлистов" },
                                { k: "4K", v: "Качество" },
                                { k: "MPV", v: "Движок" }
                            ]
                            RowLayout {
                                spacing: 8
                                Label { text: modelData.k; color: c_accent; font.bold: true; font.pixelSize: fsTitle }
                                Label { text: modelData.v; color: c_text3; font.pixelSize: fsSub }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            // --- ПУСТОЕ СОСТОЯНИЕ ---
            ColumnLayout {
                anchors.centerIn: parent
                spacing: 26
                width: Math.min(560, parent.width - 60)
                visible: backend.playlists.length === 0

                Item {
                    Layout.alignment: Qt.AlignHCenter
                    width: 150; height: 150
                    Rectangle { anchors.centerIn: parent; width: 150; height: 150; radius: 75; color: c_accent; opacity: 0.10 }
                    Rectangle { anchors.centerIn: parent; width: 116; height: 116; radius: 58; color: c_surface; border.color: c_accent; border.width: 1.5; opacity: 0.9 }
                    Label { anchors.centerIn: parent; text: "📺"; font.pixelSize: 60 }
                    SequentialAnimation on opacity { loops: Animation.Infinite; NumberAnimation { to: 0.55; duration: 1400 } NumberAnimation { to: 1.0; duration: 1400 } }
                }
                ColumnLayout {
                    Layout.alignment: Qt.AlignHCenter; spacing: 8
                    Label {
                        text: "Добро пожаловать!"
                        color: c_text; font.bold: true; font.pixelSize: fsHeader + 4
                        Layout.alignment: Qt.AlignHCenter; horizontalAlignment: Text.AlignHCenter
                    }
                    Label {
                        text: "Здесь пока пусто. Добавьте ваш первый M3U, Xtream или Stalker\nплейлист, чтобы начать смотреть любимые каналы."
                        color: c_text2; font.pixelSize: fsBody
                        horizontalAlignment: Text.AlignHCenter; Layout.alignment: Qt.AlignHCenter
                        wrapMode: Text.WordWrap; Layout.fillWidth: true
                    }
                }
                Rectangle {
                    Layout.alignment: Qt.AlignHCenter
                    width: heroEmptyTxt.implicitWidth + 64; height: 56; radius: 28
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: c_accent }
                        GradientStop { position: 1.0; color: c_accent2 }
                    }
                    Rectangle { anchors.fill: parent; anchors.margins: -12; radius: 40; color: c_accent; opacity: emptyHeroMa.containsMouse ? 0.28 : 0.12 }
                    scale: emptyHeroMa.containsPress ? 0.97 : 1.0
                    Behavior on scale { NumberAnimation { duration: 120 } }
                    MouseArea { id: emptyHeroMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: stack.push(addPlaylistPage) }
                    Label { id: heroEmptyTxt; anchors.centerIn: parent; text: "＋  ДОБАВИТЬ ПЛЕЙЛИСТ"; color: c_bgDeep; font.bold: true; font.pixelSize: fsTitle }
                }
                Label {
                    text: "Подсказка: F1–F4 на ПК переключают режимы Смартфон / Планшет / ПК / ТВ"
                    color: c_text3; font.pixelSize: fsSub - 1
                    horizontalAlignment: Text.AlignHCenter; Layout.alignment: Qt.AlignHCenter
                }
            }

            // --- СЕТКА ПЛЕЙЛИСТОВ ---
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 34
                spacing: 18
                visible: backend.playlists.length > 0

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        text: "Ваши плейлисты"
                        color: c_text2; font.bold: true; font.pixelSize: fsTitle
                        Layout.fillWidth: true
                    }
                    Label {
                        text: "Стрелки / Enter — навигация с пульта"
                        color: c_text3; font.pixelSize: fsSub
                    }
                }

                GridView {
                    id: plistGrid
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    cellWidth: isUltraWide ? width / 3 : (isWide ? width / 2 : width)
                    cellHeight: 168 * scaleFactor
                    clip: true
                    model: backend.playlists
                    focus: true

                    delegate: Item {
                        width: plistGrid.cellWidth - 18
                        height: 150 * scaleFactor

                        Rectangle {
                            id: plCard
                            anchors.fill: parent
                            radius: 20
                            color: {
                                if ((plistGrid.currentIndex === index && plistGrid.activeFocus)) return c_surface3
                                if (playlistMouseArea.hovered) return c_surface2
                                return c_surface
                            }
                            border.color: {
                                if (plistGrid.currentIndex === index && plistGrid.activeFocus) return c_accent
                                if (playlistMouseArea.hovered) return Qt.rgba(0.145, 0.902, 0.643, 0.5)
                                return c_borderSoft
                            }
                            border.width: (plistGrid.currentIndex === index && plistGrid.activeFocus) ? 2 : 1
                            Behavior on color { ColorAnimation { duration: 160 } }
                            Behavior on border.color { ColorAnimation { duration: 160 } }

                            // мягкое свечение при фокусе
                            Rectangle { anchors.fill: parent; anchors.margins: -6; radius: 26; color: c_accent; opacity: (plistGrid.currentIndex === index && plistGrid.activeFocus) ? 0.12 : 0.0; z: -1; Behavior on opacity { NumberAnimation { duration: 160 } } }

                            // верхняя градиентная полоска
                            Rectangle {
                                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                                height: 3; radius: 3
                                gradient: Gradient {
                                    orientation: Gradient.Horizontal
                                    GradientStop { position: 0.0; color: c_accent }
                                    GradientStop { position: 1.0; color: c_info }
                                }
                                opacity: (plistGrid.currentIndex === index && plistGrid.activeFocus) || playlistMouseArea.hovered ? 1.0 : 0.0
                                Behavior on opacity { NumberAnimation { duration: 160 } }
                            }

                            MouseArea {
                                id: playlistMouseArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: { plistGrid.currentIndex = index; enterPlaylist() }
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 18
                                spacing: 16

                                Rectangle {
                                    width: 56; height: 56; radius: 16
                                    color: {
                                        var p = modelData.proto
                                        if (p === "XTREAM") return Qt.rgba(0.357, 0.616, 1.0, 0.16)
                                        if (p === "STALKER") return Qt.rgba(0.706, 0.482, 1.0, 0.16)
                                        return Qt.rgba(0.145, 0.902, 0.643, 0.16)
                                    }
                                    border.color: {
                                        var p = modelData.proto
                                        if (p === "XTREAM") return c_info
                                        if (p === "STALKER") return "#B47BFF"
                                        return c_accent
                                    }
                                    border.width: 1
                                    Label {
                                        anchors.centerIn: parent
                                        text: modelData.proto === "M3U" ? "📝" : (modelData.proto === "XTREAM" ? "⚡" : "🧬")
                                        font.pixelSize: 26
                                    }
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 5
                                    Label {
                                        text: modelData.name
                                        font.bold: true; font.pixelSize: fsTitle + 1
                                        color: c_text; elide: Text.ElideRight; Layout.fillWidth: true
                                    }
                                    Label {
                                        text: modelData.proto + "  •  " + (modelData.host.length > 28 ? modelData.host.substring(0, 28) + "…" : modelData.host)
                                        font.pixelSize: fsSub; color: c_text3; elide: Text.ElideRight; Layout.fillWidth: true
                                    }
                                }

                                IconButton {
                                    text: "🗑"
                                    Layout.alignment: Qt.AlignTopRight | Qt.AlignRight
                                    z: 10
                                    accentColor: c_danger
                                    onClicked: {
                                        deleteConfirmDialog.targetId = modelData.id
                                        deleteConfirmDialog.targetName = modelData.name
                                        deleteConfirmDialog.open()
                                    }
                                }
                            }
                        }

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

    // =======================================================================
    // 2. ДОБАВЛЕНИЕ ПЛЕЙЛИСТА
    // =======================================================================
    Component {
        id: addPlaylistPage
        Page {
            objectName: "addPlaylistPage"
            background: Rectangle { color: "transparent" }

            Connections {
                target: backend
                function onLoadFinished() { loadingOverlay.visible = false; stack.pop() }
                function onLoadFailed(errorMsg) {
                    loadingOverlay.visible = false
                    errorDialogText.text = "Причина: " + errorMsg
                    errorDialog.open()
                }
            }

            header: ToolBar {
                height: 62
                background: Rectangle { color: c_bgDeep; Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: c_border } }
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 18; anchors.rightMargin: 18
                    Button { text: "‹  Назад"; flat: true; font.pixelSize: fsBody; onClicked: stack.pop() }
                    Label { text: "Новый плейлист"; font.bold: true; font.pixelSize: fsTitle; Layout.fillWidth: true }
                }
            }

            ScrollView {
                anchors.fill: parent
                contentWidth: availableWidth

                ColumnLayout {
                    width: Math.min(620, parent.width - 48)
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top; anchors.topMargin: 36
                    spacing: 22

                    // Сегментированный выбор источника
                    Rectangle {
                        Layout.fillWidth: true; height: 52; radius: 14
                        color: c_surface
                        border.color: c_borderSoft; border.width: 1
                        RowLayout {
                            id: segRow
                            anchors.fill: parent; anchors.margins: 5; spacing: 5
                            Repeater {
                                model: [
                                    { i: 0, icon: "📝", label: "M3U" },
                                    { i: 1, icon: "⚡", label: "Xtream" },
                                    { i: 2, icon: "🧬", label: "Stalker" }
                                ]
                                Rectangle {
                                    id: segItem
                                    Layout.fillWidth: true; Layout.fillHeight: true; radius: 10
                                    property bool isSel: ptabs.currentIndex === modelData.i
                                    color: "transparent"
                                    Rectangle {
                                        anchors.fill: parent; radius: 10; visible: segItem.isSel
                                        gradient: Gradient {
                                            orientation: Gradient.Horizontal
                                            GradientStop { position: 0.0; color: c_accent }
                                            GradientStop { position: 1.0; color: c_accent2 }
                                        }
                                    }
                                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: ptabs.currentIndex = modelData.i }
                                    RowLayout {
                                        anchors.centerIn: parent; spacing: 7
                                        Label { text: modelData.icon; font.pixelSize: 14; opacity: segItem.isSel ? 1.0 : 0.6 }
                                        Label { text: modelData.label; font.bold: segItem.isSel; font.pixelSize: fsBody; color: segItem.isSel ? c_bgDeep : c_text2 }
                                    }
                                }
                            }
                        }
                    }

                    // Скрытые вкладки для логики
                    TabBar { id: ptabs; Layout.fillWidth: true; visible: false
                        TabButton { text: "M3U" } TabButton { text: "Xtream" } TabButton { text: "Stalker" } }

                    StackLayout {
                        currentIndex: ptabs.currentIndex
                        Layout.fillWidth: true; Layout.preferredHeight: 200

                        // M3U
                        ColumnLayout { spacing: 14; Layout.fillWidth: true
                            FieldLabel { text: "Ссылка или путь к M3U-файлу" }
                            RowLayout { Layout.fillWidth: true; spacing: 10
                                TextField { id: murlInput; placeholderText: "http://…/playlist.m3u8 или путь"; Layout.fillWidth: true; text: window.murlPath; onTextChanged: window.murlPath = text }
                                Button { text: "📂  Файл"; onClicked: filePicker.open() }
                            }
                        }
                        // Xtream
                        ColumnLayout { spacing: 12; Layout.fillWidth: true
                            FieldLabel { text: "Адрес сервера (Хост)" }
                            TextField { id: xhInput; placeholderText: "http://server:port"; Layout.fillWidth: true }
                            FieldLabel { text: "Учётные данные" }
                            RowLayout { Layout.fillWidth: true; spacing: 10
                                TextField { id: xuInput; placeholderText: "Логин"; Layout.fillWidth: true }
                                TextField { id: xpInput; placeholderText: "Пароль"; echoMode: TextInput.Password; Layout.fillWidth: true }
                            }
                        }
                        // Stalker
                        ColumnLayout { spacing: 12; Layout.fillWidth: true
                            FieldLabel { text: "Адрес портала (Хост)" }
                            TextField { id: shInput; placeholderText: "http://portal/stalker_portal"; Layout.fillWidth: true }
                            FieldLabel { text: "MAC-адрес" }
                            TextField { id: smInput; placeholderText: "00:1A:79:…" ; Layout.fillWidth: true }
                        }
                    }

                    FieldLabel { text: "Телепрограмма XMLTV (необязательно)" }
                    TextField { id: pepgInput; placeholderText: "http://example.com/epg.xml.gz"; Layout.fillWidth: true }

                    Rectangle {
                        Layout.fillWidth: true; height: 60; radius: 16
                        opacity: connectBtnMa.enabled ? 1.0 : 0.4
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0; color: c_accent }
                            GradientStop { position: 1.0; color: c_accent2 }
                        }
                        Rectangle { anchors.fill: parent; anchors.margins: -10; radius: 26; color: c_accent; opacity: connectBtnMa.containsMouse ? 0.25 : 0.0 }
                        scale: connectBtnMa.containsPress ? 0.985 : 1.0
                        Behavior on scale { NumberAnimation { duration: 110 } }
                        MouseArea {
                            id: connectBtnMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            enabled: pnameInput.text.trim().length > 0 &&
                                (ptabs.currentIndex === 0 ? murlInput.text.trim().length > 0 :
                                 ptabs.currentIndex === 1 ? xhInput.text.trim().length > 0 : shInput.text.trim().length > 0)
                            onClicked: {
                                var proto = "M3U", host = murlInput.text
                                if (ptabs.currentIndex === 1) { proto = "XTREAM"; host = xhInput.text }
                                else if (ptabs.currentIndex === 2) { proto = "STALKER"; host = shInput.text }
                                loadingOverlay.visible = true
                                backend.addPlaylist(pnameInput.text, proto, host, pepgInput.text, xuInput.text, xpInput.text, smInput.text)
                            }
                        }
                        RowLayout {
                            anchors.centerIn: parent; spacing: 9
                            Label { text: "⟳"; color: c_bgDeep; font.bold: true; font.pixelSize: 18 }
                            Label { text: "ПОДКЛЮЧИТЬ И СОХРАНИТЬ"; color: c_bgDeep; font.bold: true; font.pixelSize: fsTitle }
                        }
                    }

                    Label {
                        Layout.fillWidth: true; Layout.topMargin: 4
                        text: pnameInput.text.trim().length === 0 ? "Введите название плейлиста, чтобы продолжить" : ""
                        color: c_text3; font.pixelSize: fsSub; horizontalAlignment: Text.AlignHCenter
                    }

                    // Название (внизу для визуального баланса)
                    ColumnLayout { Layout.fillWidth: true; spacing: 6
                        FieldLabel { text: "Название плейлиста" }
                        TextField {
                            id: pnameInput; placeholderText: "Например: Мой провайдер"; Layout.fillWidth: true
                        }
                    }
                }
            }

            Rectangle {
                id: loadingOverlay
                anchors.fill: parent; color: Qt.rgba(5/255, 6/255, 14/255, 0.94); visible: false
                Column {
                    anchors.centerIn: parent; spacing: 24; width: parent.width * 0.8
                    BusyIndicator { running: loadingOverlay.visible; anchors.horizontalCenter: parent.horizontalCenter; implicitWidth: 84; implicitHeight: 84 }
                    Label { text: backend.status; font.pixelSize: fsTitle; font.bold: true; color: c_text; anchors.horizontalCenter: parent.horizontalCenter; horizontalAlignment: Text.AlignHCenter }
                    Button { text: "ОТМЕНИТЬ"; flat: true; font.bold: true; Material.accent: c_danger; anchors.horizontalCenter: parent.horizontalCenter
                        onClicked: { backend.cancelConnection(); loadingOverlay.visible = false } }
                }
            }
        }
    }

    // =======================================================================
    // 3. ГЛАВНЫЙ ЭКРАН (КАТЕГОРИИ + КАНАЛЫ + EPG)
    // =======================================================================
    Component {
        id: mainPage
        Page {
            id: mainPageInstance
            objectName: "mainPage"
            background: Rectangle { color: "transparent" }

            header: ToolBar {
                height: 64
                background: Rectangle { color: c_bgDeep; Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: c_border } }
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; spacing: 12
                    Button { text: "‹  Плейлисты"; flat: true; font.pixelSize: fsBody; onClicked: stack.pop() }
                    Button { text: "📁  Категории"; flat: true; visible: !window.showCategoriesSidebar; font.pixelSize: fsBody; onClicked: catDrawer.open() }
                    Label {
                        text: backend.current_playlist_name
                        font.bold: true; font.pixelSize: fsTitle; color: c_text; Layout.fillWidth: true; elide: Text.ElideRight
                    }
                    TextField {
                        id: searchBar
                        placeholderText: "🔍  Поиск канала…"
                        implicitWidth: 280 * scaleFactor
                        font.pixelSize: fsBody
                        text: window.searchQuery
                        onTextChanged: { window.searchQuery = text; mainPageInstance.refreshChannels() }
                    }
                }
            }

            function refreshChannels() {
                var list = backend.getFilteredChannels(window.activeCategory, window.searchQuery)
                window.currentFilteredList = list
                clist.model = list
            }

            Component.onCompleted: {
                refreshChannels()
                clist.forceActiveFocus()
                backend.prefetchVisibleChannels()
            }

            // Тост умного предсказания
            Rectangle {
                id: statusTip
                property string tipText: ""
                width: Math.min(480, window.width * 0.92)
                height: 44 * scaleFactor; radius: height / 2
                anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter; anchors.bottomMargin: 22 * scaleFactor
                color: Qt.rgba(8/255, 30/255, 24/255, 0.92); border.color: c_accent; border.width: 1
                visible: false; z: 999
                Rectangle { anchors.fill: parent; radius: parent.radius; color: c_accent; opacity: 0.07 }
                RowLayout {
                    anchors.centerIn: parent; spacing: 9
                    Label { text: "⚡"; font.pixelSize: 15 }
                    Label { text: statusTip.tipText; color: "#A5F3D0"; font.pixelSize: fsSub; font.bold: true; elide: Text.ElideRight }
                }
                function show(msg) { tipText = msg; visible = true; hideTimer.restart() }
                Timer { id: hideTimer; interval: 3000; onTriggered: statusTip.visible = false }
            }

            RowLayout {
                anchors.fill: parent; spacing: 0

                // --- ЛЕВО: КАТЕГОРИИ ---
                Rectangle {
                    visible: window.showCategoriesSidebar
                    Layout.fillHeight: true
                    Layout.preferredWidth: Math.round(250 * scaleFactor)
                    color: Qt.rgba(16/255, 18/255, 29/255, 0.6)
                    Rectangle { anchors.right: parent.right; height: parent.height; width: 1; color: c_borderSoft }

                    ListView {
                        id: catList
                        anchors.fill: parent; anchors.margins: 12; clip: true
                        model: backend.categories; focus: false; spacing: 4
                        KeyNavigation.right: clist
                        header: Item { width: catList.width; height: 46
                            Label { anchors.left: parent.left; anchors.leftMargin: 14; anchors.verticalCenter: parent.verticalCenter; text: "КАТЕГОРИИ"; color: c_text3; font.bold: true; font.pixelSize: fsSub - 1; font.letterSpacing: 1.2 } }

                        delegate: ItemDelegate {
                            width: catList.width; height: 46 * scaleFactor
                            background: Rectangle {
                                radius: 11; color: {
                                    if (catList.currentIndex === index && catList.activeFocus) return Qt.rgba(0.145, 0.902, 0.643, 0.14)
                                    if (window.activeCategory === modelData) return c_surface2
                                    return "transparent"
                                }
                                // индикатор-полоска
                                Rectangle { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width: 3; height: 22; radius: 1.5; color: c_accent; visible: window.activeCategory === modelData }
                                Behavior on color { ColorAnimation { duration: 140 } }
                            }
                            contentItem: Label {
                                text: modelData; leftPadding: 18
                                font.bold: window.activeCategory === modelData
                                font.pixelSize: fsBody
                                color: window.activeCategory === modelData ? c_accent : c_text
                                verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                            }
                            function selectCategory() {
                                catList.currentIndex = index
                                window.activeCategory = modelData
                                mainPageInstance.refreshChannels()
                                backend.prefetchVisibleChannels()
                            }
                            onClicked: selectCategory()
                            Keys.onReturnPressed: selectCategory()
                            Keys.onEnterPressed: selectCategory()
                        }
                    }
                }

                // --- ЦЕНТР: КАНАЛЫ ---
                ColumnLayout {
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0

                    ListView {
                        id: clist
                        Layout.fillWidth: true; Layout.fillHeight: true
                        clip: true; boundsBehavior: Flickable.StopAtBounds; focus: true; spacing: 2
                        KeyNavigation.left: window.showCategoriesSidebar ? catList : null
                        KeyNavigation.right: window.showEpgSidebar ? elist : null

                        // счётчик сверху
                        header: Item {
                            width: clist.width; height: 40
                            Label { anchors.left: parent.left; anchors.leftMargin: 22; anchors.verticalCenter: parent.verticalCenter
                                text: (window.currentFilteredList.length) + " каналов"; color: c_text3; font.pixelSize: fsSub }
                        }

                        delegate: ItemDelegate {
                            width: clist.width
                            height: window.channelIconSize + Math.round(26 * scaleFactor)
                            property bool isActive: (clist.currentIndex === index && clist.activeFocus)

                            background: Rectangle {
                                anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10
                                radius: 14
                                color: {
                                    if (parent.isActive) return c_surface2
                                    if (window.selCh === modelData) return c_surface
                                    return "transparent"
                                }
                                border.color: parent.isActive ? c_accent : (hovered ? Qt.rgba(0.145, 0.902, 0.643, 0.45) : "transparent")
                                border.width: parent.isActive ? 1.5 : 1
                                Rectangle { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width: 3; height: 30; radius: 1.5; color: c_accent; visible: parent.isActive }
                                Behavior on color { ColorAnimation { duration: 130 } }
                            }

                            RowLayout {
                                anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                                anchors.leftMargin: Math.round(22 * scaleFactor); anchors.rightMargin: Math.round(16 * scaleFactor)
                                spacing: Math.round(15 * scaleFactor)

                                // номер канала
                                Label {
                                    text: (index + 1); color: c_text3; font.pixelSize: fsSub; font.bold: true
                                    Layout.preferredWidth: 34 * scaleFactor; horizontalAlignment: Text.AlignHCenter
                                    visible: window.isWide
                                }

                                // логотип
                                Rectangle {
                                    width: window.channelIconSize; height: window.channelIconSize
                                    radius: 12; color: c_bgDeep; border.color: c_borderSoft; border.width: 1
                                    Layout.alignment: Qt.AlignVCenter; clip: true
                                    Image {
                                        id: chanLogo
                                        anchors.fill: parent; anchors.margins: 4
                                        source: (backend.disableLogos || !modelData.logo) ? "" : modelData.logo
                                        fillMode: Image.PreserveAspectFit; asynchronous: true
                                        visible: !backend.disableLogos && modelData.logo && status === Image.Ready
                                    }
                                    Label {
                                        text: "📺"; font.pixelSize: Math.round(window.channelIconSize * 0.42)
                                        anchors.centerIn: parent; opacity: 0.55
                                        visible: backend.disableLogos || !modelData.logo || chanLogo.status !== Image.Ready
                                    }
                                }

                                // имя + EPG
                                ColumnLayout {
                                    Layout.fillWidth: true; Layout.alignment: Qt.AlignVCenter; spacing: 3
                                    RowLayout { Layout.fillWidth: true; spacing: 8
                                        Label { text: modelData.name; font.bold: true; font.pixelSize: fsTitle; color: c_text; elide: Text.ElideRight; Layout.fillWidth: true }
                                        Label { visible: backend.isFavorite(modelData.id); text: "★"; color: c_gold; font.pixelSize: 12 }
                                    }
                                    Label {
                                        text: backend.getCurrentEPG(modelData.id)
                                        font.pixelSize: window.isWide ? 12 : 11; color: c_text3; elide: Text.ElideRight; Layout.fillWidth: true
                                        visible: text.length > 0 && text !== "Нет программы"
                                    }
                                }

                                IconButton {
                                    text: backend.isFavorite(modelData.id) ? "★" : "☆"
                                    accentColor: backend.isFavorite(modelData.id) ? c_gold : c_text3
                                    Layout.alignment: Qt.AlignVCenter
                                    onClicked: { clist.currentIndex = index; backend.toggleFavorite(modelData.id); mainPageInstance.refreshChannels() }
                                }
                            }

                            function selectChannel() {
                                clist.currentIndex = index
                                window.selCh = modelData
                                window.currentChIndex = index
                                backend.updateEPG(modelData.id)
                                backend.recordChannelClick(modelData)
                                backend.play(modelData.url, modelData.name, modelData.group, "")
                                var pred = backend.predictNextChannel(modelData)
                                if (pred) {
                                    var msg = pred.confidence >= 30
                                        ? ("⚡ Вероятно следующий: " + pred.name + " (" + pred.confidence + "%, " + pred.source + ")")
                                        : ("🔥 Готово к мгновенному старту: " + pred.candidates_count + " каналов")
                                    console.log(msg)
                                }
                                stack.push(playerPage)
                            }
                            onHoveredChanged: if (hovered || activeFocus) backend.prefetchChannel(modelData)
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    backend.prefetchChannel(modelData)
                                    var pred = backend.predictNextChannel(modelData)
                                    if (pred && pred.confidence >= 30) statusTip.show("⚡ Вероятно: " + pred.name)
                                    else if (pred) statusTip.show("🔥 " + pred.candidates_count + " каналов готовы")
                                }
                            }
                            onClicked: selectChannel()
                            Keys.onReturnPressed: selectChannel()
                            Keys.onEnterPressed: selectChannel()
                        }
                    }
                }

                // --- ПРАВО: EPG ---
                Rectangle {
                    visible: window.showEpgSidebar
                    Layout.fillHeight: true
                    Layout.preferredWidth: Math.round(340 * scaleFactor)
                    color: Qt.rgba(12/255, 14/255, 21/255, 0.6)
                    Rectangle { anchors.left: parent.left; height: parent.height; width: 1; color: c_borderSoft }

                    ColumnLayout {
                        anchors.fill: parent; spacing: 0
                        Rectangle {
                            Layout.fillWidth: true; height: 50; color: "transparent"
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: c_borderSoft }
                            Label { anchors.centerIn: parent; text: "ТЕЛЕПРОГРАММА"; font.bold: true; color: c_accent; font.pixelSize: fsSub + 1; font.letterSpacing: 1 }
                        }
                        ListView {
                            id: elist
                            Layout.fillWidth: true; Layout.fillHeight: true
                            model: backend ? backend.epgModel : null
                            clip: true; focus: false; spacing: 4
                            KeyNavigation.left: clist

                            delegate: ItemDelegate {
                                width: elist.width; height: 78 * scaleFactor
                                background: Rectangle {
                                    anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10
                                    radius: 11
                                    color: (elist.currentIndex === index && elist.activeFocus) ? Qt.rgba(0.145, 0.902, 0.643, 0.12) : "transparent"
                                    border.color: (elist.currentIndex === index && elist.activeFocus) ? c_accent : "transparent"
                                    border.width: 1.5
                                    Rectangle { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width: 3; height: 26; radius: 1.5; color: c_accent; opacity: 0.7 }
                                }
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 12; spacing: 4
                                    RowLayout { Layout.fillWidth: true; spacing: 8
                                        Rectangle { width: 6; height: 6; radius: 3; color: c_accent; Layout.alignment: Qt.AlignVCenter }
                                        Label { text: model.displayTime; color: c_accent; font.bold: true; font.pixelSize: fsSub }
                                    }
                                    Label { text: model.displayTitle; color: c_text; elide: Text.ElideRight; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: fsBody; maximumLineCount: 2 }
                                    Label { text: model.desc; color: c_text3; elide: Text.ElideRight; Layout.fillWidth: true; font.pixelSize: fsSub - 1; visible: text.length > 0; maximumLineCount: 1 }
                                }
                                function selectEpgItem() {
                                    elist.currentIndex = index
                                    var archUrl = backend.getArchiveUrl(window.selCh.url, model.startRaw)
                                    window.selCh = { "id": window.selCh.id, "name": window.selCh.name, "logo": window.selCh.logo, "group": window.selCh.group, "url": archUrl }
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

            // Drawer категорий (мобильные)
            Drawer {
                id: catDrawer
                width: Math.min(320, parent.width * 0.82); height: parent.height; edge: Qt.LeftEdge
                background: Rectangle { color: c_surface; radius: 0 }
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 18; spacing: 14
                    Label { text: "КАТЕГОРИИ"; font.bold: true; font.pixelSize: fsHeader; color: c_accent; Layout.alignment: Qt.AlignHCenter }
                    ListView {
                        id: catDrawerList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; model: backend.categories; spacing: 4
                        delegate: ItemDelegate {
                            width: catDrawerList.width; height: 48 * scaleFactor
                            background: Rectangle {
                                radius: 11; color: window.activeCategory === modelData ? Qt.rgba(0.145, 0.902, 0.643, 0.14) : "transparent"
                                Rectangle { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width: 3; height: 22; radius: 1.5; color: c_accent; visible: window.activeCategory === modelData }
                            }
                            contentItem: Label { text: modelData; leftPadding: 16; font.bold: window.activeCategory === modelData; font.pixelSize: fsBody; color: window.activeCategory === modelData ? c_accent : c_text; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                            onClicked: { window.activeCategory = modelData; mainPageInstance.refreshChannels(); catDrawer.close() }
                        }
                    }
                }
            }
        }
    }

    // =======================================================================
    // 4. ФОН ПЛЕЕРА (POD MPV)
    // =======================================================================
    Component {
        id: playerPage
        Page {
            id: proot
            objectName: "playerPage"
            background: Rectangle { color: "black" }

            // Экран загрузки / заставка канала
            ColumnLayout {
                anchors.centerIn: parent; spacing: 22; visible: busyIndicator.visible
                Rectangle {
                    width: 124 * scaleFactor; height: 124 * scaleFactor; radius: 20; color: c_surface
                    border.color: c_accent; border.width: 1.5; Layout.alignment: Qt.AlignHCenter
                    Rectangle { anchors.fill: parent; color: c_accent; opacity: 0.06; radius: 20 }
                    Image {
                        id: playerLogo
                        anchors.fill: parent; anchors.margins: 14
                        source: (backend.disableLogos || !window.selCh || !window.selCh.logo) ? "" : window.selCh.logo
                        fillMode: Image.PreserveAspectFit
                        visible: !backend.disableLogos && window.selCh && window.selCh.logo && status === Image.Ready
                    }
                    Label { text: "📺"; font.pixelSize: 56 * scaleFactor; anchors.centerIn: parent; visible: backend.disableLogos || !window.selCh || !window.selCh.logo || playerLogo.status !== Image.Ready }
                    SequentialAnimation on scale { loops: Animation.Infinite; running: busyIndicator.visible; NumberAnimation { to: 0.94; duration: 900; easing.type: Easing.InOutSine } NumberAnimation { to: 1.0; duration: 900; easing.type: Easing.InOutSine } }
                }
                Label { text: window.selCh ? window.selCh.name : "Загрузка трансляции…"; font.bold: true; font.pixelSize: fsHeader; color: c_text; Layout.alignment: Qt.AlignHCenter }
                Label {
                    text: backend.isBuffering ? "Слабый сигнал — буферизация " + backend.bufferingProgress + "%…" : "Инициализация видеопотока MPV…"
                    font.pixelSize: fsBody; color: backend.isBuffering ? c_warn : c_text2; Layout.alignment: Qt.AlignHCenter
                }
            }
            BusyIndicator {
                id: busyIndicator
                anchors.centerIn: parent; anchors.verticalCenterOffset: 135 * scaleFactor
                width: 76 * scaleFactor; height: 76 * scaleFactor; running: true; visible: true
            }
            Timer { id: hideBusyTimer; interval: 5000; onTriggered: { busyIndicator.visible = false; busyIndicator.running = false } }

            Connections {
                target: backend
                function onPlayingChanged(playing) {
                    if (playing && !backend.isBuffering) { busyIndicator.visible = false; busyIndicator.running = false; hideBusyTimer.stop() }
                    else { busyIndicator.visible = true; busyIndicator.running = true }
                }
                function onBufferingChanged() {
                    busyIndicator.visible = backend.isBuffering
                    busyIndicator.running = backend.isBuffering
                }
            }
            onVisibleChanged: if (visible) { busyIndicator.visible = true; busyIndicator.running = true; hideBusyTimer.start() }
        }
    }

    // =======================================================================
    // 5. ПРОЗРАЧНОЕ ОВЕРЛЕЙ-ОКНО OSD (поверх MPV — решает airspace на Windows)
    // =======================================================================
    Window {
        id: playerOsdWindow
        visible: window.visible && (Qt.application.state === Qt.ApplicationActive) && (stack.currentItem && stack.currentItem.objectName === "playerPage")
        color: "transparent"
        flags: Qt.FramelessWindowHint | Qt.Dialog
        x: window.x; y: window.y; width: window.width; height: window.height

        Item {
            id: prootOsd
            anchors.fill: parent
            focus: true

            // ---- D-Pad / клавиатура ТВ-пульта ----
            Keys.onUpPressed: { backend.volume = Math.min(100, backend.volume + 5); showOsdTemporarily() }
            Keys.onDownPressed: { backend.volume = Math.max(0, backend.volume - 5); showOsdTemporarily() }
            Keys.onLeftPressed: { prootOsd.playPrevChannel(); showOsdTemporarily() }
            Keys.onRightPressed: { prootOsd.playNextChannel(); showOsdTemporarily() }
            Keys.onReturnPressed: { backend.togglePause(); showOsdTemporarily() }
            Keys.onEnterPressed: { backend.togglePause(); showOsdTemporarily() }
            Keys.onPressed: {
                if (event.key === Qt.Key_R) { if (window.selCh) backend.play(window.selCh.url, window.selCh.name, window.selCh.group, ""); event.accepted = true }
            }

            function showOsdTemporarily() { topOsdBar.opacity = 1; bottomOsdBar.opacity = 1; osdTimer.restart() }
            function playNextChannel() {
                if (window.currentFilteredList.length > 0 && window.currentChIndex !== -1) {
                    var i = (window.currentChIndex + 1) % window.currentFilteredList.length
                    window.currentChIndex = i
                    var ch = window.currentFilteredList[i]
                    window.selCh = ch; backend.updateEPG(ch.id); backend.play(ch.url, ch.name, ch.group, "")
                }
            }
            function playPrevChannel() {
                if (window.currentFilteredList.length > 0 && window.currentChIndex !== -1) {
                    var i = window.currentChIndex - 1; if (i < 0) i = window.currentFilteredList.length - 1
                    window.currentChIndex = i
                    var ch = window.currentFilteredList[i]
                    window.selCh = ch; backend.updateEPG(ch.id); backend.play(ch.url, ch.name, ch.group, "")
                }
            }

            MouseArea {
                anchors.fill: parent; hoverEnabled: true
                onClicked: {
                    osdTimer.restart()
                    topOsdBar.opacity = topOsdBar.opacity > 0 ? 0 : 1
                    bottomOsdBar.opacity = bottomOsdBar.opacity > 0 ? 0 : 1
                }
                onPositionChanged: { topOsdBar.opacity = 1; bottomOsdBar.opacity = 1; osdTimer.restart() }
            }

            Timer {
                id: osdTimer; interval: 4000; running: true
                onTriggered: { topOsdBar.opacity = 0; bottomOsdBar.opacity = 0 }
            }

            // ---- ВЕРХНИЙ БАР ----
            Rectangle {
                id: topOsdBar
                anchors.top: parent.top; width: parent.width; height: 78 * scaleFactor
                visible: opacity > 0
                color: Qt.rgba(5/255, 6/255, 14/255, 0.82)
                opacity: 1
                Behavior on opacity { NumberAnimation { duration: 220 } }
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: c_border; opacity: 0.6 }

                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20; spacing: 14

                    Rectangle {
                        width: backTxt.implicitWidth + 42; height: 40; radius: 20; color: "transparent"
                        border.color: backMa.containsMouse ? c_accent : c_border; border.width: 1
                        Behavior on border.color { ColorAnimation { duration: 140 } }
                        MouseArea { id: backMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { backend.stop(); stack.pop() } }
                        RowLayout { anchors.centerIn: parent; spacing: 7
                            Label { text: "‹"; color: c_text; font.bold: true; font.pixelSize: 18 }
                            Label { id: backTxt; text: "КАНАЛЫ"; color: c_text; font.bold: true; font.pixelSize: fsBody } }
                    }

                    // имя канала
                    RowLayout { spacing: 10; Layout.fillWidth: true
                        Label { text: window.selCh ? window.selCh.name : "Загрузка…"; font.bold: true; font.pixelSize: fsHeader; color: c_text; elide: Text.ElideRight; Layout.maximumWidth: 380 * scaleFactor }
                        Rectangle { visible: backend.duration === 0; width: 50; height: 20; radius: 5; color: c_live
                            Label { anchors.centerIn: parent; text: "LIVE"; color: "white"; font.bold: true; font.pixelSize: 10 } }
                    }

                    // индикатор сигнала
                    RowLayout { spacing: 7; Layout.alignment: Qt.AlignVCenter
                        Label {
                            text: { var q = backend.connectionQuality
                                if (q === "excellent") return "●●●"; if (q === "good") return "●●●"; if (q === "fair") return "●●"; if (q === "poor") return "●"; return "●●●" }
                            color: { var q = backend.connectionQuality
                                if (q === "excellent" || q === "good") return c_accent; if (q === "fair") return c_warn; return c_danger }
                            font.pixelSize: 11
                        }
                        Label { text: { var q = backend.connectionQuality; return q === "excellent" ? "Отлично" : q === "good" ? "Хорошо" : q === "fair" ? "Средне" : q === "poor" ? "Плохо" : "—" }
                            color: c_text2; font.pixelSize: fsSub - 1; Layout.preferredWidth: 64; elide: Text.ElideRight }
                    }

                    // качество
                    Rectangle {
                        width: qTxt.implicitWidth + 44; height: 34; radius: 8; color: c_surface2; border.color: c_borderSoft; border.width: 1
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: qualityMenu.open() }
                        RowLayout { anchors.centerIn: parent; spacing: 6
                            Label { text: "📺"; font.pixelSize: 12 }
                            Label { id: qTxt; text: { var q = backend.currentQuality
                                if (q === "ultra") return "4K"; if (q === "high") return "1080p"; if (q === "medium") return "720p"; if (q === "low") return "480p"; if (q === "minimal") return "360p"; return "AUTO" }
                                color: c_accent; font.bold: true; font.pixelSize: fsSub } }
                        Menu {
                            id: qualityMenu
                            MenuItem { text: "🔄 Авто (рекомендуется)"; onTriggered: backend.setQuality("auto") }
                            MenuItem { text: "📺 4K Ultra HD"; onTriggered: backend.setQuality("ultra"); enabled: Array.from(backend.availableQualities).indexOf("ultra") !== -1 }
                            MenuItem { text: "📺 1080p Full HD"; onTriggered: backend.setQuality("high"); enabled: Array.from(backend.availableQualities).indexOf("high") !== -1 }
                            MenuItem { text: "📺 720p HD"; onTriggered: backend.setQuality("medium"); enabled: Array.from(backend.availableQualities).indexOf("medium") !== -1 }
                            MenuItem { text: "📺 480p"; onTriggered: backend.setQuality("low"); enabled: Array.from(backend.availableQualities).indexOf("low") !== -1 }
                            MenuItem { text: "📺 360p"; onTriggered: backend.setQuality("minimal"); enabled: Array.from(backend.availableQualities).indexOf("minimal") !== -1 }
                        }
                    }

                    // формат экрана
                    Rectangle {
                        width: 34; height: 34; radius: 8; color: c_surface2; border.color: c_borderSoft; border.width: 1
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
                            if (window.currentAspect === "no") window.currentAspect = "16:9"
                            else if (window.currentAspect === "16:9") window.currentAspect = "4:3"
                            else if (window.currentAspect === "4:3") window.currentAspect = "stretch"
                            else window.currentAspect = "no"
                            backend.setAspectRatio(window.currentAspect) } }
                        Label { anchors.centerIn: parent; text: "⬔"; color: c_text2; font.pixelSize: 16 }
                    }

                    // экономия трафика
                    Rectangle {
                        width: 34; height: 34; radius: 8
                        color: backend.forceLowestVariant ? Qt.rgba(0.145, 0.902, 0.643, 0.16) : c_surface2
                        border.color: backend.forceLowestVariant ? c_accent : c_borderSoft; border.width: 1
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: saverMenu.open() }
                        Label { anchors.centerIn: parent; text: "💰"; font.pixelSize: 14 }
                        Menu {
                            id: saverMenu
                            MenuItem { text: (backend.forceLowestVariant ? "✅ " : "⬜ ") + "Самый низкий битрейт в HLS"; onTriggered: backend.forceLowestVariant = !backend.forceLowestVariant }
                            MenuItem { text: (backend.disableLogos ? "✅ " : "⬜ ") + "Не качать логотипы"; onTriggered: backend.disableLogos = !backend.disableLogos }
                            MenuItem { text: (backend.skipCountryDetect ? "✅ " : "⬜ ") + "Не определять страну"; onTriggered: backend.skipCountryDetect = !backend.skipCountryDetect }
                            MenuSeparator {}
                            MenuItem { text: "ℹ️ Кэш: 60 с / 200 МБ"; enabled: false }
                        }
                    }
                }
            }

            // ---- НИЖНИЙ БАР ----
            Rectangle {
                id: bottomOsdBar
                anchors.bottom: parent.bottom; width: parent.width; height: 168 * scaleFactor
                visible: opacity > 0
                color: Qt.rgba(5/255, 6/255, 14/255, 0.84)
                opacity: 1
                Behavior on opacity { NumberAnimation { duration: 220 } }
                Rectangle { anchors.top: parent.top; width: parent.width; height: 1; color: c_border; opacity: 0.6 }

                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 18; spacing: 9

                    // бейджи статуса
                    RowLayout { Layout.fillWidth: true; spacing: 12
                        Rectangle { visible: backend.duration === 0; width: 64; height: 22; radius: 5; color: (backend.isPaused || backend.isBuffering) ? "#303240" : c_live
                            RowLayout { anchors.centerIn: parent; spacing: 5
                                Rectangle { width: 6; height: 6; radius: 3; color: (backend.isPaused || backend.isBuffering) ? c_text2 : "white"
                                    SequentialAnimation on color { loops: Animation.Infinite; running: !backend.isPaused && !backend.isBuffering && backend.duration === 0; ColorAnimation { to: "transparent"; duration: 800 } ColorAnimation { to: "white"; duration: 800 } } }
                                Label { text: "LIVE"; color: "white"; font.bold: true; font.pixelSize: 10 } } }
                        Rectangle { visible: backend.isBuffering; width: 150; height: 22; radius: 5; color: Qt.rgba(0.106, 0.371, 0.122, 1.0)
                            RowLayout { anchors.centerIn: parent; spacing: 6
                                Label { text: "📡 БУФЕР"; color: "#A5F3D0"; font.bold: true; font.pixelSize: 10 }
                                Label { text: backend.bufferingProgress + "%"; color: "white"; font.bold: true; font.pixelSize: 11 } } }
                        Label { text: backend.status; font.pixelSize: fsSub - 1; color: c_text2; elide: Text.ElideRight; Layout.maximumWidth: 260 * scaleFactor }
                        Item { Layout.fillWidth: true }
                        // текущая программа
                        Label { text: window.selCh ? backend.getCurrentEPG(window.selCh.id) : ""; font.pixelSize: fsSub; color: c_text2; elide: Text.ElideRight; Layout.maximumWidth: 320 * scaleFactor }
                    }

                    // программа крупно
                    Label {
                        text: window.selCh ? backend.getCurrentEPG(window.selCh.id) : "Программа недоступна"
                        font.pixelSize: fsTitle; color: c_text; font.bold: true; Layout.alignment: Qt.AlignHCenter; elide: Text.ElideRight; Layout.maximumWidth: parent.width * 0.7
                    }

                    // скроббер
                    RowLayout { Layout.fillWidth: true; spacing: 14
                        Label { text: formatTime(backend.position); color: c_text2; font.pixelSize: fsSub; Layout.preferredWidth: 52; horizontalAlignment: Text.AlignRight }
                        Slider { id: progressSlider; Layout.fillWidth: true; from: 0; to: backend.duration > 0 ? backend.duration : 100; value: backend.position; onMoved: backend.position = value }
                        Label { text: formatTime(backend.duration); color: c_text2; font.pixelSize: fsSub; Layout.preferredWidth: 52 }
                    }

                    // управление
                    RowLayout { Layout.fillWidth: true; spacing: 18
                        IconButton { text: "⏮"; onClicked: prootOsd.playPrevChannel() }
                        Rectangle {
                            width: 52; height: 52; radius: 26
                            gradient: Gradient { orientation: Gradient.Vertical; GradientStop { position: 0.0; color: c_accent } GradientStop { position: 1.0; color: c_accent2 } }
                            Rectangle { anchors.fill: parent; anchors.margins: -8; radius: 34; color: c_accent; opacity: playMa.containsMouse ? 0.3 : 0.0 }
                            scale: playMa.containsPress ? 0.94 : 1.0; Behavior on scale { NumberAnimation { duration: 110 } }
                            MouseArea { id: playMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: backend.togglePause() }
                            Label { anchors.centerIn: parent; text: backend.isPaused ? "▶" : "⏸"; color: c_bgDeep; font.pixelSize: 20 }
                        }
                        IconButton { text: "⏭"; onClicked: prootOsd.playNextChannel() }

                        Item { Layout.fillWidth: true }

                        Label { text: backend.connectionQuality === "excellent" || backend.connectionQuality === "good" ? "🟢" : backend.connectionQuality === "fair" ? "🟡" : "🔴"; font.pixelSize: 18 }

                        RowLayout { spacing: 8
                            Label { text: backend.volume === 0 ? "🔇" : "🔊"; font.pixelSize: fsTitle }
                            Slider { id: volSlider; from: 0; to: 100; value: backend.volume; implicitWidth: 110 * scaleFactor; onMoved: backend.volume = value }
                        }
                    }
                }
            }
        }
    }

    // =======================================================================
    // МЕЛКИЕ КОМПОНЕНТЫ
    // =======================================================================
    component IconButton : Button {
        id: iconBtn
        property color accentColor: c_text2
        implicitWidth: 44 * scaleFactor; implicitHeight: 44 * scaleFactor
        flat: true; padding: 0
        contentItem: Text {
            text: iconBtn.text; font.pixelSize: 18 * scaleFactor
            color: iconBtn.hovered ? c_accent : iconBtn.accentColor
            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            Behavior on color { ColorAnimation { duration: 130 } }
        }
        background: Rectangle {
            color: iconBtn.hovered ? Qt.rgba(0.145, 0.902, 0.643, 0.12) : "transparent"
            radius: width / 2; border.color: iconBtn.pressed ? c_accent : "transparent"; border.width: 1
            Behavior on color { ColorAnimation { duration: 130 } }
        }
    }

    component FieldLabel : Label {
        color: c_text2; font.bold: true; font.pixelSize: fsSub
        Layout.fillWidth: true; topPadding: 2
    }
}

