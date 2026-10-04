import { Eye, Hand } from "lucide-react"
import type { ReactNode } from "react"
import { useCallback, useEffect, useRef } from "react"

import { Spinner } from "@/components/ui/spinner"
import {
  isMacPlatform,
  planKey,
} from "@/features/agents/components/browser/browserKeys"
import type { BrowserGeometry } from "@/features/agents/components/browser/browserMessages"
import type {
  BrowserClientMessage,
  BrowserLiveStatus,
} from "@/features/agents/components/browser/useBrowserLive"
import { cn } from "@/lib/utils"

const WHEEL_LINE_PIXELS = 40
const WHEEL_PAGE_PIXELS = 800

/** Who is driving the page, as the person watching should read it. */
export type StageOwner =
  | { kind: "you" }
  | { kind: "agent"; canTakeOver: boolean }
  | { kind: "person"; login: string | null }

interface BrowserStageProps {
  attachVideo: (node: HTMLVideoElement | null) => void
  containerRef: (node: HTMLDivElement | null) => void
  geometry: BrowserGeometry | null
  cursor: string
  status: BrowserLiveStatus
  owner: StageOwner
  takingOver: boolean
  send: (message: BrowserClientMessage) => void
  onTakeOver: () => void
  children?: ReactNode
}

function ownerChip(owner: StageOwner, takingOver: boolean): string | null {
  if (takingOver) return "Taking control…"
  if (owner.kind === "you") return null
  if (owner.kind === "agent")
    return owner.canTakeOver
      ? "The agent is driving."
      : "The agent is driving. View only."
  return `${owner.login ?? "A person"} is in control`
}

/**
 * The live page, edge to edge at 1:1 CSS pixels. While the viewer drives, a
 * hidden textarea holds focus so keys, paste, and composed text reach the page;
 * while the agent drives, the page darkens on hover and a click anywhere takes over.
 */
