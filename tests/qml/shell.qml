// Headless harness for tests/python/test_shell_service.py: loads the real
// Service.qml (path in PIXELBUDS_TEST_SERVICE) and logs its state once per
// 200 ms. Run only offscreen, against a private bus, never on a desktop.
import QtQuick
import Quickshell

ShellRoot {
  Loader {
    id: service
    source: Quickshell.env("PIXELBUDS_TEST_SERVICE")
  }

  // Optional: once the first session is ready, request this listening mode.
  property string setAnc: Quickshell.env("PIXELBUDS_TEST_SET_ANC") || ""
  property bool ancSent: false

  Timer {
    interval: 200
    running: true
    repeat: true
    onTriggered: {
      if (!service.item) return
      if (setAnc !== "" && !ancSent && service.item.ready) {
        ancSent = true
        service.item.setAnc(setAnc)
      }
      console.log("PBSTATE " + JSON.stringify({
        pendingAnc: service.item.pendingAnc,
        ancSent: ancSent,
        connected: service.item.connected,
        ready: service.item.ready,
        addr: service.item.status.addr || ""
      }))
    }
  }
}
