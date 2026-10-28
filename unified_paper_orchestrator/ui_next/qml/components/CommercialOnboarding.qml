import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var controllerRef
    property var status: controllerRef ? controllerRef.commercialStatus : ({})
    property bool codeRequested: false
    property string feedback: ""

    function tr(key) { return i18n.text(key, i18n.revision) }

    Connections {
        target: root.controllerRef
        function onCommercialActionCompleted(action, result) {
            var key = String(action || "")
            if (key === "commercial:request_code") {
                root.codeRequested = true
                if (result.development_code)
                    verificationCode.text = String(result.development_code)
                root.feedback = result.development_code
                    ? root.tr("commercial.code_ready")
                    : root.tr("commercial.code_sent")
            } else if (key === "commercial:complete_login") {
                root.feedback = root.tr("commercial.activation_complete")
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.canvas
    }

    RowLayout {
        anchors.centerIn: parent
        width: Math.min(1040, parent.width - 64)
        height: Math.min(650, parent.height - 64)
        spacing: 0

        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 410
            radius: Theme.radiusLarge
            color: Theme.primaryDark
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0; color: "#0B5EC7" }
                GradientStop { position: 1; color: "#073B84" }
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 42
                spacing: 16
                Image {
                    source: root.controllerRef ? root.controllerRef.brandLogo : ""
                    sourceSize.width: 64
                    sourceSize.height: 64
                    Layout.preferredWidth: 64
                    Layout.preferredHeight: 64
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                }
                Text {
                    Layout.fillWidth: true
                    text: root.tr("commercial.welcome_title")
                    color: "white"
                    font.family: Theme.fontFamily
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
                Text {
                    Layout.fillWidth: true
                    text: root.tr("commercial.welcome_subtitle")
                    color: "#D6E8FF"
                    font.family: Theme.fontFamily
                    font.pixelSize: 14
                    lineHeight: 1.45
                    wrapMode: Text.WordWrap
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: "#42FFFFFF"
                }
                Repeater {
                    model: [
                        root.tr("commercial.benefit_local"),
                        root.tr("commercial.benefit_pipeline"),
                        root.tr("commercial.benefit_resume"),
                        root.tr("commercial.benefit_trial")
                            .replace("{days}", String(root.status.trialDays || 14))
                    ]
                    delegate: RowLayout {
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 10
                        Rectangle {
                            width: 24
                            height: 24
                            radius: 12
                            color: "#24FFFFFF"
                            Icon {
                                anchors.centerIn: parent
                                name: "check"
                                iconSize: 14
                                iconColor: "white"
                            }
                        }
                        Text {
                            Layout.fillWidth: true
                            text: String(modelData)
                            color: "#EAF4FF"
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            wrapMode: Text.WordWrap
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Text {
                    text: root.tr("commercial.edition_label")
                          + " · v" + (root.controllerRef ? root.controllerRef.version : "")
                    color: "#AFCFF5"
                    font.family: Theme.monoFontFamily
                    font.pixelSize: 10
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Theme.radiusLarge
            color: Theme.surface
            border.width: 1
            border.color: Theme.border

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: 54
                anchors.rightMargin: 54
                anchors.topMargin: 34
                anchors.bottomMargin: 34
                spacing: 14

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: Boolean(root.status.signedIn)
                              ? root.tr("commercial.subscription_required")
                              : root.tr("commercial.sign_in_title")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: 22
                        font.weight: Font.DemiBold
                    }
                    LanguageSelector { compact: true }
                }

                Text {
                    Layout.fillWidth: true
                    text: Boolean(root.status.signedIn)
                          ? root.tr("commercial.subscription_required_note")
                          : root.tr("commercial.sign_in_note")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    lineHeight: 1.4
                    wrapMode: Text.WordWrap
                }

                Rectangle {
                    visible: Boolean(root.status.signedIn)
                    Layout.fillWidth: true
                    Layout.preferredHeight: 94
                    radius: Theme.radius
                    color: Theme.attentionSoft
                    border.width: 1
                    border.color: "#F1D2A8"
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 14
                        spacing: 5
                        Text {
                            text: root.tr("commercial.signed_in_as")
                                  + " " + String(root.status.emailHint || "")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                        Text {
                            Layout.fillWidth: true
                            text: String(root.status.reason || "")
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                ModernTextField {
                    id: emailAddress
                    visible: !Boolean(root.status.signedIn)
                    Layout.fillWidth: true
                    placeholderText: root.tr("commercial.email_placeholder")
                    selectByMouse: true
                    inputMethodHints: Qt.ImhEmailCharactersOnly
                }

                RowLayout {
                    visible: !Boolean(root.status.signedIn) && root.codeRequested
                    Layout.fillWidth: true
                    spacing: 10
                    ModernTextField {
                        id: verificationCode
                        Layout.fillWidth: true
                        placeholderText: root.tr("commercial.code_placeholder")
                        selectByMouse: true
                        inputMethodHints: Qt.ImhDigitsOnly
                    }
                    ModernButton {
                        text: root.tr("commercial.resend")
                        compact: true
                        enabled: emailAddress.text.trim().length > 3
                        onClicked: root.controllerRef.requestCommercialLoginCode(
                            emailAddress.text
                        )
                    }
                }

                RowLayout {
                    visible: !Boolean(root.status.signedIn) && !root.codeRequested
                    Layout.fillWidth: true
                    spacing: 6
                    CheckBox {
                        id: acceptTerms
                        text: ""
                        Layout.alignment: Qt.AlignTop
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.tr("commercial.accept_terms")
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }
                }

                ModernButton {
                    visible: !Boolean(root.status.signedIn) && !root.codeRequested
                    Layout.fillWidth: true
                    text: root.tr("commercial.start_trial")
                    iconName: "forward"
                    tone: "primary"
                    enabled: acceptTerms.checked && emailAddress.text.trim().length > 3
                    onClicked: root.controllerRef.requestCommercialLoginCode(
                        emailAddress.text
                    )
                }

                ModernButton {
                    visible: !Boolean(root.status.signedIn) && root.codeRequested
                    Layout.fillWidth: true
                    text: root.tr("commercial.sign_in_activate")
                    iconName: "account"
                    tone: "primary"
                    enabled: verificationCode.text.trim().length >= 6
                    onClicked: root.controllerRef.completeCommercialLogin(
                        emailAddress.text,
                        verificationCode.text
                    )
                }

                RowLayout {
                    visible: Boolean(root.status.signedIn)
                    Layout.fillWidth: true
                    ModernButton {
                        Layout.fillWidth: true
                        text: root.tr("commercial.choose_plan")
                        iconName: "external"
                        tone: "primary"
                        onClicked: root.controllerRef.openCommercialUrl("pricing")
                    }
                    ModernButton {
                        text: root.tr("commercial.refresh_license")
                        iconName: "refresh"
                        onClicked: root.controllerRef.refreshCommercialLicense()
                    }
                }

                Text {
                    visible: root.feedback.length > 0
                    Layout.fillWidth: true
                    text: root.feedback
                    color: Theme.primary
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }

                Item { Layout.fillHeight: true }

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: root.tr("commercial.local_files_note")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        wrapMode: Text.WordWrap
                    }
                    ModernButton {
                        text: root.tr("commercial.privacy")
                        quiet: true
                        compact: true
                        onClicked: root.controllerRef.openCommercialUrl("privacy")
                    }
                    ModernButton {
                        text: root.tr("commercial.terms")
                        quiet: true
                        compact: true
                        onClicked: root.controllerRef.openCommercialUrl("terms")
                    }
                    ModernButton {
                        visible: Boolean(root.status.signedIn)
                        text: root.tr("commercial.sign_out")
                        quiet: true
                        compact: true
                        onClicked: root.controllerRef.signOutCommercial()
                    }
                }
            }
        }
    }
}
