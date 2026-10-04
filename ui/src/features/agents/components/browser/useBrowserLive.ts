import { useCallback, useEffect, useRef, useState } from "react"

import { agentsApi } from "@/features/agents/lib/api"
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

export interface ViewportPoint {
  x: number
  y: number
}

const RETRY_DELAY_MS = 3_000
/** Capacity and server-side failures are worth one more try; access changes are not. */
const RETRYABLE_CLOSE_CODES = new Set([1011, 1013])

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

async function decodeFrame(data: string): Promise<ImageBitmap> {
  const bytes = Uint8Array.from(atob(data), (char) => char.charCodeAt(0))
  return createImageBitmap(new Blob([bytes], { type: "image/jpeg" }))
}

/**
 * Streams a thread browser's viewport onto a canvas. Frames use ack pacing:
 * each is acknowledged only after it is drawn, so a slow viewer gets the
 * current page rather than a backlog.
 */
export function useBrowserLive(threadId: string, sessionId: string | null) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [status, setStatus] = useState<BrowserLiveStatus>("idle")
  const [role, setRole] = useState<BrowserLiveConnection["role"] | null>(null)

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
  /** Sends one input event; the server forwards it only while this viewer holds the lease. */
  const sendInput = useCallback((event: BrowserInputEvent) => {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(event))
  }, [])

  useEffect(() => {
    if (!sessionId) return
    let disposed = false
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null

    const draw = async (frame: BrowserFrame, ws: WebSocket) => {
      const canvas = canvasRef.current
      try {
        if (canvas) {
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
      } finally {
        // A frame that failed to decode is still acknowledged, so the next one comes.
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ack", seq: frame.seq }))
        }
      }
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
            if (!disposed) setStatus("live")
          }
          ws.onmessage = (event) => {
            if (disposed || typeof event.data !== "string") return
            const frame = parseBrowserFrame(event.data)
            if (frame) void draw(frame, ws)
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
    status: sessionId ? status : "idle",
    role: sessionId ? role : null,
  }
}
