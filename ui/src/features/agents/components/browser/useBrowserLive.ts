import { useCallback, useEffect, useRef, useState } from "react"

import { agentsApi } from "@/features/agents/lib/api"
import type { ViewportSize } from "@/features/agents/components/browser/browserViewport"
import type { BrowserLiveConnection } from "@/features/agents/lib/api"

export type BrowserLiveStatus =
  | "idle"
  | "connecting"
  | "live"
  | "ended"
  | "error"

interface BrowserFrame {
  type: "frame"
  seq: number
  data: string
  metadata: { deviceWidth: number; deviceHeight: number }
}

const BROWSER_CURSORS = [
  "auto",
  "default",
  "none",
  "context-menu",
  "help",
  "pointer",
  "progress",
  "wait",
  "cell",
  "crosshair",
  "text",
  "vertical-text",
  "alias",
  "copy",
  "move",
  "no-drop",
  "not-allowed",
  "grab",
  "grabbing",
  "all-scroll",
  "col-resize",
  "row-resize",
  "n-resize",
  "e-resize",
  "s-resize",
  "w-resize",
  "ne-resize",
  "nw-resize",
  "se-resize",
  "sw-resize",
  "ew-resize",
  "ns-resize",
  "nesw-resize",
  "nwse-resize",
  "zoom-in",
  "zoom-out",
] as const

export type BrowserCursor = (typeof BROWSER_CURSORS)[number]

function isBrowserCursor(value: string): value is BrowserCursor {
  return (BROWSER_CURSORS as readonly string[]).includes(value)
}

export type BrowserInputEvent =
  | {
      type: "input_mouse"
      eventType: "mousePressed" | "mouseReleased" | "mouseMoved" | "mouseWheel"
      x: number
      y: number
      button: "none" | "left" | "middle" | "right"
      clickCount: number
      deltaX?: number
      deltaY?: number
      modifiers: number
    }
  | {
      type: "input_keyboard"
      eventType: "keyDown" | "keyUp"
      key: string
      code: string
      text?: string
      modifiers: number
    }

type BrowserClientMessage =
  | BrowserInputEvent
  | { type: "resize"; width: number; height: number }
  | { type: "config"; maxFps: number }

export interface ViewportPoint {
  x: number
  y: number
}

const RETRY_DELAY_MS = 3_000
const RESIZE_DEBOUNCE_MS = 250
/** The server's frame-rate ceiling; a backgrounded tab drops to a trickle. */
const LIVE_FPS = 10
const BACKGROUND_FPS = 1
/** Capacity and server-side failures are worth one more try; access changes are not. */
const RETRYABLE_CLOSE_CODES = new Set([1011, 1013])

function usePageVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || !document.hidden
  )
  useEffect(() => {
    const update = () => setVisible(!document.hidden)
    document.addEventListener("visibilitychange", update)
    return () => document.removeEventListener("visibilitychange", update)
  }, [])
  return visible
}

export function parseBrowserFrame(raw: string): BrowserFrame | null {
  let message: unknown
  try {
    message = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof message !== "object" || message === null) return null
  const candidate = message as Partial<BrowserFrame>
  if (
    candidate.type !== "frame" ||
    typeof candidate.seq !== "number" ||
    typeof candidate.data !== "string" ||
    typeof candidate.metadata?.deviceWidth !== "number" ||
    typeof candidate.metadata.deviceHeight !== "number"
  )
    return null
  return candidate as BrowserFrame
}

export function parseBrowserCursor(raw: string): BrowserCursor | null {
  let message: unknown
  try {
    message = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof message !== "object" || message === null) return null
  const candidate = message as { type?: unknown; cursor?: unknown }
  if (
    candidate.type !== "cursor" ||
    typeof candidate.cursor !== "string" ||
    !isBrowserCursor(candidate.cursor)
  )
    return null
  return candidate.cursor
}

async function decodeFrame(data: string): Promise<ImageBitmap> {
  const response = await fetch(`data:image/jpeg;base64,${data}`)
  return createImageBitmap(await response.blob())
}

/**
 * Streams a thread browser's viewport onto a canvas. Frames use ack pacing:
 * each is acknowledged on arrival so the next one travels while this one is
 * decoded, and a frame still waiting when a newer one lands is dropped, so a
 * slow viewer gets the current page rather than a backlog.
 */
