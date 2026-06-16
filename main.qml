import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: window
    width: Screen.width
    height: Screen.height
    visible: true
    title: "Pure IPTV Premium"

    Material.theme: Material.Dark
    Material.accent: "#00E676"

    readonly property bool isWide: width > 1000
    property var selCh: null

    background: Rectangle {
        color: "#000000"
    }

    Connections {
        target: backend
        function onLoadFinished() {
            stack.push(mainPage)
        }
    }

    StackView {
        id: stack
        anchors.fill: parent
        initialItem: loginPage
    }

    Component {
        id: loginPage
        ScrollView {
            contentWidth: availableWidth
            ColumnLayout {
                width: window.width
                spacing: 20
                anchors.margins: 25

                Label {
                    text: "PURE IPTV PREMIUM"
                    font.pixelSize: 36
                    font.bold: true
                    color: "#00E676"
                    Layout.alignment: Qt.AlignHCenter
                }

                TabBar {
                    id: tabs
                    Layout.fillWidth: true
                    TabButton { text: "M3U" }
                    TabButton { text: "XTREAM" }
                    TabButton { text: "STALKER" }
                }

                TextField {
                    id: hin
                    placeholderText: "Server URL"
                    Layout.fillWidth: true
                    color: "white"
                }

                TextField {
                    id: ein
                    placeholderText: "EPG XMLTV (.gz)"
                    Layout.fillWidth: true
                    color: "white"
                }
                
                RowLayout {
                    visible: tabs.currentIndex > 0
                    Layout.fillWidth: true
                    TextField {
                        id: uin
                        placeholderText: "User"
                        Layout.fillWidth: true
                    }
                    TextField {
                        id: pin
                        placeholderText: "Pass/MAC"
                        Layout.fillWidth: true
                        echoMode: TextInput.Password
                    }
                }

                Button {
                    text: "УСТАНОВИТЬ СОЕДИНЕНИЕ"
                    Layout.fillWidth: true
                    height: 60
                    highlighted: true
                    onClicked: {
                        var p = "M3U"
                        if (tabs.currentIndex === 1) p = "XTREAM"
                        if (tabs.currentIndex === 2) p = "STALKER"
                        backend.connect(p, hin.text, ein.text, uin.text, pin.text, pin.text)
                    }
                }

                // ВОТ ОНА, ВЕРНУЛ НА МЕСТО!
                Button {
                    text: "ВОЙТИ В ХРАНИЛИЩЕ (OFFLINE)"
                    Layout.fillWidth: true
                    flat: true
                    onClicked: stack.push(mainPage)
                }

                Label {
                    text: backend.status
                    Layout.alignment: Qt.AlignHCenter
                    color: "#444444"
                    font.pixelSize: 11
                }
            }
        }
    }

    Component {
        id: mainPage
        RowLayout {
            spacing: 0
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Rectangle {
                    Layout.fillWidth: true
                    height: 60
                    color: "#111111"
                    Label {
                        anchors.centerIn: parent
                        text: "КАНАЛЫ ЭФИРА"
                        font.bold: true
                    }
                }
                ListView {
                    id: clist
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: backend.channels
                    delegate: ItemDelegate {
                        width: clist.width
                        height: 80
                        background: Rectangle {
                            color: selCh === modelData ? "#1A1A1A" : "transparent"
                        }
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 15
                            Rectangle {
                                width: 60
                                height: 60
                                color: "#000"
                                radius: 8
                                Image {
                                    anchors.fill: parent
                                    source: modelData.logo || ""
                                    fillMode: Image.PreserveAspectFit
                                    asynchronous: true
                                }
                            }
                            Label {
                                text: modelData.name
                                font.bold: true
                                Layout.fillWidth: true
                                color: "white"
                                elide: Text.ElideRight
                            }
                        }
                        onClicked: {
                            selCh = modelData
                            backend.updateEPG(modelData.id)
                            if (!window.isWide) edrawer.open()
                        }
                    }
                }
            }
            Rectangle {
                visible: window.isWide
                Layout.fillHeight: true
                Layout.preferredWidth: 420
                color: "#080808"
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0
                    Rectangle {
                        Layout.fillWidth: true
                        height: 60
                        color: "#111"
                        Label {
                            anchors.centerIn: parent
                            text: "ПРОГРАММА / АРХИВ"
                            color: "#00E676"
                        }
                    }
                    ListView {
                        id: elist
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        model: backend.epgModel
                        clip: true
                        delegate: ItemDelegate {
                            width: elist.width
                            height: 75
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 10
                                Label { text: model.displayTime; color: "#00E676"; font.bold: true }
                                Label { text: model.displayTitle; color: "white"; elide: Text.ElideRight; Layout.fillWidth: true }
                            }
                            onClicked: {
                                var wid = int(window.winId)
                                backend.play(selCh.url, wid, model.startRaw)
                                stack.push(playerPage)
                            }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: playerPage
        Rectangle {
            id: proot
            color: "black"
            focus: true
            Item {
                anchors.fill: parent
                MouseArea {
                    anchors.fill: parent
                    onClicked: osd.visible = !osd.visible
                }
            }
            BusyIndicator {
                anchors.centerIn: parent
                running: true
            }
            Rectangle {
                id: osd
                anchors.bottom: parent.bottom
                width: parent.width
                height: 160
                color: "#CC000000"
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 25
                    Label {
                        text: selCh ? selCh.name : ""
                        font.bold: true
                        color: "#00E676"
                        font.pixelSize: 22
                    }
                    RowLayout {
                        Button {
                            text: "ВЫХОД"
                            highlighted: true
                            onClicked: {
                                backend.stop()
                                stack.pop()
                            }
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: "EPG"
                            onClicked: edrawer.open()
                            visible: !window.isWide
                        }
                    }
                }
            }
            Keys.onBackPressed: {
                backend.stop()
                stack.pop()
            }
        }
    }

    Drawer {
        id: edrawer
        width: parent.width
        height: parent.height * 0.8
        edge: Qt.BottomEdge
        background: Rectangle {
            color: "#080808"
            radius: 25
        }
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 15
            Label {
                text: "ПРОГРАММА ПЕРЕДАЧ"
                font.bold: true
                Layout.alignment: Qt.AlignHCenter
            }
            ListView {
                id: mepglist
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: backend.epgModel
                clip: true
                delegate: ItemDelegate {
                    width: mepglist.width
                    height: 80
                    ColumnLayout {
                        anchors.fill: parent
                        Label { text: model.displayTime; color: "#00E676"; font.bold: true }
                        Label { text: model.displayTitle; color: "white" }
                    }
                    onClicked: {
                        var wid = int(window.winId)
                        backend.play(selCh.url, wid, model.startRaw)
                        edrawer.close()
                        if (stack.depth < 3) stack.push(playerPage)
                    }
                }
            }
        }
    }
}
