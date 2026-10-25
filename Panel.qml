import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Strata in the bar: the icon shows whether a model is served, the popup lists
// the installed models and the server actions. strata-profiles.py does the
// work: it runs each model as a `strata runtime` profile on a config patched
// with your own settings (~/.config/strata/patches.json).
Panel {
  id: root
  moduleName: "io.github.schmittcn.strata-control"
  ipcTarget: "io.github.schmittcn.strata-control"
  // own IpcHandler below (one per target): the base's calls plus `monitor`
  manageIpc: false

  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
  readonly property string tool: pluginDir + "strata-profiles.py"
  readonly property string strataCommand: setting("strataCommand", "strata")
  readonly property string updateCommand: setting("updateCommand", "strata update")
  readonly property string updateCheckCommand: setting("updateCheckCommand", "")   // empty: `strata update --check`, summed up
  property bool updateAvailable: false   // the last check said so: the Update button installs instead of checking
  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string fontName: bar ? bar.fontFamily : Style.font.family

  property var models: []          // [{ id, name, context, status, state, url }]
  property string patchesPath: ""
  property string errorText: ""
  property string runningCmd: ""
  property string outputCmd: ""     // label of the command whose output is shown
  property var outputLines: []
  property var details: ({})       // strata-profiles details: version, per-model sizes and memory split
  property bool monitorOpen: false
  property var snapshot: ({})      // strata status, polled while the mini monitor is open
  property int cursor: 0
  property bool cursorActive: false

  // the model that is not stopped (starting, ready, stopping), if any
  readonly property var active: {
    for (var i = 0; i < models.length; i++) if (models[i].state !== "stopped") return models[i]
    return null
  }
  readonly property bool ready: active !== null && active.state === "ready"
  readonly property string statusText: runningCmd !== "" ? runningCmd
    : errorText !== "" ? errorText
    : active === null ? "Stopped"
    : active.state === "ready" ? "Ready · port " + active.url.split(":").pop()
    : active.state.charAt(0).toUpperCase() + active.state.slice(1)

  // Commands for the floating terminal carry the same STRATA_CLI as the inline ones.
  function inTerminal(cmd) {
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
      "export STRATA_CLI=" + Util.shellQuote(strataCommand) + "; " + cmd])
    close()
  }

  function contextLabel(tokens) {
    var n = Number(tokens || 0)
    return n >= 1024 ? Math.round(n / 1024) + "K" : ""
  }

  function refresh() { if (!info.running) info.running = true }

  function parseInfo(raw) {
    try {
      var data = JSON.parse(String(raw || ""))
      models = data.models || []
      patchesPath = data.patches || ""
      errorText = data.error ? String(data.error)
        : models.length === 0 ? "No models found: set one up with strata launch" : ""
    } catch (e) {
      // no JSON at all: the helper did not run (no python3) or crashed
      errorText = String(raw || "").trim() === "" ? "No answer from strata-profiles.py: is python3 installed?"
                                                 : "strata-profiles.py failed: run it in a terminal to see why"
    }
  }

  function rate(tps) { return tps ? Math.round(tps) + " tok/s" : "—" }

  // prompt and generation speed over the recent requests, weighted by time: freshly read prompt tokens (not the
  // reused cache) over prompt time, output tokens over decode time
  function averages(recent) {
    var list = recent || [], pt = 0, pms = 0, ot = 0, dms = 0, first = 0
    for (var i = 0; i < list.length; i++) {
      var r = list[i]
      if (r.prompt_ms > 0) { pt += Math.max(0, (r.prompt_tokens || 0) - (r.reused || 0)); pms += r.prompt_ms }
      if (r.decode_ms > 0) { ot += r.output_tokens || 0; dms += r.decode_ms }
      if (!first || r.time < first) first = r.time
    }
    return { n: list.length, prompt: pms > 0 ? pt / pms * 1000 : 0, gen: dms > 0 ? ot / dms * 1000 : 0,
             since: first ? Qt.formatTime(new Date(first * 1000), "HH:mm") : "" }
  }

  function gb(bytes) { return (Number(bytes || 0) / 1073741824).toFixed(1) }

  // "17.9 GB model · ~16 GB VRAM + 5.9 GB RAM · 18.8 GB disk"; ~ marks Strata's formula on a model its catalog lacks
  function sizeLine(id) {
    var d = (details.models || {})[id]
    if (!d) return ""
    var parts = [d.model_gb + " GB model"]
    if (d.vram_gb !== undefined) {
      var mem = (d.estimate ? "~" : "") + d.vram_gb + " GB VRAM + " + d.ram_gb + " GB RAM"
      parts.push(d.spill_gb > 0 ? mem + " + " + d.spill_gb + " GB from SSD" : mem)
    }
    parts.push(d.disk_gb + " GB disk")
    return parts.join(" · ")
  }

  // the running model's measured use: "in use: 13.2 GB VRAM · 50.2 GB RAM"
  function liveLine(m) {
    return m.vram_used_gb ? "in use: " + m.vram_used_gb + " GB VRAM · " + m.ram_used_gb + " GB RAM" : ""
  }

  function run(label, args) {
    if (proc.running) return
    // the same button again hides its output (Help, Apply, ...)
    if (outputCmd === label && outputLines.length > 0) { outputLines = []; outputCmd = ""; return }
    runningCmd = label
    outputCmd = label
    outputLines = []
    proc.command = ["python3", tool].concat(args)
    proc.running = true
  }

  function addOutput(line) {
    if (String(line).trim() === "") return
    // ponytail: keeps only the tail; full logs are in ~/.local/state/strata/runtime/
    outputLines = outputLines.concat([line]).slice(-12)
  }

  onOpenedChanged: if (opened) { refresh(); cursorActive = false; if (!detailsProc.running) detailsProc.running = true }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: info
    command: ["python3", root.tool, "info"]
    environment: ({ STRATA_CLI: root.strataCommand })
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.parseInfo(text) }
  }

  IpcHandler {
    target: "io.github.schmittcn.strata-control"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    // omarchy-shell io.github.schmittcn.strata-control monitor: the popup with the mini monitor open
    function monitor(): void { root.monitorOpen = true; root.open() }
  }

  Process {
    id: detailsProc
    command: ["python3", root.tool, "details"]
    environment: ({ STRATA_CLI: root.strataCommand })
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: { try { root.details = JSON.parse(text) } catch (e) {} } }
  }

  Process {
    id: statusProc
    command: ["python3", root.tool, "status"]
    environment: ({ STRATA_CLI: root.strataCommand })
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: { try { root.snapshot = JSON.parse(text) } catch (e) { root.snapshot = {} } } }
  }

  Timer {
    interval: 2000
    running: root.opened && root.monitorOpen
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!statusProc.running) statusProc.running = true
  }

  Process {
    id: proc
    environment: ({ STRATA_CLI: root.strataCommand, STRATA_UPDATE_CHECK: root.updateCheckCommand })
    stdout: SplitParser { onRead: function(line) { root.addOutput(line) } }
    stderr: SplitParser { onRead: function(line) { root.addOutput(line) } }
    onExited: {
      if (root.runningCmd === "Checking for updates")
        root.updateAvailable = /update available/i.test(root.outputLines.join("\n"))
      root.runningCmd = ""
      root.refresh()
    }
  }

  Timer {
    interval: (root.opened || root.runningCmd !== "" || (root.active && !root.ready) ? 3 : Math.max(2, Number(root.setting("refreshIntervalSec", 30)))) * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰧑"
    slotSize: Style.bar.statusSlot
    dimmed: !root.ready
    tooltipText: root.opened ? "" : "Strata: " + (root.active ? root.active.id + " (" + root.active.state + ")" : "stopped")
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.cursor = Math.max(0, Math.min(root.models.length - 1, root.cursor + dy))
      }
      onActivateRequested: if (root.cursorActive && root.models[root.cursor]) root.run("Starting " + root.models[root.cursor].id, ["start", root.models[root.cursor].id])
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero: icon · model / status ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            text: "󰧑"
            color: root.fg
            opacity: root.ready ? 1 : 0.5
            font.family: root.fontName
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter

            SequentialAnimation on opacity {
              running: root.runningCmd !== "" || (root.active !== null && !root.ready)
              loops: Animation.Infinite
              alwaysRunToEnd: true
              NumberAnimation { to: 0.25; duration: 700; easing.type: Easing.InOutSine }
              NumberAnimation { to: 1.0; duration: 700; easing.type: Easing.InOutSine }
            }
          }

          // the two things used most: the browser chat and the full terminal UI
          Row {
            id: heroButtons
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            ActionButton {
              text: "TUI Monitor"; iconText: "󰆍"
              tooltipText: "strata monitor: the full terminal UI"
              onClicked: root.inTerminal("$STRATA_CLI monitor")
            }
            ActionButton {
              text: "Web UI"; iconText: "󰖟"
              tooltipText: root.ready ? root.active.url : "No model is ready"
              enabled: root.ready
              onClicked: { Qt.openUrlExternally(root.active.url + "/"); root.close() }
            }
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroButtons.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: root.active ? root.active.id : "Strata"
              color: root.fg
              font.family: root.fontName
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideMiddle
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.statusText.toUpperCase()
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontName
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Models ----------
        Column {
          width: parent.width
          spacing: Style.space(4)

          PanelSectionHeader { text: "MODELS"; foreground: root.fg; fontFamily: root.fontName }

          // nothing to list: say why in full (the status line above only has room for the start of it)
          Text {
            visible: root.models.length === 0
            width: parent.width
            textFormat: Text.PlainText
            text: root.errorText !== "" ? root.errorText : "Asking strata…"
            color: root.fg
            opacity: 0.7
            font.family: root.fontName
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }

          Repeater {
            model: root.models
            ModelRow {
              required property var modelData
              required property int index
              title: modelData.id
              subtitle: [modelData.name, root.contextLabel(modelData.context), modelData.status !== "present" ? "files incomplete" : ""]
                .filter(function(s) { return s !== "" }).join(" · ")
              detail: root.sizeLine(modelData.id)
              live: root.liveLine(modelData)
              modelState: modelData.state
              hasCursor: root.cursorActive && root.cursor === index
              onClicked: root.run("Starting " + modelData.id, ["start", modelData.id])
              onHovered: function(h) { if (h) { root.cursorActive = true; root.cursor = index } }
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Server ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "SERVER"; foreground: root.fg; fontFamily: root.fontName }

          Flow {
            width: parent.width
            spacing: Style.space(6)

            ActionButton {
              text: "Stop"; iconText: "󰓛"
              tooltipText: "Stop the running model and free GPU and RAM"
              enabled: root.active !== null && root.runningCmd === ""
              onClicked: root.run("Stopping", ["stop"])
            }
            ActionButton {
              text: "Monitor"; iconText: "󰄪"
              tooltipText: "Mini monitor: GPU, memory, speed and requests"
              selected: root.monitorOpen
              onClicked: root.monitorOpen = !root.monitorOpen
            }
            ActionButton {
              text: "Log"; iconText: "󰦪"
              tooltipText: "Follow the server log"
              enabled: root.active !== null
              onClicked: root.inTerminal("tail -n 200 -F " + Util.shellQuote(root.active.log))
            }
            ActionButton {
              text: root.updateAvailable ? "Install update" : "Check for updates"
              iconText: root.updateAvailable ? "󰏗" : "󰚰"
              selected: root.updateAvailable
              tooltipText: root.updateAvailable ? "Runs in a terminal: " + root.updateCommand
                                                : "Only checks; nothing is installed"
              enabled: root.runningCmd === ""
              onClicked: {
                if (!root.updateAvailable) { root.run("Checking for updates", ["update-check"]); return }
                root.updateAvailable = false
                root.inTerminal(root.updateCommand)
              }
            }
          }
        }

        // ---------- Mini monitor: one `strata status` snapshot every 2 s while open ----------
        Column {
          id: monitor
          visible: root.monitorOpen
          width: parent.width
          spacing: Style.space(8)

          readonly property var s: root.snapshot || {}
          readonly property bool live: !!(s.instance && s.instance.state)
          readonly property var rates: s.rates || {}
          readonly property var work: s.work || {}

          PanelSeparator { foreground: root.fg }

          Item {
            width: parent.width
            implicitHeight: monHeader.implicitHeight
            PanelSectionHeader { id: monHeader; text: "MONITOR"; foreground: root.fg; fontFamily: root.fontName }
            Text {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: "󰅖"
              color: root.fg
              opacity: closeMouse.containsMouse ? 1 : 0.5
              font.family: root.fontName
              font.pixelSize: Style.font.body
              MouseArea { id: closeMouse; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true; onClicked: root.monitorOpen = false }
            }
          }

          Text {
            visible: !monitor.live
            text: root.active ? "Waiting for the server…" : "No server is running."
            color: root.fg
            opacity: 0.6
            font.family: root.fontName
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: monitor.live ? (monitor.s.devices || []) : []
            Meter {
              required property var modelData
              label: "VRAM · " + (modelData.name || "GPU")
              value: root.gb(modelData.mem_used) + " / " + root.gb(modelData.mem_total) + " GB"
              extra: [modelData.util !== null && modelData.util !== undefined ? modelData.util + "%" : "",
                      modelData.temp ? Math.round(modelData.temp) + "°C" : "",
                      modelData.power ? Math.round(modelData.power) + " W" : ""].filter(function(x) { return x !== "" }).join(" · ")
              fraction: Number(modelData.mem_used || 0) / Math.max(1, Number(modelData.mem_total || 1))
            }
          }

          Meter {
            visible: monitor.live && !!monitor.s.ram
            label: "RAM"
            value: monitor.s.ram ? root.gb(monitor.s.ram.used) + " / " + root.gb(monitor.s.ram.total) + " GB" : ""
            fraction: monitor.s.ram ? Number(monitor.s.ram.used) / Math.max(1, Number(monitor.s.ram.total)) : 0
          }

          // measured by strata-profiles itself: Strata's snapshot has no CPU; watts need a readable RAPL counter
          Meter {
            readonly property var cpu: monitor.s.cpu || null
            visible: cpu !== null
            label: "CPU · " + (cpu ? cpu.name.replace(/ \d+-Core Processor$/, "").replace(/^AMD |^Intel\(R\) Core\(TM\) /, "") : "")
            value: cpu ? cpu.util + "%" : ""
            extra: cpu ? [cpu.temp !== null ? Math.round(cpu.temp) + "°C" : "", cpu.power !== null ? Math.round(cpu.power) + " W" : "— W"]
              .filter(function(x) { return x !== "" }).join(" · ") : ""
            fraction: cpu ? cpu.util / 100 : 0
          }

          // the drive with the running model's weights: Strata reads from it when the weights do not fit in RAM
          Meter {
            readonly property var disk: monitor.s.cpu ? monitor.s.cpu.disk || null : null
            visible: disk !== null
            label: "SSD · " + (disk ? disk.model : "")
            value: disk ? (disk.read_mbs >= 1000 ? (disk.read_mbs / 1000).toFixed(1) + " GB/s" : Math.round(disk.read_mbs) + " MB/s") + " read" : ""
            extra: disk ? disk.util + "% busy" : ""
            fraction: disk ? disk.util / 100 : 0
          }

          Row {
            visible: monitor.live
            width: parent.width
            spacing: Style.space(16)
            Stat { label: "PROMPT NOW"; value: root.rate(monitor.rates.prefill_tok_s) }
            Stat { label: "GEN NOW"; value: root.rate(monitor.rates.answer_tok_s) }
            Stat { label: "ACTIVE"; value: String(monitor.work.active || 0) }
            Stat { label: "QUEUED"; value: String(monitor.work.queued || 0) }
          }

          // averages over the requests the server still lists: tokens over the time spent on them
          Row {
            readonly property var avg: root.averages(monitor.s.recent)
            visible: monitor.live && avg.n > 0
            width: parent.width
            spacing: Style.space(16)
            Stat { label: "PROMPT AVG"; value: root.rate(parent.avg.prompt) }
            Stat { label: "GEN AVG"; value: root.rate(parent.avg.gen) }
            Stat { label: "OVER"; value: parent.avg.n + (parent.avg.n === 1 ? " request" : " requests") + " since " + parent.avg.since }
          }

          Column {
            visible: monitor.live && (monitor.s.recent || []).length > 0
            width: parent.width
            spacing: Style.space(2)
            Text {
              text: "RECENT REQUESTS"
              color: root.fg
              opacity: 0.6
              font.family: root.fontName
              font.pixelSize: Style.font.caption
              font.letterSpacing: 1.2
            }
            Repeater {
              model: monitor.live ? (monitor.s.recent || []).slice(0, 4) : []
              Text {
                required property var modelData
                textFormat: Text.PlainText
                text: Qt.formatTime(new Date(modelData.time * 1000), "HH:mm:ss") + "   "
                  + (modelData.prompt_tokens || 0) + " in"
                  + (modelData.reused ? " (" + modelData.reused + " reused)" : "")
                  + " → " + (modelData.output_tokens || 0) + " out   "
                  + Number(modelData.duration_s || 0).toFixed(1) + " s"
                color: root.fg
                font.family: root.fontName
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Your settings: patches.json on top of setup's configs ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader { text: "YOUR SETTINGS"; foreground: root.fg; fontFamily: root.fontName }

          Flow {
            width: parent.width
            spacing: Style.space(6)

            ActionButton {
              text: "Edit"; iconText: "󰏫"
              tooltipText: root.patchesPath + "\nApplied when you close the editor"
              onClicked: root.inTerminal("f=$(python3 " + Util.shellQuote(root.tool) + " edit) && ${VISUAL:-${EDITOR:-nvim}} \"$f\" && python3 " + Util.shellQuote(root.tool) + " apply")
            }
            ActionButton {
              text: "Apply"; iconText: "󰒓"
              tooltipText: "Rebuild the patched configs and profiles; a running model picks them up on its next start"
              enabled: root.runningCmd === ""
              onClicked: root.run("Applying settings", ["apply"])
            }
            ActionButton {
              text: "Help"; iconText: "󰋖"
              tooltipText: "How patches.json works"
              enabled: root.runningCmd === ""
              onClicked: root.run("Help", ["help"])
            }
          }
        }

        // ---------- Output of the last inline command ----------
        Rectangle {
          visible: root.outputLines.length > 0
          width: parent.width
          implicitHeight: outputText.implicitHeight + Style.space(16)
          radius: Style.space(6)
          color: Style.normalFillFor(root.fg, Color.accent)

          Text {
            id: outputText
            anchors.fill: parent
            anchors.margins: Style.space(8)
            textFormat: Text.PlainText
            text: root.outputLines.join("\n")
            color: root.fg
            font.family: root.fontName
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          MouseArea {
            anchors.fill: parent
            enabled: root.runningCmd === ""
            cursorShape: Qt.PointingHandCursor
            onClicked: { root.outputLines = []; root.outputCmd = "" }
          }
        }

        // ---------- Footer: which Strata answers ----------
        Text {
          visible: (root.details.version || "") !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: root.details.version + " · " + String(root.details.install_dir || "").replace(Quickshell.env("HOME"), "~")
          color: root.fg
          opacity: 0.45
          font.family: root.fontName
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignRight
          elide: Text.ElideMiddle
        }
      }
    }
  }

  // A labelled bar: label and value above, a thin fill below.
  component Meter: Column {
    property string label: ""
    property string value: ""
    property string extra: ""
    property real fraction: 0
    width: parent.width
    spacing: Style.space(4)

    Item {
      width: parent.width
      implicitHeight: meterLabel.implicitHeight
      Text {
        id: meterLabel
        anchors.left: parent.left
        anchors.right: meterValue.left
        textFormat: Text.PlainText
        text: parent.parent.label + (parent.parent.extra !== "" ? "   " + parent.parent.extra : "")
        color: root.fg
        opacity: 0.7
        font.family: root.fontName
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
      Text {
        id: meterValue
        anchors.right: parent.right
        textFormat: Text.PlainText
        text: parent.parent.value
        color: root.fg
        font.family: root.fontName
        font.pixelSize: Style.font.caption
      }
    }
    Rectangle {
      width: parent.width
      height: Style.space(6)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
      Rectangle {
        height: parent.height
        radius: parent.radius
        width: Math.max(parent.height, parent.width * Math.min(1, parent.parent.fraction))
        color: root.fg
        Behavior on width { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
      }
    }
  }

  // A small figure with a caption above it.
  component Stat: Column {
    property string label: ""
    property string value: ""
    spacing: Style.space(1)
    Text { text: parent.label; color: root.fg; opacity: 0.6; font.family: root.fontName; font.pixelSize: Style.font.caption; font.letterSpacing: 1.2 }
    Text { text: parent.value; color: root.fg; font.family: root.fontName; font.pixelSize: Style.font.body; font.bold: true }
  }

  component ActionButton: Button {
    opacity: enabled ? 1 : 0.4   // Button itself does not show that it is disabled
    fontSize: Style.font.bodySmall
    foreground: root.fg
    fontFamily: root.fontName
    bordered: true
  }

  // One model: id on the left, name and context below; a mark shows its runtime state.
  component ModelRow: Rectangle {
    id: row
    property string title: ""
    property string subtitle: ""
    property string detail: ""
    property string live: ""
    property string modelState: "stopped"
    property bool hasCursor: false
    signal clicked()
    signal hovered(bool h)

    width: parent.width
    implicitHeight: rowText.implicitHeight + Style.space(12)
    radius: Style.space(6)
    color: mouse.pressed ? Style.pressedFillFor(root.fg, Color.accent)
      : (mouse.containsMouse || hasCursor) ? Style.hoverFillFor(root.fg, Color.accent)
      : modelState !== "stopped" ? Style.selectedFillFor(root.fg, Color.accent)
      : "transparent"

    Column {
      id: rowText
      anchors.left: parent.left
      anchors.right: mark.left
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(1)

      Text {
        textFormat: Text.PlainText
        text: row.title
        color: root.fg
        font.family: root.fontName
        font.pixelSize: Style.font.body
        font.bold: row.modelState !== "stopped"
      }
      Text {
        textFormat: Text.PlainText
        text: row.subtitle
        color: root.fg
        opacity: 0.6
        font.family: root.fontName
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: parent.width
      }
      Text {
        visible: row.live !== ""
        textFormat: Text.PlainText
        text: row.live
        color: root.fg
        opacity: 0.8
        font.family: root.fontName
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: parent.width
      }
      Text {
        visible: row.detail !== ""
        textFormat: Text.PlainText
        text: row.detail
        color: root.fg
        opacity: 0.45
        font.family: root.fontName
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: parent.width
      }
    }

    Text {
      id: mark
      text: row.modelState === "ready" ? "󰄬" : row.modelState === "stopped" ? "" : "󰔟"
      color: root.fg
      font.family: root.fontName
      font.pixelSize: Style.font.body
      anchors.right: parent.right
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
    }

    MouseArea {
      id: mouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: row.clicked()
      onContainsMouseChanged: row.hovered(containsMouse)
    }
  }
}