export function useBrowserLive(
  threadId: string,
  sessionId: string | null,
  panelViewport: ViewportSize | null
) {
  const pageVisible = usePageVisible()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [status, setStatus] = useState<BrowserLiveStatus>("idle")
  const [role, setRole] = useState<BrowserLiveConnection["role"] | null>(null)
  const [cursor, setCursor] = useState<BrowserCursor>("default")

  const socketRef = useRef<WebSocket | null>(null)
  const viewportRef = useRef<{ width: number; height: number } | null>(null)
  const attachCanvas = useCallback((node: HTMLCanvasElement | null) => {
    canvasRef.current = node
  }, [])
  /**
   * Maps a pointer position to the browser's CSS viewport. The canvas holds
   * device pixels and is letterboxed by object-contain, so neither its pixel
   * size nor its element box matches the page's coordinate space.
   */
  const pointAt = useCallback(
    (clientX: number, clientY: number): ViewportPoint | null => {
      const canvas = canvasRef.current
      const viewport = viewportRef.current
      if (!canvas || !viewport || !canvas.width || !canvas.height) return null
      const rect = canvas.getBoundingClientRect()
      const scale = Math.min(
        canvas.clientWidth / canvas.width,
        canvas.clientHeight / canvas.height
      )
      if (!(scale > 0)) return null
      const left =
        rect.left +
        canvas.clientLeft +
        (canvas.clientWidth - canvas.width * scale) / 2
      const top =
        rect.top +
        canvas.clientTop +
        (canvas.clientHeight - canvas.height * scale) / 2
      const x = ((clientX - left) / (canvas.width * scale)) * viewport.width
      const y = ((clientY - top) / (canvas.height * scale)) * viewport.height
      return {
        x: Math.round(Math.min(Math.max(x, 0), viewport.width - 1)),
        y: Math.round(Math.min(Math.max(y, 0), viewport.height - 1)),
      }
    },
    []
  )
  const send = useCallback((message: BrowserClientMessage) => {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(message))
  }, [])
  /** Sends one input event; the server forwards it only while this viewer holds the lease. */
  const sendInput = useCallback(
    (event: BrowserInputEvent) => send(event),
    [send]
  )

  const width = panelViewport?.width
  const height = panelViewport?.height
  useEffect(() => {
    if (status !== "live") return
    send({ type: "config", maxFps: pageVisible ? LIVE_FPS : BACKGROUND_FPS })
  }, [status, pageVisible, send])
  useEffect(() => {
    if (status !== "live" || role === "view" || !pageVisible) return
    if (!width || !height) return
    const timer = setTimeout(
      () => send({ type: "resize", width, height }),
      RESIZE_DEBOUNCE_MS
    )
    return () => clearTimeout(timer)
  }, [status, role, pageVisible, width, height, send])

  useEffect(() => {
    if (!sessionId) return
    let disposed = false
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let drawing = false
    let waiting: BrowserFrame | null = null

    const paint = async (frame: BrowserFrame) => {
      const canvas = canvasRef.current
      if (!canvas) return
      const bitmap = await decodeFrame(frame.data)
      // Frames arrive at device pixels, above the viewport's CSS size, so the
      // canvas keeps them at full resolution and CSS scales it to the panel.
      if (canvas.width !== bitmap.width) canvas.width = bitmap.width
      if (canvas.height !== bitmap.height) canvas.height = bitmap.height
      canvas.getContext("2d")?.drawImage(bitmap, 0, 0)
      viewportRef.current = {
        width: frame.metadata.deviceWidth,
        height: frame.metadata.deviceHeight,
      }
      bitmap.close()
    }

    const draw = async (frame: BrowserFrame, ws: WebSocket) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ack", seq: frame.seq }))
      }
      if (drawing) {
        waiting = frame
        return
      }
      drawing = true
      let next: BrowserFrame | null = frame
      while (next && !disposed) {
        waiting = null
        try {
          await paint(next)
        } catch (error) {
          console.debug("Skipped a browser frame that failed to decode", error)
        }
        next = waiting
      }
      drawing = false
    }

    const connect = () => {
      setStatus("connecting")
      agentsApi
        .connectBrowserLive(threadId)
        .then((connection) => {
          if (disposed) return
          setRole(connection.role)
          const ws = new WebSocket(connection.url, [
            connection.protocol,
            connection.ticket,
          ])
          socket = ws
          socketRef.current = ws
          ws.onopen = () => {
            if (disposed) return
            setCursor("default")
            setStatus("live")
          }
          ws.onmessage = (event) => {
            if (disposed || typeof event.data !== "string") return
            const frame = parseBrowserFrame(event.data)
            if (frame) {
              void draw(frame, ws)
              return
            }
            const next = parseBrowserCursor(event.data)
            if (next) setCursor(next)
          }
          ws.onclose = (event) => {
            if (disposed) return
            if (RETRYABLE_CLOSE_CODES.has(event.code)) {
              setStatus("connecting")
              retry = setTimeout(connect, RETRY_DELAY_MS)
              return
            }
            setStatus(event.code === 1000 ? "ended" : "error")
          }
        })
        .catch(() => {
          if (!disposed) setStatus("error")
        })
    }

    connect()
    return () => {
      disposed = true
      if (retry) clearTimeout(retry)
      socketRef.current = null
      socket?.close()
    }
  }, [threadId, sessionId])

  return {
    attachCanvas,
    pointAt,
    sendInput,
    cursor,
    status: sessionId ? status : "idle",
    role: sessionId ? role : null,
  }
}
