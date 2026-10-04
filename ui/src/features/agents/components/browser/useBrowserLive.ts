import type { eventWithTime } from "@rrweb/types"
import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { agentsApi } from "@/features/agents/lib/api"
import { parseFontCatalog } from "@/features/agents/components/browser/mirror/fontCatalog"
import { MirrorPlayer } from "@/features/agents/components/browser/mirror/MirrorPlayer"
import type {
  MirrorHit,
  MirrorViewport,
} from "@/features/agents/components/browser/mirror/MirrorPlayer"
import type { ViewportSize } from "@/features/agents/components/browser/browserViewport"
import type { BrowserLiveConnection } from "@/features/agents/lib/api"
import {
  isRecord,
  parsePage,
  type BrowserLiveRecord,
  type BrowserPage,
} from "@/features/agents/components/browser/browserMessages"

export type BrowserLiveStatus =
  | "idle"
  | "connecting"
  | "live"
  | "ended"
  | "error"

export interface BrowserAnchor {
  id: number
  fx: number
  fy: number
}

export type BrowserClientMessage =
  | {
      type: "mouse"
      action: "move" | "down" | "up" | "wheel"
      x: number
      y: number
      button?: number
      dx?: number
      dy?: number
      anchor?: BrowserAnchor
    }
  | { type: "key"; action: "down" | "up"; key: string; code: string }
  | { type: "choice"; id: number; value: string }
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
const FONTS_WAIT_MS = 3_000
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

/** Where the page's resources load from: the live socket's own route, under the ticket the server issued. */
function assetBaseFor(socketUrl: string, ticket: string): string {
  const url = new URL(socketUrl)
  url.protocol = url.protocol === "wss:" ? "https:" : "http:"
  url.pathname = url.pathname.replace(/\/live$/, "/assets/") + `${ticket}/`
  return url.toString()
}

/**
 * Rebuilds a thread browser's page as a real document from the events the
 * server relays, and carries the controller's input back. The server decides
 * who may send input; this hook only transports it.
 */
export function useBrowserLive(
  threadId: string,
  sessionId: string | null,
  panelViewport: ViewportSize | null
) {
  const suspended = usePageHiddenFor(HIDDEN_DISCONNECT_MS)
  const [root, setRoot] = useState<HTMLDivElement | null>(null)
  const [status, setStatus] = useState<BrowserLiveStatus>("idle")
  const [role, setRole] = useState<BrowserLiveConnection["role"] | null>(null)
  const [page, setPage] = useState<BrowserPage | null>(null)
  const [viewport, setViewport] = useState<MirrorViewport | null>(null)
  const [attempt, setAttempt] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const playerRef = useRef<MirrorPlayer | null>(null)
  const send = useCallback((message: BrowserClientMessage) => {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(message))
  }, [])
  const hit = useCallback(
    (x: number, y: number): MirrorHit | null =>
      playerRef.current?.hitTest(x, y) ?? null,
    []
  )
  const setDriving = useCallback((driving: boolean) => {
    playerRef.current?.setDriving(driving)
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
    if (!sessionId || suspended || !root) return
    let disposed = false
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let fontsTimer: ReturnType<typeof setTimeout> | null = null
    let frame = 0
    let fontsReady = false
    let queued: eventWithTime[] = []

    const flush = () => {
      frame = 0
      if (!fontsReady) return
      const events = queued
      queued = []
      if (!events.length) return
      try {
        playerRef.current?.apply(events)
      } catch (error) {
        console.debug(
          "The live view couldn't apply page events; reconnecting",
          error
        )
        if (!disposed && !retry)
          retry = setTimeout(
            () => setAttempt((count) => count + 1),
            RETRY_DELAY_MS
          )
      }
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(flush)
    }
    const markFontsReady = () => {
      fontsReady = true
      if (fontsTimer) clearTimeout(fontsTimer)
      fontsTimer = null
      schedule()
    }

    const handleMessage = (
      message: BrowserLiveRecord,
      player: MirrorPlayer,
      socketUrl: string
    ) => {
      switch (message.type) {
        case "reset":
          queued = []
          player.reset()
          return
        case "page": {
          const next = parsePage(message)
          if (next) setPage(next)
          return
        }
        case "assets":
          if (typeof message.ticket === "string")
            player.setAssetBase(assetBaseFor(socketUrl, message.ticket))
          return
        case "fonts": {
          const catalog = parseFontCatalog(message)
          player.setCatalog(
            Object.keys(catalog.families).length ? catalog : null
          )
          markFontsReady()
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

    const connect = async () => {
      setStatus("connecting")
      fontsReady = false
      queued = []
      playerRef.current?.destroy()
      playerRef.current = null
      try {
        const connection = await agentsApi.connectBrowserLive(threadId)
        if (disposed) return
        const player = await MirrorPlayer.create(root, {
          assetBase: "",
          onViewport: setViewport,
        })
        if (disposed) {
          player.destroy()
          return
        }
        playerRef.current = player
        setRole(connection.role)
        const ws = new WebSocket(connection.url, [
          connection.protocol,
          connection.ticket,
        ])
        socket = ws
        socketRef.current = ws
        ws.onopen = () => {
          if (disposed) return
          fontsTimer = setTimeout(markFontsReady, FONTS_WAIT_MS)
          setStatus("live")
        }
        ws.onmessage = (event) => {
          if (disposed || typeof event.data !== "string") return
          try {
            const message: unknown = JSON.parse(event.data)
            if (Array.isArray(message)) {
              queued.push(...(message as eventWithTime[]))
              schedule()
            } else if (isRecord(message)) {
              handleMessage(message, player, connection.url)
            }
          } catch (error) {
            console.debug("Ignored an unreadable live view message", error)
          }
        }
        ws.onclose = (event) => {
          if (disposed) return
          if (RETRYABLE_CLOSE_CODES.has(event.code)) {
            setStatus("connecting")
            retry = setTimeout(() => void connect(), RETRY_DELAY_MS)
            return
          }
          setStatus(event.code === 1000 ? "ended" : "error")
        }
      } catch (error) {
        console.debug("Couldn't open the live view", error)
        if (!disposed) setStatus("error")
      }
    }

    void connect()
    return () => {
      disposed = true
      if (retry) clearTimeout(retry)
      if (fontsTimer) clearTimeout(fontsTimer)
      if (frame) cancelAnimationFrame(frame)
      socketRef.current = null
      socket?.close()
      playerRef.current?.destroy()
      playerRef.current = null
      setViewport(null)
    }
  }, [threadId, sessionId, suspended, root, attempt])

  return {
    attachRoot: setRoot,
    send,
    hit,
    setDriving,
    page: sessionId ? page : null,
    viewport: sessionId ? viewport : null,
    status: !sessionId ? "idle" : status,
    role: sessionId ? role : null,
  }
}
