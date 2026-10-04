import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { agentsApi } from "@/features/agents/lib/api"
import type { ViewportSize } from "@/features/agents/components/browser/browserViewport"
import type { BrowserLiveConnection } from "@/features/agents/lib/api"
import {
  cursorStyle,
  isRecord,
  parsePage,
  parseScreen,
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
  | {
      type: "signal"
      event: "request" | "answer" | "candidate" | "restart"
      sdp?: string
      candidate?: {
        candidate: string
        sdpMid: string | null
        sdpMLineIndex: number | null
        usernameFragment: string | null
      }
    }

const RETRY_DELAY_MS = 3_000
const RESIZE_DEBOUNCE_MS = 120
const HIDDEN_DISCONNECT_MS = 20_000
const ICE_FAILURE_MS = 8_000
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

function iceServersOf(value: unknown): RTCIceServer[] {
  if (!Array.isArray(value)) return []
  return value.filter(isRecord).flatMap((server) => {
    const { urls, username, credential } = server
    if (
      !Array.isArray(urls) ||
      !urls.every((url) => typeof url === "string") ||
      typeof username !== "string" ||
      typeof credential !== "string"
    )
      return []
    return [{ urls, username, credential }]
  })
}

/**
 * Plays a thread browser's neko stream: WebRTC video into a `<video>` element, the
 * cursor image from its data channel, and the controller's input back over the
 * dashboard socket. The server decides who may send input; this hook only
 * transports it. Media is relayed through TURN only, so the sandbox never learns
 * the viewer's address.
 */
export function useBrowserLive(
  threadId: string,
  sessionId: string | null,
  panelViewport: ViewportSize | null
) {
  const suspended = usePageHiddenFor(HIDDEN_DISCONNECT_MS)
  const unsupported = typeof RTCPeerConnection === "undefined"
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [status, setStatus] = useState<BrowserLiveStatus>("idle")
  const [role, setRole] = useState<BrowserLiveConnection["role"] | null>(null)
  const [cursor, setCursor] = useState("default")
  const [page, setPage] = useState<BrowserPage | null>(null)
  const [geometry, setGeometry] = useState<BrowserGeometry | null>(null)
  const [attempt, setAttempt] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const geometryRef = useRef<BrowserGeometry | null>(null)
  const attachVideo = useCallback((node: HTMLVideoElement | null) => {
    videoRef.current = node
    if (node && streamRef.current) node.srcObject = streamRef.current
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
    let peer: RTCPeerConnection | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let iceTimer: ReturnType<typeof setTimeout> | null = null
    let waiting: RTCIceCandidateInit[] = []

    const reconnect = () => {
      if (!disposed) setAttempt((count) => count + 1)
    }

    const startPeer = async (message: BrowserLiveRecord) => {
      if (typeof message.sdp !== "string") return
      peer?.close()
      const connection = new RTCPeerConnection({
        iceServers: iceServersOf(message.iceServers),
        iceTransportPolicy: message.relayOnly === true ? "relay" : "all",
      })
      peer = connection
      connection.onicecandidate = (event) => {
        if (!event.candidate) return
        const { candidate, sdpMid, sdpMLineIndex, usernameFragment } =
          event.candidate
        send({
          type: "signal",
          event: "candidate",
          candidate: { candidate, sdpMid, sdpMLineIndex, usernameFragment },
        })
      }
      connection.ontrack = (event) => {
        if (event.track.kind !== "video") return
        const stream = event.streams[0] ?? new MediaStream([event.track])
        streamRef.current = stream
        if (videoRef.current) videoRef.current.srcObject = stream
      }
      connection.ondatachannel = (event) => {
        event.channel.binaryType = "arraybuffer"
        event.channel.onmessage = (frame) => {
          if (!(frame.data instanceof ArrayBuffer)) return
          const scale = geometryRef.current?.scale ?? 1
          const next = cursorStyle(frame.data, scale)
          if (next) setCursor(next)
        }
      }
      connection.onconnectionstatechange = () => {
        if (disposed || peer !== connection) return
        if (connection.connectionState === "connected") {
          if (iceTimer) clearTimeout(iceTimer)
          iceTimer = null
          setStatus("live")
        } else if (connection.connectionState === "failed") {
          setStatus("error")
        }
      }
      if (iceTimer) clearTimeout(iceTimer)
      iceTimer = setTimeout(() => {
        if (!disposed && connection.connectionState !== "connected")
          setStatus("error")
      }, ICE_FAILURE_MS)
      await connection.setRemoteDescription({ type: "offer", sdp: message.sdp })
      for (const early of waiting) await connection.addIceCandidate(early)
      waiting = []
      const answer = await connection.createAnswer()
      await connection.setLocalDescription(answer)
      send({ type: "signal", event: "answer", sdp: answer.sdp ?? "" })
    }

    const addCandidate = async (message: BrowserLiveRecord) => {
      const candidate = message.candidate
      if (!isRecord(candidate) || typeof candidate.candidate !== "string")
        return
      const init: RTCIceCandidateInit = {
        candidate: candidate.candidate,
        sdpMid: typeof candidate.sdpMid === "string" ? candidate.sdpMid : null,
        sdpMLineIndex:
          typeof candidate.sdpMLineIndex === "number"
            ? candidate.sdpMLineIndex
            : null,
      }
      if (peer?.remoteDescription) await peer.addIceCandidate(init)
      else waiting.push(init)
    }

    const handleMessage = async (message: BrowserLiveRecord) => {
      switch (message.type) {
        case "signal":
          if (message.event === "provide") await startPeer(message)
          else if (message.event === "candidate") await addCandidate(message)
          return
        case "screen": {
          const next = parseScreen(message)
          if (next) {
            geometryRef.current = next
            setGeometry(next)
          }
          return
        }
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
          socket = ws
          socketRef.current = ws
          ws.onopen = () => {
            if (disposed) return
            setCursor("default")
            send({ type: "signal", event: "request" })
          }
          ws.onmessage = (event) => {
            if (disposed || typeof event.data !== "string") return
            try {
              const message: unknown = JSON.parse(event.data)
              if (isRecord(message))
                handleMessage(message).catch((error: unknown) => {
                  console.debug("The live view's stream failed", error)
                  reconnect()
                })
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
      if (iceTimer) clearTimeout(iceTimer)
      socketRef.current = null
      socket?.close()
      peer?.close()
      streamRef.current = null
      if (videoRef.current) videoRef.current.srcObject = null
    }
  }, [threadId, sessionId, suspended, unsupported, attempt, send])

  return {
    attachVideo,
    send,
    cursor,
    page: sessionId ? page : null,
    geometry: sessionId ? geometry : null,
    status: !sessionId ? "idle" : unsupported ? "unsupported" : status,
    role: sessionId ? role : null,
  }
}
