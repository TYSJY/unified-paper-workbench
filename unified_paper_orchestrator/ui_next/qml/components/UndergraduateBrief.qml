import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Popup {
    id: root
    property var controllerRef
    property var materials: controllerRef ? controllerRef.undergraduateMaterials : null
    property string workflowVersion: ""
    property string schemeError: ""
    property string previewHeading: "资料读取预览（仅文字，不代表格式验收）"
    property bool locked: Boolean(materials && materials.busy) || Boolean(controllerRef && controllerRef.loading)
    function tr(key) { return i18n.text(key, i18n.revision) }
    function syncScheme() {
        var response = controllerRef.undergraduateScheme(direction.currentValue)
        workflowVersion = response.version
        schemeError = response.error
    }
    function prepare(name, version) {
        if (visible || locked) return
        direction.currentIndex = name.indexOf("计算机") >= 0 ? 1 : 0
        syncScheme()
        if (materials) materials.reset()
        topic.text = ""; requirements.text = ""; minimumReferences.text = ""; reviewed.checked = false
        open(); topic.forceActiveFocus()
    }
    anchors.centerIn: parent
    width: Math.min(980, parent.width - 32)
    height: Math.min(820, parent.height - 32)
    modal: true; focus: true; closePolicy: Popup.NoAutoClose; padding: 22
    background: Rectangle { color: Theme.surface; radius: Theme.radius; border.color: Theme.border }
    Connections { target: root.controllerRef; function onUndergraduateCreated() { root.close() } }
    Connections { target: root.materials; function onChanged() { reviewed.checked = false } }
    FileDialog {
        id: files
        title: "添加学校要求、模板或论文资料（可多选）"; fileMode: FileDialog.OpenFiles
        nameFilters: ["论文资料 (*.zip *.pdf *.docx *.doc *.txt *.md *.png *.jpg *.jpeg *.webp *.tex *.bib *.csv *.json *.py *.java *.js *.ts *.html *.css *.sql)", "所有文件 (*)"]
        onAccepted: root.materials.add(selectedFiles.map(function(url) { return String(url) }), role.currentValue)
    }
    FolderDialog {
        id: folder
        title: "选择学校资料文件夹（不要选择整个磁盘或个人目录）"
        onAccepted: root.materials.add([String(selectedFolder)], role.currentValue)
    }
    Popup {
        id: preview; anchors.centerIn: parent
        width: parent.width - 40; height: parent.height - 40; modal: true; padding: 18
        background: Rectangle { color: Theme.surface; radius: Theme.radius; border.color: Theme.border }
        contentItem: ColumnLayout {
            Text { text: root.previewHeading; color: Theme.text; font.family: Theme.fontFamily }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea { id: previewText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: Theme.text; font.family: Theme.fontFamily }
            }
            ModernButton { Layout.alignment: Qt.AlignRight; text: "关闭预览"; onClicked: preview.close() }
        }
    }
    contentItem: ColumnLayout {
        spacing: 12
        RowLayout {
            Text { text: "新建本科毕业论文"; color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 23; font.weight: Font.DemiBold }
            Item { Layout.fillWidth: true }
            Text { text: "题目 · 学校要求 · 资料包"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 12 }
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.WordWrap; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12
            text: "一篇论文绑定一套题目和材料。类型仅影响这篇新论文，不改变期刊设置或已有论文。导入在本机完成，不调用 AI；开始写作后才使用模型额度。"
        }
        ScrollView {
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: parent.width; spacing: 10; enabled: !root.locked
                RowLayout {
                    Text { text: "论文类型"; color: Theme.text; font.family: Theme.fontFamily }
                    ModernComboBox {
                        id: direction; objectName: "undergraduateDirection"; Layout.fillWidth: true
                        textRole: "label"; valueRole: "value"
                        model: [{value: "undergraduate_humanities", label: "本科 · 人文社科"}, {value: "undergraduate_software", label: "本科 · 计算机软件"}]
                        onActivated: root.syncScheme()
                    }
                }
                RowLayout {
                    Text {
                        Layout.fillWidth: true; wrapMode: Text.WordWrap; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12
                        text: direction.currentIndex === 0 ? "已联动人文社科配图：研究框架 / 分析流程 / 真实数据图；数量按论文需要。"
                            : "已联动软件工程配图：系统架构 / 流程 / E-R / 真实测试图；不虚构实现。"
                    }
                    ModernButton {
                        text: "查看配图方案"; compact: true
                        onClicked: { root.previewHeading = "配套本科画图方案（三步）"; previewText.text = root.controllerRef.previewUndergraduateFigures(direction.currentValue); preview.open() }
                    }
                }
                Text { text: "论文题目 *"; color: Theme.text; font.family: Theme.fontFamily }
                ModernTextField { id: topic; objectName: "undergraduateTitle"; Layout.fillWidth: true; maximumLength: 500; placeholderText: "填写已确定的题目，第一步不会自动换题" }
                Text { text: "学校要求与补充说明（文字或附件至少提供一种）"; color: Theme.text; font.family: Theme.fontFamily }
                ScrollView {
                    Layout.fillWidth: true; Layout.preferredHeight: 100; clip: true
                    TextArea {
                        id: requirements; objectName: "undergraduateRequirements"; wrapMode: TextEdit.Wrap; selectByMouse: true
                        placeholderText: "例如：学校、专业、字数、导师意见、AI 使用要求。要求在附件里时可以不重复抄写。"
                        color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13; padding: 10
                        background: Rectangle { color: Theme.surfaceSoft; radius: Theme.radiusSmall; border.color: Theme.border }
                    }
                }
                RowLayout {
                    Text { text: "添加资料用途"; color: Theme.text; font.family: Theme.fontFamily }
                    ModernComboBox {
                        id: role; Layout.preferredWidth: 175; textRole: "label"; valueRole: "value"
                        model: [{value: "requirements", label: "学校要求"}, {value: "template", label: "学校模板"}, {value: "reference", label: "参考资料 / 范例"}, {value: "project", label: "已有研究 / 代码"}]
                    }
                    ModernButton { objectName: "addUndergraduateFiles"; text: "添加文件 / ZIP"; iconName: "plus"; compact: true; onClicked: files.open() }
                    ModernButton { objectName: "addUndergraduateFolder"; text: "添加文件夹"; compact: true; onClicked: folder.open() }
                    Item { Layout.fillWidth: true }
                }
                Text {
                    Layout.fillWidth: true; wrapMode: Text.WordWrap; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11
                    text: "最多 100 个文件，单文件 30MB，总计 150MB。ZIP 自动展开安全文件；支持 DOCX、文字 PDF、文本。图片/扫描件不做 OCR，旧 DOC 需转换。不执行代码或宏，请勿导入密码和个人敏感资料。"
                }
                Repeater {
                    model: root.materials ? root.materials.items : []
                    delegate: Rectangle {
                        required property var modelData
                        Layout.fillWidth: true; implicitHeight: 72; color: Theme.surfaceSoft; radius: Theme.radiusSmall
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 10; spacing: 8
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 3
                                Text { Layout.fillWidth: true; text: modelData.name; elide: Text.ElideMiddle; color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 12 }
                                Text {
                                    Layout.fillWidth: true; elide: Text.ElideRight; font.family: Theme.fontFamily; font.pixelSize: 11
                                    color: modelData.status === "text" ? Theme.success : Theme.attention
                                    text: ({requirements: "学校要求", template: "学校模板", reference: "参考资料", project: "研究/代码"})[modelData.role]
                                        + " · " + (modelData.status === "text" ? "已提取文字" : modelData.status === "partial" ? "部分提取" : "需人工查看") + " · " + modelData.detail
                                }
                            }
                            ModernButton { text: "预览"; compact: true; onClicked: { root.previewHeading = "资料读取预览（仅文字，不代表格式验收）"; previewText.text = root.materials.preview(modelData.id); preview.open() } }
                            ModernButton { text: "移除"; compact: true; onClicked: root.materials.remove(modelData.id) }
                        }
                    }
                }
                Text { visible: root.materials && root.materials.items.length === 0; text: "尚未添加资料。可直接选择学校给的 ZIP 文件包。"; color: Theme.textMuted; font.family: Theme.fontFamily }
                Text {
                    Layout.fillWidth: true; visible: Boolean(root.materials && root.materials.warnings)
                    text: root.materials ? root.materials.warnings : ""; wrapMode: Text.WrapAnywhere; color: Theme.attention; font.family: Theme.fontFamily; font.pixelSize: 11
                }
                RowLayout {
                    Text { text: root.tr("undergraduate.min_references"); color: Theme.textSecondary; font.family: Theme.fontFamily }
                    ModernTextField { id: minimumReferences; objectName: "undergraduateMinimumReferences"; Layout.preferredWidth: 100; placeholderText: "可不填"; validator: IntValidator { bottom: 1; top: 1000 } }
                    Item { Layout.fillWidth: true }
                }
                CheckBox {
                    id: reviewed; objectName: "undergraduateMaterialsReviewed"; visible: Boolean(root.materials && root.materials.items.length)
                    Layout.fillWidth: true; text: "已核对附件清单和读取状态；未提取的内容会保留为待确认项。"
                }
            }
        }
        Text { Layout.fillWidth: true; visible: root.schemeError.length > 0; text: root.schemeError; wrapMode: Text.WordWrap; color: Theme.danger }
        Text {
            Layout.fillWidth: true; wrapMode: Text.WordWrap; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11
            text: root.materials && root.materials.busy ? "正在本地复制资料、展开 ZIP 并提取文字，请稍候…"
                : "资料副本保存在当前工作区；移除仅取消本次关联，原文件不变。最终交付可编辑 Word 和排版预览；图表整合后按学校要求排版、逐页检查。"
        }
        RowLayout {
            Layout.fillWidth: true
            Text { text: root.materials ? "已导入 " + root.materials.items.length + " 个文件" : ""; color: Theme.textSecondary; font.family: Theme.fontFamily }
            Item { Layout.fillWidth: true }
            ModernButton { text: root.tr("action.cancel"); enabled: !root.locked; onClicked: root.close() }
            ModernButton {
                objectName: "startUndergraduateWriting"; text: "创建并开始第一步"; tone: "primary"
                enabled: !root.locked && root.workflowVersion.length > 0 && topic.text.trim().length > 0
                    && (requirements.text.trim().length > 0 || Boolean(root.materials && root.materials.items.length))
                    && requirements.text.length <= 40000 && (minimumReferences.text.length === 0 || minimumReferences.acceptableInput)
                    && (!(root.materials && root.materials.items.length) || reviewed.checked)
                onClicked: root.controllerRef.createUndergraduateProject(topic.text, requirements.text, minimumReferences.text, direction.currentValue, root.workflowVersion)
            }
        }
    }
}
