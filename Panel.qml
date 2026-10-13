import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Pixel Buds Pro in the bar. Device data and control live in Service.qml
// (one per shell), which runs the bundled Python Maestro bridge; every bar
// widget instance, one per monitor, reads and drives that single service.
// Hidden entirely while no Pixel Buds are connected.
Panel {
  id: root
  moduleName: "io.github.rdoupe.pixelbuds"
  ipcTarget: "io.github.rdoupe.pixelbuds"
  manageIpc: false

  property bool advancedOpen: false
  property bool eqOpen: false
  property int ancIndex: 1
  property bool cursorActive: false

  // The service registers asynchronously; re-ask for it for a short while.
  property int serviceTick: 0
  readonly property var svc: {
    var tick = serviceTick
    var host = root.bar ? root.bar.shell : null
    return host && typeof host.serviceFor === "function" ? host.serviceFor(root.moduleName) : null
  }
  Timer {
    interval: 1000
    repeat: true
    running: root.svc === null && root.serviceTick < 60
    onTriggered: root.serviceTick++
  }

  readonly property var status: svc ? svc.status : ({})
  readonly property var controls: svc ? svc.controls : ({})
  readonly property string pendingAnc: svc ? svc.pendingAnc : ""

  // Distro identity for the OSD helper. Never ambient PATH.
  readonly property int pollInterval: Math.max(5, parseInt(setting("pollIntervalSec", 30)) || 30) * 1000
  readonly property bool hideWhenDisconnected: String(setting("hideWhenDisconnected", true)) === "true"
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  // Device-supplied; only trusted as five finite numbers, else no EQ rows.
  readonly property var eqBands: {
    if (controls.ctl_eq === undefined) return []
    var b = String(controls.ctl_eq).split(",").map(Number)
    return b.length === 5 && b.every(function(x) { return isFinite(x) }) ? b : []
  }

  readonly property bool connected: String(status.connected || "0") === "1"
  readonly property bool adaptiveSupported: String(status.adaptive_supported || "0") === "1"
  readonly property var ancModes: adaptiveSupported ? Model.ANC_MODES : Model.LEGACY_ANC_MODES
  readonly property string budsName: String(status.name || "Pixel Buds")
  readonly property string anc: pendingAnc !== "" ? pendingAnc : String(status.anc || "unknown")
  readonly property int leftPct: Model.pct(status, "left")
  readonly property int rightPct: Model.pct(status, "right")
  readonly property int casePct: Model.pct(status, "case")
  readonly property int caseLastPct: Model.pct(status, "case_last")
  readonly property int caseLastAge: Model.pct(status, "case_last_age")
  readonly property bool caseStale: casePct < 0 && caseLastPct >= 0
  readonly property bool leftCharging: Model.charging(status, "left")
  readonly property bool rightCharging: Model.charging(status, "right")
  readonly property bool caseCharging: Model.charging(status, "case")
  readonly property bool leftInCase: String(status.left_in_case || "0") === "1"
  readonly property bool rightInCase: String(status.right_in_case || "0") === "1"
  readonly property int minPct: Model.budsMin(status)
  readonly property bool anyCharging: leftCharging || rightCharging

  // Bluetooth absolute volume is already the laptop's PipeWire volume. Read
  // that shared value directly so a swipe on either bud updates this panel
  // without another RFCOMM query.
  readonly property var audioSink: Pipewire.defaultAudioSink
  readonly property string bluezSinkNeedle: String(status.addr || "").replace(/:/g, "_")
  readonly property bool budsAudioActive: connected && audioSink && audioSink.audio
      && bluezSinkNeedle !== "" && String(audioSink.name || "").indexOf(bluezSinkNeedle) >= 0
  readonly property int budsVolumePct: budsAudioActive
      ? Math.max(0, Math.min(100, Math.round(audioSink.audio.volume * 100))) : -1
  property bool volumeWatchReady: false
  property int lastBudsVolumePct: -1
  readonly property var ancGestureModes: String(controls.ctl_anc_gesture_loop || "")
      .split(",").filter(function(x) { return x !== "" })

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function refresh() { if (svc) svc.refresh() }
  function refreshControls() { if (svc && connected) svc.refreshControls() }

  // Every set is followed by a re-read in the bridge, so the UI shows the
  // device's truth.
  function setControl(key, fields) {
    if (!svc || !connected) return
    svc.setControl(key, fields)
  }

  function setEqBand(i, v) {
    var b = eqBands.slice()
    if (b.length !== 5) return
    b[i] = Math.round(Number(v) * 10) / 10
    setControl("eq", { bands: b })
  }

  function setAnc(mode) {
    if (!svc || !connected) return
    svc.setAnc(mode)
  }

  function cycleAnc(delta) {
    if (!svc || !connected) return
    svc.cycleAnc(delta)
  }

  function ancModeIndex(mode) {
    var i = ancModes.indexOf(String(mode || ""))
    return i < 0 ? 0 : i
  }

  function moveAncCursor(delta) {
    var n = ancModes.length
    ancIndex = ((ancIndex + delta) % n + n) % n
  }

  function setHoldAction(side, action) {
    if (action !== "anc" && action !== "assistant") return
    var left = String(controls.ctl_gesture_left || "")
    var right = String(controls.ctl_gesture_right || "")
    if (left === "" || right === "") return
    if (side === "left") left = action
    else if (side === "right") right = action
    else return
    setControl("gesture-control", { left: left, right: right })
  }

  function setAncGestureMode(mode, enabled) {
    if (ancModes.indexOf(mode) < 0) return
    var selected = ancGestureModes.slice()
    var at = selected.indexOf(mode)
    if (enabled && at < 0) selected.push(mode)
    else if (!enabled && at >= 0) selected.splice(at, 1)
    // The buds require at least two modes in their physical cycle.
    if (selected.length < 2) return
    setControl("anc-gesture-loop", { modes: selected })
  }


  function observeBudsVolume() {
    if (!budsAudioActive || budsVolumePct < 0) {
      volumeWatchReady = false
      lastBudsVolumePct = -1
      return
    }
    // Binding the sink or reconnecting establishes a baseline; only a later
    // real change deserves an OSD.
    if (!volumeWatchReady) {
      lastBudsVolumePct = budsVolumePct
      volumeWatchReady = true
      return
    }
    if (budsVolumePct === lastBudsVolumePct) return
    lastBudsVolumePct = budsVolumePct
    if (svc) svc.showVolumeOsd(budsVolumePct)   // one coalesced OSD for all bars
  }

  onBudsAudioActiveChanged: observeBudsVolume()
  onBudsVolumePctChanged: observeBudsVolume()

  function tooltip() {
    if (!connected) return "Pixel Buds — not connected"
    var parts = []
    if (leftPct >= 0) parts.push("L " + leftPct + "%")
    if (rightPct >= 0) parts.push("R " + rightPct + "%")
    if (casePct >= 0) parts.push("Case " + casePct + "%")
    else if (caseStale) parts.push("Case " + caseLastPct + "% (" + Model.ageText(caseLastAge) + ")")
    return budsName + " — " + parts.join(" · ") + " — " + Model.ancLabel(anc)
  }

  IpcHandler {
    target: "io.github.rdoupe.pixelbuds"

    function open() { root.open() }
    function close() { root.close() }
    function show() { root.open() }
    function hide() { root.close() }
    function toggle() { root.toggle() }
    function showAdvanced() { root.open(); root.advancedOpen = true }
    function refresh() { root.refresh() }
    function cycleAnc() { root.cycleAnc(1) }
    function setAnc(mode: string) { root.setAnc(mode) }
  }

  // Battery/ANC changes arrive as pushes from the buds; this slow poll is
  // only a fallback (faster while the panel is open). Disconnected idles at
  // zero cost — reconnects arrive via the service's BlueZ monitor.
  Timer {
    interval: root.opened ? 5000 : root.pollInterval
    running: root.connected
    repeat: true
    onTriggered: root.refresh()
  }


  PwObjectTracker { objects: root.audioSink ? [root.audioSink] : [] }

  onOpenedChanged: {
    if (opened) {
      if (!connected) { close(); return }
      refresh()
      refreshControls()
      ancIndex = root.ancModeIndex(root.anc)
      cursorActive = false
      advancedOpen = false
      eqOpen = false
    }
  }
  onConnectedChanged: {
    if (!connected) close()
  }
  onStatusChanged: if (opened && !cursorActive) ancIndex = root.ancModeIndex(root.anc)

  readonly property bool shown: connected || !hideWhenDisconnected
  visible: shown
  implicitWidth: shown ? button.implicitWidth : 0
  implicitHeight: shown ? button.implicitHeight : 0

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: caseIcon
    active: false
    opacity: root.connected ? 1 : 0.45
    tooltipText: root.tooltip()
    onPressed: function(b) {
      if (!root.connected) return
      if (b === Qt.RightButton || b === Qt.MiddleButton) root.cycleAnc(1)
      else root.toggle()
    }
  }

  // No glyph exists for the closed Pixel Buds case, so draw it: rounded lid
  // and body separated by a thin gap for the hinge seam.
  Component {
    id: caseIcon
    Item {
      id: icon
      readonly property color tint: button.active && button.useActiveColor ? button.activeColor : button.foreground
      onTintChanged: caseCanvas.requestPaint()

      Canvas {
        id: caseCanvas
        anchors.fill: parent
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
          var ctx = getContext("2d")
          ctx.reset()
          // Real case: 2 1/2" tall by 1 7/8" wide, hinge seam 3/4" from the
          // top, flaring out for about another 1/4" below it before the sides
          // go flat. The same curve caps the top and bottom (superellipse
          // quadrants, n = 2.5, each reaching 1" in from its end); only the
          // middle 1/2" is straight side.
          var ch = height * 0.82
          var cw = ch * (1.875 / 2.5)
          var capH = ch * (1.0 / 2.5)
          var cx = width / 2
          // The measured shape still reads a touch tall at bar size: take one
          // pixel row out of the flat section below the hinge.
          var trim = 1
          var top = (height - ch + trim) / 2
          var a = cw / 2
          var n = 2.5
          var yTop = top + capH
          var yBot = top + ch - capH - trim
          ctx.fillStyle = icon.tint
          ctx.beginPath()
          var STEPS = 24
          for (var i = 0; i <= STEPS; i++) {  // top cap, right edge to left
            var t = Math.PI * i / STEPS
            var c = Math.cos(t), s = Math.sin(t)
            var px = cx + a * Math.sign(c) * Math.pow(Math.abs(c), 2 / n)
            var py = yTop - capH * Math.pow(Math.abs(s), 2 / n)
            if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py)
          }
          ctx.lineTo(cx - a, yBot)            // left flat side
          for (var j = 0; j <= STEPS; j++) {  // bottom cap, left edge to right
            var u = Math.PI * j / STEPS
            var cu = Math.cos(u), su = Math.sin(u)
            ctx.lineTo(cx - a * Math.sign(cu) * Math.pow(Math.abs(cu), 2 / n),
                       yBot + capH * Math.pow(Math.abs(su), 2 / n))
          }
          ctx.closePath()                     // right flat side
          ctx.fill()
          var seam = Math.max(1.1, height * 0.06)
          ctx.clearRect(0, top + ch * (0.75 / 2.5) - seam / 2, width, seam)
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened && root.connected
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveAncCursor(dx !== 0 ? dx : dy)
      }
      onActivateRequested: if (root.cursorActive) root.setAnc(root.ancModes[root.ancIndex])
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroPercent.implicitHeight)

          Text {

            textFormat: Text.PlainText
            id: heroIcon
            text: Model.ancIcon(root.anc)
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroPercent.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {

              textFormat: Text.PlainText
              text: root.budsName
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {

              textFormat: Text.PlainText
              text: (root.pendingAnc !== "" ? "Switching to " + Model.ancLabel(root.anc)
                  : Model.ancLabel(root.anc)).toUpperCase()
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Text {

            textFormat: Text.PlainText
            id: heroPercent
            text: root.minPct >= 0 ? root.minPct + "%" : "—"
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        // The buds' swipe gesture and the laptop share Bluetooth absolute
        // volume. This row follows PipeWire live; BlueZ changes also summon
        // Omarchy's normal volume OSD even while this panel is closed.
        Item {
          visible: root.budsAudioActive
          width: parent.width
          implicitHeight: volumeLabel.implicitHeight

          Text {
            id: volumeLabel
            textFormat: Text.PlainText
            anchors.left: parent.left
            text: "Volume · swipe either bud"
            color: root.fg
            opacity: 0.8
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            textFormat: Text.PlainText
            anchors.right: parent.right
            text: root.budsVolumePct + "%  " + Model.volumeIcon(root.budsVolumePct)
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }

        // ---------- Batteries ----------
        Column {
          width: parent.width
          spacing: Style.space(8)

          BatteryRow {
            label: "Left"
            level: root.leftPct
            charging: root.leftCharging
            hint: root.leftInCase ? "in case" : ""
          }
          BatteryRow {
            label: "Right"
            level: root.rightPct
            charging: root.rightCharging
            hint: root.rightInCase ? "in case" : ""
          }
          BatteryRow {
            label: "Case"
            level: root.casePct >= 0 ? root.casePct : root.caseLastPct
            charging: root.caseCharging
            stale: root.caseStale
            hint: root.caseStale ? "last seen " + Model.ageText(root.caseLastAge)
                : root.casePct < 0 ? "reports when a bud is docked" : ""
          }
        }

        // ---------- Listening mode ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "LISTENING MODE"
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          Row {
            id: modeRow
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * (root.ancModes.length - 1)) / root.ancModes.length

            Repeater {
              model: root.ancModes
              Button {
                required property var modelData
                required property int index
                width: modeRow.cellWidth
                iconText: Model.ancIcon(String(modelData))
                iconSize: Style.font.title
                text: Model.ancShort(String(modelData))
                fontSize: Style.font.bodySmall
                foreground: root.fg
                fontFamily: root.fontFamily
                horizontalPadding: Style.spacing.controlPaddingX
                verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
                bordered: true
                active: root.anc === String(modelData)
                hasCursor: root.cursorActive && root.ancIndex === index
                tooltipText: Model.ancLabel(String(modelData))
                onClicked: root.setAnc(String(modelData))
                onHovered: function(h) {
                  if (h) { root.cursorActive = true; root.ancIndex = index }
                }
              }
            }
          }
        }

        // ---------- EQ (collapsed): sound tuning ----------
        PanelSeparator { foreground: root.fg }

        Item {
          id: eqHeader
          width: parent.width
          implicitHeight: eqLabel.implicitHeight + Style.space(4)

          Text {
            id: eqLabel
            textFormat: Text.PlainText
            text: (root.eqOpen ? "▾" : "▸") + "  EQ"
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            anchors.verticalCenter: parent.verticalCenter
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              root.eqOpen = !root.eqOpen
              if (root.eqOpen && Object.keys(root.controls).length === 0) root.refreshControls()
            }
          }
        }

        Column {
          visible: root.eqOpen
          width: parent.width
          spacing: Style.space(6)

          Text {
            textFormat: Text.PlainText
            visible: root.controls.ctl_volume_eq === undefined && root.controls.ctl_mono === undefined
                && root.controls.ctl_balance === undefined && root.controls.ctl_eq === undefined
            width: parent.width
            text: root.svc && root.svc.readingControls ? "Reading device settings…" : "The buds reported no sound settings."
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          ToggleRow { label: "Volume EQ"; ctlKey: "volume-eq"; statusKey: "ctl_volume_eq"; hint: "Boosts bass and treble at low volume so quiet listening sounds fuller" }
          ToggleRow { label: "Mono audio"; ctlKey: "mono"; statusKey: "ctl_mono"; hint: "Plays the same mixed sound in both buds, handy with one bud in" }

          SliderRow {
            label: "Balance"
            hint: "Shift volume toward the left or right bud"
            visible: root.controls.ctl_balance !== undefined
            from: -100; to: 100; step: 5
            value: Math.max(-100, Math.min(100, parseInt(root.controls.ctl_balance) || 0))
            format: function(v) { return v === 0 ? "center" : (v < 0 ? "L " + (-v) : "R " + v) }
            onCommitted: function(v) { root.setControl("balance", { value: Math.round(v) }) }
          }

          Repeater {
            model: ["Low bass", "Bass", "Mid", "Treble", "Upper treble"]
            SliderRow {
              required property var modelData
              required property int index
              label: modelData
              hint: ["Boost or cut the deepest bass: rumble and kick drums",
                     "Boost or cut bass: body and punch",
                     "Boost or cut the midrange: voices and guitars",
                     "Boost or cut treble: clarity and detail",
                     "Boost or cut the highest treble: air and sparkle"][index]
              visible: root.eqBands.length === 5
              from: -6; to: 6; step: 0.5
              value: root.eqBands.length === 5 ? root.eqBands[index] : 0
              format: function(v) { return v.toFixed(1) }
              onCommitted: function(v) { root.setEqBand(index, v) }
            }
          }
        }

        // ---------- Advanced (collapsed): device toggles + touch controls ----------
        PanelSeparator { visible: advancedHeader.visible; foreground: root.fg }

        Item {
          id: advancedHeader
          width: parent.width
          implicitHeight: advLabel.implicitHeight + Style.space(4)

          Text {

            textFormat: Text.PlainText
            id: advLabel
            text: (root.advancedOpen ? "▾" : "▸") + "  ADVANCED"
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            anchors.verticalCenter: parent.verticalCenter
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              root.advancedOpen = !root.advancedOpen
              if (root.advancedOpen && Object.keys(root.controls).length === 0) root.refreshControls()
            }
          }
        }

        Column {
          visible: root.advancedOpen && advancedHeader.visible
          width: parent.width
          spacing: Style.space(6)

          Text {

            textFormat: Text.PlainText
            visible: Object.keys(root.controls).length === 0
            width: parent.width
            text: root.svc && root.svc.readingControls ? "Reading device settings…" : "The buds reported no adjustable settings."
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          ToggleRow { label: "Multipoint audio"; ctlKey: "multipoint"; statusKey: "ctl_multipoint"; hint: "Stay connected to two devices at once, like your phone and this computer" }
          ToggleRow { label: "Speech detection"; ctlKey: "speech-detection"; statusKey: "ctl_speech_detection"; hint: "When you start talking, the buds lower your audio and switch to transparency" }

          PanelSectionHeader {
            visible: root.controls.ctl_gestures !== undefined
                || root.controls.ctl_gesture_left !== undefined
                || root.controls.ctl_anc_gesture_loop !== undefined
            text: "TOUCH CONTROLS"
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          ToggleRow { label: "Touch controls"; ctlKey: "gestures"; statusKey: "ctl_gestures"; hint: "Turn the buds' tap, swipe and hold gestures on or off" }
          HoldActionRow { label: "Left hold"; side: "left"; statusKey: "ctl_gesture_left"; hint: "Press and hold the left bud to cycle listening modes" }
          HoldActionRow { label: "Right hold"; side: "right"; statusKey: "ctl_gesture_right"; hint: "Press and hold the right bud to cycle listening modes" }

          Column {
            visible: root.controls.ctl_anc_gesture_loop !== undefined
            width: parent.width
            spacing: Style.space(6)

            HoverHandler { id: cyclesHover }
            PanelToolTip {
              visible: cyclesHover.hovered
              text: "Listening modes a press and hold cycles through (pick at least two)"
            }

            Text {
              textFormat: Text.PlainText
              text: "Hold gesture cycles"
              color: root.fg
              opacity: 0.8
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Row {
              width: parent.width
              spacing: Style.space(6)
              Repeater {
                model: root.ancModes
                Button {
                  required property var modelData
                  readonly property bool selected: root.ancGestureModes.indexOf(String(modelData)) >= 0
                  width: (parent.width - parent.spacing * (root.ancModes.length - 1)) / root.ancModes.length
                  text: Model.ancShort(String(modelData))
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  fontFamily: root.fontFamily
                  horizontalPadding: Style.spacing.controlPaddingX
                  verticalPadding: Style.spacing.controlPaddingY
                  bordered: true
                  active: selected
                  onClicked: root.setAncGestureMode(String(modelData), !selected)
                }
              }
            }
          }

        }

        Text {

          textFormat: Text.PlainText
          visible: !!root.status.error
          width: parent.width
          wrapMode: Text.WordWrap
          text: String(root.status.error || "")
          color: root.bar ? root.bar.urgent : Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }

  component BatteryRow: Item {
    id: row
    property string label: ""
    property int level: -1
    property bool charging: false
    property bool stale: false
    property string hint: ""

    readonly property bool low: level >= 0 && level <= 20 && !charging

    width: parent.width
    implicitHeight: labelText.implicitHeight + track.height + Style.space(6)

    Text {

      textFormat: Text.PlainText
      id: labelText
      anchors.left: parent.left
      anchors.top: parent.top
      text: row.label + (row.hint !== "" ? "  ·  " + row.hint : "")
      color: root.fg
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {

      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.top: parent.top
      text: (row.level >= 0 ? row.level + "%" : "—") + "  " + Model.batteryIcon(row.level, row.charging)
      color: row.low ? root.urgentColor : root.fg
      opacity: row.stale ? 0.5 : 1.0
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Rectangle {
      id: track
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      height: Style.space(6)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        radius: parent.radius
        color: row.low ? root.urgentColor : root.fg
        opacity: row.stale ? 0.4 : 1.0
        width: row.level >= 0 ? Math.max(parent.height, parent.width * row.level / 100) : 0
        Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }

        SequentialAnimation on opacity {
          running: row.charging && root.opened
          loops: Animation.Infinite
          alwaysRunToEnd: true
          NumberAnimation { from: 1.0; to: 0.55; duration: 950; easing.type: Easing.InOutSine }
          NumberAnimation { from: 0.55; to: 1.0; duration: 950; easing.type: Easing.InOutSine }
        }
      }
    }
  }

  // A label with an On/Off button, shown only when the buds reported a state
  // for it — an unanswered control renders nothing at all.
  component ToggleRow: Item {
    id: trow
    // Mouse-over explanation; HoverHandler is passive, so clicks and drags
    // still reach the controls.
    property string hint: ""
    HoverHandler { id: trowHover }
    PanelToolTip { visible: trow.hint !== "" && trowHover.hovered; text: trow.hint }
    property string label: ""
    property string ctlKey: ""
    property string statusKey: ""
    readonly property bool known: root.controls[statusKey] !== undefined
    readonly property bool on: String(root.controls[statusKey] || "false") === "true"

    visible: known
    width: parent.width
    implicitHeight: known ? toggleBtn.implicitHeight : 0

    Text {

      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: trow.label
      color: root.fg
      opacity: 0.8
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Button {
      id: toggleBtn
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: trow.on ? "On" : "Off"
      fontSize: Style.font.bodySmall
      foreground: root.fg
      fontFamily: root.fontFamily
      horizontalPadding: Style.spacing.controlPaddingX
      verticalPadding: Style.spacing.controlPaddingY
      bordered: true
      active: trow.on
      onClicked: root.setControl(trow.ctlKey, { value: !trow.on })
    }
  }

  component HoldActionRow: Item {
    id: hrow
    // Mouse-over explanation; HoverHandler is passive, so clicks and drags
    // still reach the controls.
    property string hint: ""
    HoverHandler { id: hrowHover }
    PanelToolTip { visible: hrow.hint !== "" && hrowHover.hovered; text: hrow.hint }
    property string label: ""
    property string side: ""
    property string statusKey: ""
    readonly property string action: String(root.controls[statusKey] || "")
    readonly property bool known: action === "anc" || action === "assistant"

    visible: known
    width: parent.width
    implicitHeight: known ? holdButtons.implicitHeight : 0

    Text {
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: hrow.label
      color: root.fg
      opacity: 0.8
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Row {
      id: holdButtons
      anchors.right: parent.right
      spacing: Style.space(4)

      Repeater {
        // Assistant is served only to Google's app on a phone; on this
        // machine a hold set to it just plays the "no assistant" tone, so
        // the panel offers ANC only (and can move a hold back to it).
        model: ["anc"]
        Button {
          required property var modelData
          text: "ANC"
          fontSize: Style.font.bodySmall
          foreground: root.fg
          fontFamily: root.fontFamily
          horizontalPadding: Style.spacing.controlPaddingX
          verticalPadding: Style.spacing.controlPaddingY
          bordered: true
          active: hrow.action === String(modelData)
          onClicked: root.setHoldAction(hrow.side, String(modelData))
        }
      }
    }
  }

  // Drag-to-set slider; the value is committed to the buds on release and
  // then re-read, so the row always ends up showing the device's truth.
  component SliderRow: Item {
    id: srow
    // Mouse-over explanation; HoverHandler is passive, so clicks and drags
    // still reach the controls.
    property string hint: ""
    HoverHandler { id: srowHover }
    PanelToolTip { visible: srow.hint !== "" && srowHover.hovered; text: srow.hint }
    property string label: ""
    property real from: 0
    property real to: 100
    property real step: 1
    property real value: 0
    property var format: function(v) { return String(Math.round(v)) }
    signal committed(real v)

    property bool dragging: false
    property real dragVal: 0
    // Between release and the device answering, keep showing what was asked
    // for; the next controls refresh replaces it with the device's truth.
    property bool holding: false
    property real holdVal: 0
    readonly property real shownVal: dragging ? dragVal : (holding ? holdVal : value)

    Connections {
      target: root
      function onControlsChanged() { srow.holding = false }
    }

    width: parent.width
    implicitHeight: sliderLabel.implicitHeight + strack.height + Style.space(6)

    function valueAt(x) {
      var t = Math.max(0, Math.min(1, x / strack.width))
      var v = from + t * (to - from)
      v = Math.round(v / step) * step
      return Math.max(from, Math.min(to, v))
    }

    Text {

      textFormat: Text.PlainText
      id: sliderLabel
      anchors.left: parent.left
      anchors.top: parent.top
      text: srow.label
      color: root.fg
      opacity: 0.8
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {

      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.top: parent.top
      text: srow.format(srow.shownVal)
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Rectangle {
      id: strack
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      height: Style.space(6)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        radius: parent.radius
        color: root.fg
        width: Math.max(parent.height, parent.width * (srow.shownVal - srow.from) / (srow.to - srow.from))
      }

      MouseArea {
        anchors.fill: parent
        anchors.margins: -Style.space(6)
        onPressed: function(mouse) { srow.dragging = true; srow.dragVal = srow.valueAt(mouse.x + Style.space(6)) }
        onPositionChanged: function(mouse) { if (srow.dragging) srow.dragVal = srow.valueAt(mouse.x + Style.space(6)) }
        onReleased: {
          if (!srow.dragging) return
          srow.dragging = false
          if (srow.dragVal !== srow.value) {
            srow.holding = true
            srow.holdVal = srow.dragVal
            srow.committed(srow.dragVal)
          }
        }
      }
    }
  }
}
