import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: window
    width: Screen.width; height: Screen.height; visible: true
    title: "Pure IPTV Premium"
    Material.theme: Material.Dark
    Material.accent: "#00E676"

    readonly property bool isWide: width > 1000
    property var selCh: null

    background: Rectangle { color: "#000" }

    Connections {
        target: backend
        function onLoadFinished() { stack.push(mainPage) }
    }

    StackView { id: stack; anchors.fill: parent; initialItem: loginPage }

    Component {
        id: loginPage
        ScrollView {
            contentWidth: availableWidth
            ColumnLayout {
                width: window.width; spacing: 20; anchors.margins: 30
                
                Label { 
                    text: "PURE IPTV PREMIUM"; font.pixelSize: 36; 
                    font.bold: true; color: "#00E676"; Layout.alignment: Qt.AlignHCenter 
                }
                
                TabBar { 
                    id: tabs; Layout.fillWidth: true
                    TabButton { text: "M3U" }
                    TabButton { text: "XTREAM" }
                    TabButton { text: "STALKER" } 
                }
                
                TextField { id: hostIn; placeholderText: "Server URL"; Layout.fillWidth: true; color: "white" }
                TextField { id: epgIn; placeholderText: "EPG XMLTV (.xml.gz)"; Layout.fillWidth: true; color: "white" }
                
                RowLayout {
                    visible: tabs.currentIndex > 0; Layout.fillWidth: true
                    TextField { id: userIn; placeholderText: "User"; Layout.fillWidth: true }
                    TextField { id: passIn; placeholderText: "Pass/MAC"; Layout.fillWidth: true; echoMode: TextInput.Password }
                }
                
                Button { 
                    text: "CONNECT"; Layout.fillWidth: true; height: 60; highlighted: true
                    onClicked: backend.connect(tabs.currentIndex==0?"M3U":tabs.currentIndex==1?"XTREAM":"STALKER", hostIn.text, epgIn.text, userIn.text, passIn.text, passIn.text) 
                }
                
                Label { text: backend.status; Layout.alignment: Qt.AlignHCenter; color: "#444" }
            }
        }
    }

    Component {
        id: mainPage
        RowLayout {
            spacing: 0
            
            ColumnLayout {
                Layout.fillWidth: true; spacing: 0
                Rectangle { 
                    Layout.fillWidth: true; height: 60; color: "#111"
                    Label { anchors.centerIn: parent; text: "CHANNELS"; font.bold: true } 
                }
                
                ListView {
                    id: clist; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; model: backend.channels
                    delegate: ItemDelegate {
                        width: clist.width; height: 80
                        background: Rectangle { color: selCh === modelData ? "#1a1a1a" : "transparent" }
                        
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 10; spacing: 15
                            Rectangle { 
                                width: 60; height: 60; color: "#000"; radius: 8
                                Image { anchors.fill: parent; source: modelData.logo || ""; fillMode: Image.PreserveAspectFit; asynchronous: true } 
                            }
                            Label { text: modelData.name; font.bold: true; Layout.fillWidth: true; color: "white"; elide: Text.ElideRight }
                        }
                        onClicked: { 
                            selCh = modelData; 
                            backend.updateEPG(modelData.id); 
                            if(!isWide) eDrawer.open() 
                        }
                    }
                }
            }

            Rectangle {
                visible: isWide; Layout.fillHeight: true; Layout.preferredWidth: 420; color: "#080808"
                ColumnLayout {
                    anchors.fill: parent; spacing: 0
                    Rectangle { 
                        Layout.fillWidth: true; height: 60; color: "#111"
                        Label { anchors.centerIn: parent; text: "EPG / ARCHIVE"; color: "#00E676" } 
                    }
                    
                    ListView { 
                        id: elist; Layout.fillWidth: true; Layout.fillHeight: true; model: backend.epgModel; clip: true
                        delegate: ItemDelegate {
                            width: elist.width; height: 75
                            ColumnLayout { 
                                anchors.fill: parent; anchors.margins: 10
                                Label { text: model.displayTime; color: "#00E676"; font.bold: true }
                                Label { text: model.title; color: "white"; elide: Text.ElideRight; Layout.fillWidth: true }
                            }
                            onClicked: { 
                                backend.play(selCh.url, int(window.winId), model.startRaw); 
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
            id: pRoot; color: "black"; focus: true
            MouseArea { anchors.fill: parent; onClicked: osd.visible = !osd.visible }
            BusyIndicator { anchors.centerIn: parent; width: 80; height: 80; running: true }
            
            Rectangle {
                id: osd; anchors.bottom: parent.bottom; width: parent.width; height: 160; color: "#CC000000"
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 25
                    Label { text: selCh ? selCh.name : ""; font.bold: true; color: "#00E676"; font.pixelSize: 22 }
                    RowLayout {
                        Button { text: "EXIT"; highlighted: true; onClicked: { backend.stop(); stack.pop() } }
                        Item { Layout.fillWidth: true }
                        Button { text: "EPG"; onClicked: eDrawer.open(); visible: !isWide }
                    }
                }
            }
            Keys.onBackPressed: { backend.stop(); stack.pop() }
            Keys.onEscapePressed: { backend.stop(); stack.pop() }
        }
    }

    Drawer { 
        id: eDrawer; width: parent.width; height: parent.height*0.8; edge: Qt.BottomEdge
        background: Rectangle { color: "#080808"; radius: 25 }
        
        ColumnLayout { 
            anchors.fill: parent; anchors.margins: 15
            Label { text: "PROGRAM GUIDE"; font.bold: true; Layout.alignment: Qt.AlignHCenter }
            
            ListView { 
                id: drawerList; Layout.fillWidth: true; Layout.fillHeight: true; model: backend.epgModel; clip: true
                delegate: ItemDelegate { 
                    width: drawerList.width; height: 80
                    ColumnLayout { 
                        anchors.fill: parent
                        Label { text: model.displayTime; color: "#00E676" }
                        Label { text: model.title; color: "white" } 
                    }
                    onClicked: { 
                        backend.play(selCh.url, int(window.winId), model.startRaw); 
                        eDrawer.close(); 
                        if(stack.depth < 3) stack.push(playerPage) 
                    } 
                } 
            }
        }
    }
}
