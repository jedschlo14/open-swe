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
  const response = await fetch(`data:image/jpeg;base64,${data}`)
  return createImageBitmap(await response.blob())
}

/**
 * Streams a thread browser's viewport onto a canvas. Frames use ack pacing:
 * each is acknowledged on arrival so the next one travels while this one is
 * decoded, and a frame still waiting when a newer one lands is dropped, so a
 * slow viewer gets the current page rather than a backlog.
 */
export function useBrowserLive(threadId: string, sessionId: string | null) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [status, setStatus] = useState<BrowserLiveStatus>("idle")
  const [role, setRole] = useState<BrowserLiveConnection["role"] | null>(null)

  const attachCanvas = useCallback((node: HTMLCanvasElement | null) => {
    canvasRef.current = node
  }, [])

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
      socket?.close()
    }
  }, [threadId, sessionId])

  return {
    attachCanvas,
    status: sessionId ? status : "idle",
    role: sessionId ? role : null,
  }
}
