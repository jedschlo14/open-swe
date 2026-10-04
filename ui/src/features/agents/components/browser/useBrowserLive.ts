import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { agentsApi } from "@/features/agents/lib/api"
import {
  BrowserVideoPlayer,
  canPlayLiveView,
} from "@/features/agents/components/browser/browserVideo"
import type { ViewportSize } from "@/features/agents/components/browser/browserViewport"
import type { BrowserLiveConnection } from "@/features/agents/lib/api"
import {
  isRecord,
  parseScreen,
  parsePage,
  type BrowserGeometry,
  type BrowserLiveRecord,
  type BrowserPage,
} from "@/features/agents/components/browser/browserMessages"

export type BrowserLiveStatus =
  | "idle"
  | "connecting"
  | "live"
  | "ended"
  | "error"
  | "unsupported"

export type BrowserClientMessage =
  | {
      type: "mouse"
      action: "move" | "down" | "up" | "wheel"
      x: number
      y: number
      button?: number
      dx?: number
      dy?: number
    }
  | { type: "key"; action: "down" | "up"; key: string; code: string }
  | { type: "resize"; width: number; height: number }
  | {
      type: "navigate"
      action: "back" | "forward" | "reload" | "go"
      url?: string
    }
  | { type: "copy" }
  | { type: "paste"; text: string }

const RETRY_DELAY_MS = 3_000
const RESIZE_DEBOUNCE_MS = 120
const HIDDEN_DISCONNECT_MS = 20_000
/** Capacity and server-side failures are worth one more try; access changes are not. */
const RETRYABLE_CLOSE_CODES = new Set([1011, 1013])

function usePageHiddenFor(delayMs: number): boolean {
  const [suspended, setSuspended] = useState(false)
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const update = () => {
      if (timer) clearTimeout(timer)
      timer = null
      if (document.hidden) {
        timer = setTimeout(() => setSuspended(true), delayMs)
      } else {
        setSuspended(false)
      }
    }
    document.addEventListener("visibilitychange", update)
    return () => {
      if (timer) clearTimeout(timer)
      document.removeEventListener("visibilitychange", update)
    }
  }, [delayMs])
  return suspended
}

async function writeClipboard(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch (error) {
    console.debug("Couldn't copy the page's selection", error)
    toast.error("Couldn't copy to your clipboard.")
  }
}

/**
 * Streams a thread browser onto a canvas as H.264 video and carries the
 * controller's input back. The server decides who may send input; this hook
 * only transports it.
 */
export function useBrowserLive(
  threadId: string,
  sessionId: string | null,
  panelViewport: ViewportSize | null
) {
  const suspended = usePageHiddenFor(HIDDEN_DISCONNECT_MS)
  const unsupported = !canPlayLiveView()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [status, setStatus] = useState<BrowserLiveStatus>("idle")
  const [role, setRole] = useState<BrowserLiveConnection["role"] | null>(null)
  const [page, setPage] = useState<BrowserPage | null>(null)
  const [geometry, setGeometry] = useState<BrowserGeometry | null>(null)
  const [attempt, setAttempt] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const attachCanvas = useCallback((node: HTMLCanvasElement | null) => {
    canvasRef.current = node
  }, [])
  const send = useCallback((message: BrowserClientMessage) => {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(message))
  }, [])

  const width = panelViewport?.width
  const height = panelViewport?.height
  useEffect(() => {
    if (status !== "live" || role === "view" || !width || !height) return
    const timer = setTimeout(
      () => send({ type: "resize", width, height }),
      RESIZE_DEBOUNCE_MS
    )
    return () => clearTimeout(timer)
  }, [status, role, width, height, send])

  useEffect(() => {
    if (!sessionId || suspended) return
    if (unsupported) return
    let disposed = false
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    const player = new BrowserVideoPlayer(
      () => canvasRef.current,
      (error) => {
        console.debug("The live view's decoder failed; reconnecting", error)
        if (!disposed) setAttempt((count) => count + 1)
      }
    )

    const handleMessage = (message: BrowserLiveRecord) => {
      switch (message.type) {
        case "screen": {
          const next = parseScreen(message)
          if (next) setGeometry(next)
          return
        }
        case "video":
          if (typeof message.codec === "string") player.setCodec(message.codec)
          return
        case "page": {
          const next = parsePage(message)
          if (next) setPage(next)
          return
        }
        case "clipboard":
          if (typeof message.text === "string")
            void writeClipboard(message.text)
          return
        case "notice":
          if (typeof message.message === "string") toast.error(message.message)
          return
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
          ws.binaryType = "arraybuffer"
          socket = ws
          socketRef.current = ws
          ws.onopen = () => {
            if (disposed) return
            setStatus("live")
          }
          ws.onmessage = (event) => {
            if (disposed) return
            if (event.data instanceof ArrayBuffer) {
              player.push(event.data)
              return
            }
            if (typeof event.data !== "string") return
            try {
              const message: unknown = JSON.parse(event.data)
              if (isRecord(message)) handleMessage(message)
            } catch (error) {
              console.debug("Ignored an unreadable live view message", error)
            }
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
      player.dispose()
    }
  }, [threadId, sessionId, suspended, unsupported, attempt])

  return {
    attachCanvas,
    send,
    page: sessionId ? page : null,
    geometry: sessionId ? geometry : null,
    status: !sessionId ? "idle" : unsupported ? "unsupported" : status,
    role: sessionId ? role : null,
  }
}
