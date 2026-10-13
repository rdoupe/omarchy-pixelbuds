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

  Timer {
    interval: 200
    running: true
    repeat: true
    onTriggered: {
      if (!service.item) return
      console.log("PBSTATE " + JSON.stringify({
        connected: service.item.connected,
        ready: service.item.ready,
        addr: service.item.status.addr || ""
      }))
    }
  }
}
