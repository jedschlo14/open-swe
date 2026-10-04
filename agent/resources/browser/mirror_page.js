/* Runs in every page the mirror watches, after rrweb's recorder, before the page's own scripts. */
;(() => {
  "use strict"
  const record = window.rrwebRecord && window.rrwebRecord.record
  delete window.rrwebRecord
  if (typeof record !== "function") return
  if (window.__openSweMirror) window.__openSweMirror.stop()

  const CANVAS_INTERVAL_MS = 120
  const CANVAS_MAX_PIXELS = 4_000_000
  const SCROLL_SAMPLE_MS = 16
  const MOUSE_SAMPLE_MS = 30
  const CARET_EVENTS = [
    "focusin",
    "focusout",
    "input",
    "keyup",
    "keydown",
    "mouseup",
    "select",
    "selectionchange",
  ]

  let recording = false
  let stopRecording = null
  let lastCaret = ""
  let caretFrame = 0
  let canvasTimer = 0
  const canvasStates = new WeakMap()

  const send = (event, checkout) => {
    if (typeof window.__openSweEmit !== "function") return
    window.__openSweEmit(
      (checkout ? "c" : "n") + event.type + JSON.stringify(event)
    )
  }

  const custom = (tag, payload) => {
    if (recording) record.addCustomEvent(tag, payload)
  }

  const reportViewport = () =>
    custom("ows-viewport", {
      width: window.innerWidth,
      height: window.innerHeight,
      dpr: window.devicePixelRatio,
    })

  const textField = (node) =>
    node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement

  const caretOf = () => {
    const active = document.activeElement
    if (!active || active === document.body)
      return { id: -1, start: null, end: null, direction: "none" }
    const id = record.mirror.getId(active)
    if (!textField(active))
      return { id, start: null, end: null, direction: "none" }
    try {
      return {
        id,
        start: active.selectionStart,
        end: active.selectionEnd,
        direction: active.selectionDirection || "none",
      }
    } catch (error) {
      return { id, start: null, end: null, direction: "none" }
    }
  }

  const reportCaret = () => {
    caretFrame = 0
    if (!recording) return
    const next = caretOf()
    const key = JSON.stringify(next)
    if (key === lastCaret) return
    lastCaret = key
    custom("ows-caret", next)
  }

  const scheduleCaret = () => {
    if (!caretFrame) caretFrame = requestAnimationFrame(reportCaret)
  }

  const captureCanvas = (canvas) => {
    const id = record.mirror.getId(canvas)
    if (id < 0) return
    let state = canvasStates.get(canvas)
    if (!state) {
      state = { busy: false, last: "", next: 0 }
      canvasStates.set(canvas, state)
    }
    const began = performance.now()
    if (state.busy || began < state.next) return
    const { width, height } = canvas
    if (!width || !height || width * height > CANVAS_MAX_PIXELS) return
    state.busy = true
    try {
      canvas.toBlob((blob) => {
        if (!blob) {
          state.busy = false
          return
        }
        const reader = new FileReader()
        reader.onload = () => {
          const png = String(reader.result).split(",")[1] || ""
          if (png !== state.last) {
            state.last = png
            custom("ows-canvas:" + id, { id, png })
          }
          state.next = performance.now() + Math.max(CANVAS_INTERVAL_MS, 3 * (performance.now() - began))
          state.busy = false
        }
        reader.onerror = () => {
          state.busy = false
        }
        reader.readAsDataURL(blob)
      }, "image/png")
    } catch (error) {
      state.busy = false
      state.next = began + 5000
    }
  }

  const sweepCanvases = () => {
    if (!recording || document.hidden) return
    for (const canvas of document.querySelectorAll("canvas")) captureCanvas(canvas)
  }

  const start = () => {
    if (stopRecording === false) return
    stopRecording = record({
      emit: send,
      recordCrossOriginIframes: true,
      recordCanvas: false,
      inlineStylesheet: true,
      collectFonts: true,
      recordAfter: "DOMContentLoaded",
      maskInputOptions: { password: true },
      sampling: {
        scroll: SCROLL_SAMPLE_MS,
        mousemove: MOUSE_SAMPLE_MS,
        input: "last",
      },
      slimDOMOptions: {
        script: true,
        comment: true,
        headFavicon: true,
        headWhitespace: true,
        headMetaDescKeywords: true,
        headMetaSocial: true,
        headMetaRobots: true,
        headMetaHttpEquiv: true,
        headMetaAuthorship: true,
        headMetaVerification: true,
      },
    })
    recording = true
    if (window.top !== window) return
    reportViewport()
    canvasTimer = setInterval(sweepCanvases, CANVAS_INTERVAL_MS)
    window.addEventListener("resize", reportViewport, { passive: true })
    for (const name of CARET_EVENTS)
      document.addEventListener(name, scheduleCaret, true)
  }

  const stop = () => {
    const handle = stopRecording
    stopRecording = false
    recording = false
    if (typeof handle === "function") handle()
    clearInterval(canvasTimer)
    window.removeEventListener("resize", reportViewport)
    for (const name of CARET_EVENTS)
      document.removeEventListener(name, scheduleCaret, true)
  }

  const locate = (id, fx, fy) => {
    const node = record.mirror.getNode(id)
    if (!node || node.nodeType !== 1 || !node.isConnected) return null
    const box = node.getBoundingClientRect()
    if (!box.width && !box.height) return null
    let x = box.left + fx * box.width
    let y = box.top + fy * box.height
    let win = node.ownerDocument.defaultView
    while (win && win !== win.top) {
      const frame = win.frameElement
      if (!frame) return null
      const frameBox = frame.getBoundingClientRect()
      x += frameBox.left + frame.clientLeft
      y += frameBox.top + frame.clientTop
      win = win.parent
    }
    return [x, y]
  }

  const selection = () => {
    const active = document.activeElement
    if (
      active &&
      typeof active.selectionStart === "number" &&
      active.selectionEnd > active.selectionStart
    )
      return active.value.slice(active.selectionStart, active.selectionEnd)
    return String(getSelection())
  }

  const setChoice = (id, value) => {
    const node = record.mirror.getNode(id)
    if (!(node instanceof HTMLSelectElement || node instanceof HTMLInputElement))
      return false
    node.value = value
    node.dispatchEvent(new Event("input", { bubbles: true }))
    node.dispatchEvent(new Event("change", { bubbles: true }))
    return true
  }

  const snapshot = () => {
    if (recording) record.takeFullSnapshot(true)
    return recording
  }

  Object.defineProperty(window, "__openSweMirror", {
    value: Object.freeze({ locate, selection, setChoice, snapshot, stop }),
    enumerable: false,
    configurable: true,
  })

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", start, { once: true })
  else start()
})()