export function BrowserStage(props: BrowserStageProps) {
  const { owner, send, status } = props
  const driving = owner.kind === "you" && status === "live"
  const takeoverReady =
    owner.kind === "agent" && owner.canTakeOver && status === "live"
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const held = useRef(new Map<string, string>())
  const move = useRef<{ x: number; y: number } | null>(null)
  const frame = useRef(0)
  const mac = useRef(isMacPlatform())

  const { attachVideo, containerRef } = props
  const attach = useCallback(
    (node: HTMLVideoElement | null) => {
      videoRef.current = node
      attachVideo(node)
    },
    [attachVideo]
  )

  const attachStage = useCallback(
    (node: HTMLDivElement | null) => {
      stageRef.current = node
      containerRef(node)
    },
    [containerRef]
  )

  const point = (event: { clientX: number; clientY: number }) => {
    const rect = videoRef.current?.getBoundingClientRect()
    if (!rect) return null
    return { x: event.clientX - rect.left, y: event.clientY - rect.top }
  }

  const flushMove = useCallback(() => {
    frame.current = 0
    const next = move.current
    move.current = null
    if (next) send({ type: "mouse", action: "move", ...next })
  }, [send])

  const releaseKeys = useCallback(() => {
    for (const [code, key] of held.current)
      send({ type: "key", action: "up", key, code })
    held.current.clear()
  }, [send])

  useEffect(() => {
    if (driving) inputRef.current?.focus()
    else releaseKeys()
  }, [driving, releaseKeys])

  useEffect(
    () => () => {
      if (frame.current) cancelAnimationFrame(frame.current)
    },
    []
  )

  useEffect(() => {
    const stage = stageRef.current
    if (!stage || !driving) return
    const onWheel = (event: WheelEvent) => {
      const at = point(event)
      if (!at) return
      event.preventDefault()
      const unit =
        event.deltaMode === 1
          ? WHEEL_LINE_PIXELS
          : event.deltaMode === 2
            ? WHEEL_PAGE_PIXELS
            : 1
      send({
        type: "mouse",
        action: "wheel",
        ...at,
        dx: event.deltaX * unit,
        dy: event.deltaY * unit,
      })
    }
    stage.addEventListener("wheel", onWheel, { passive: false })
    return () => stage.removeEventListener("wheel", onWheel)
  }, [driving, send])

  const sendButton = (
    event: React.PointerEvent<HTMLVideoElement>,
    action: "down" | "up"
  ) => {
    const at = point(event)
    if (!at) return
    if (frame.current) cancelAnimationFrame(frame.current)
    frame.current = 0
    move.current = null
    send({ type: "mouse", action, ...at, button: event.button })
  }

  const onKey = (
    event: React.KeyboardEvent<HTMLTextAreaElement>,
    phase: "down" | "up"
  ) => {
    if (!driving || event.nativeEvent.isComposing || event.keyCode === 229)
      return
    const plan = planKey(event, phase, mac.current)
    if (plan.kind === "paste") return
    event.preventDefault()
    if (plan.kind === "ignore") return
    if (phase === "up") {
      held.current.delete(plan.code)
      send({ type: "key", action: "up", key: plan.key, code: plan.code })
      return
    }
    if (plan.copy) send({ type: "copy" })
    send({ type: "key", action: "down", key: plan.key, code: plan.code })
    if (plan.tap)
      send({ type: "key", action: "up", key: plan.key, code: plan.code })
    else held.current.set(plan.code, plan.key)
  }

  const chip = ownerChip(owner, props.takingOver)
  const geometry = props.geometry
  return (
    <div
      ref={attachStage}
      className="group relative min-h-0 flex-1 overflow-hidden bg-muted/40 select-none"
    >
      <video
        ref={attach}
        autoPlay
        muted
        playsInline
        disablePictureInPicture
        aria-label={
          driving
            ? "The thread's browser. You are in control."
            : "Live view of the thread's browser"
        }
        className={cn(
          "absolute top-0 left-0 touch-none bg-background outline-none",
          status !== "live" && "opacity-40"
        )}
        style={{
          width: geometry ? geometry.cssWidth : "100%",
          height: geometry ? geometry.cssHeight : "100%",
          cursor: driving ? props.cursor : "default",
        }}
        onPointerDown={(event) => {
          if (!driving) return
          event.preventDefault()
          event.currentTarget.setPointerCapture(event.pointerId)
          inputRef.current?.focus()
          sendButton(event, "down")
        }}
        onPointerUp={(event) => {
          if (!driving) return
          sendButton(event, "up")
          if (event.currentTarget.hasPointerCapture(event.pointerId))
            event.currentTarget.releasePointerCapture(event.pointerId)
        }}
        onPointerMove={(event) => {
          if (!driving) return
          const at = point(event)
          if (!at) return
          move.current = at
          if (!frame.current) frame.current = requestAnimationFrame(flushMove)
        }}
        onContextMenu={(event) => {
          if (driving) event.preventDefault()
        }}
      />
      <textarea
        ref={inputRef}
        aria-label="Keyboard input for the thread's browser"
        tabIndex={driving ? 0 : -1}
        className="pointer-events-none absolute top-0 left-0 size-px resize-none opacity-0"
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        onKeyDown={(event) => onKey(event, "down")}
        onKeyUp={(event) => onKey(event, "up")}
        onBlur={releaseKeys}
        onPaste={(event) => {
          if (!driving) return
          event.preventDefault()
          const text = event.clipboardData.getData("text/plain")
          if (text) send({ type: "paste", text })
        }}
        onCompositionEnd={(event) => {
          if (driving && event.data) send({ type: "paste", text: event.data })
          event.currentTarget.value = ""
        }}
        onInput={(event) => {
          const input = event.nativeEvent as InputEvent
          if (!input.isComposing && driving && input.data)
            send({ type: "paste", text: input.data })
          if (!input.isComposing) event.currentTarget.value = ""
        }}
      />
      {chip ? (
        <span
          className={cn(
            "pointer-events-none absolute bottom-3 left-3 flex items-center gap-1.5 rounded-full bg-background/90 px-2.5 py-1 text-[11px] text-foreground shadow-sm ring-1 ring-border backdrop-blur-sm transition-opacity",
            takeoverReady &&
              !props.takingOver &&
              "group-focus-within:opacity-0 group-hover:opacity-0"
          )}
        >
          {props.takingOver ? (
            <Spinner className="size-3" />
          ) : (
            <Eye className="size-3" />
          )}
          {chip}
        </span>
      ) : null}
      {takeoverReady && !props.takingOver ? (
        <button
          type="button"
          aria-label="The agent is driving. Click anywhere to take over."
          className="absolute inset-0 flex cursor-pointer flex-col items-center justify-center gap-3 bg-black/60 px-6 text-center text-white opacity-0 transition-opacity duration-150 outline-none hover:opacity-100 focus-visible:opacity-100"
          onClick={props.onTakeOver}
        >
          <Hand className="size-6" />
          <span className="space-y-1">
            <span className="block text-sm font-medium">
              The agent is in control
            </span>
            <span className="block text-xs text-white/70">
              Click anywhere to take over
            </span>
          </span>
        </button>
      ) : null}
      {status === "connecting" ? (
        <span className="absolute inset-0 flex items-center justify-center gap-2 text-xs text-muted-foreground">
          <Spinner /> Connecting to the live view…
        </span>
      ) : null}
      {status === "error" ? (
        <span className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
          The live view disconnected.
        </span>
      ) : null}
      {status === "unsupported" ? (
        <span className="absolute inset-0 flex items-center justify-center px-6 text-center text-xs text-muted-foreground">
          This browser can't play the live view. Use a current Chrome, Edge,
          Safari, or Firefox.
        </span>
      ) : null}
      {props.children}
    </div>
  )
}
