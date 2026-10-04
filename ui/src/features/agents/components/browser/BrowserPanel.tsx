import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react"
import {
  Bot,
  Eye,
  Globe2,
  Hand,
  MousePointer2,
  Play,
  ShieldAlert,
  Square,
  Timer,
} from "lucide-react"
import { useCallback, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { agentsApi } from "@/features/agents/lib/api"
import type {
  BrowserFailureReason,
  BrowserPendingConfirmation,
  BrowserSessionAction,
  BrowserSessionView,
  BrowserStopReason,
} from "@/features/agents/lib/api"
import {
  fitViewport,
  type ViewportSize,
} from "@/features/agents/components/browser/browserViewport"
import { useBrowserLive } from "@/features/agents/components/browser/useBrowserLive"
import { cn } from "@/lib/utils"

const STATUS_POLL_MS = 5_000
const TRANSITION_POLL_MS = 750

const FAILURE_COPY: Record<BrowserFailureReason, string> = {
  sandbox_lost: "The browser's sandbox went away, so its page is gone.",
  sandbox_unsupported: "This thread's sandbox can't run a browser.",
  engine_missing: "The sandbox image doesn't include a supported browser.",
  egress_unavailable:
    "The sandbox couldn't isolate the browser's network, so it didn't start.",
  launch_failed: "The browser failed to start.",
}

const STOP_COPY: Record<BrowserStopReason, string> = {
  requested: "The browser was stopped.",
  thread_closed: "The browser stopped when the thread closed.",
  idle_timeout: "The browser stopped after sitting idle.",
  sandbox_recreated: "The browser stopped when the sandbox was recreated.",
}

const MOUSE_BUTTONS = ["left", "middle", "right"] as const

function modifierBits(event: {
  altKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  shiftKey: boolean
}): number {
  return (
    (event.altKey ? 1 : 0) |
    (event.ctrlKey ? 2 : 0) |
    (event.metaKey ? 4 : 0) |
    (event.shiftKey ? 8 : 0)
  )
}

export function browserQueryKey(threadId: string) {
  return ["browser", threadId] as const
}

function controllerLabel(view: BrowserSessionView): string {
  if (view.controller !== "user") return "Agent in control"
  if (view.viewerControls)
    return "You're in control. Click the page to interact."
  return `${view.controllerLogin ?? "A person"} is in control`
}

function formatExpiry(expiresAt: string): string {
  return new Date(expiresAt).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  })
}

const CANVAS_BORDER_PX = 2

/** The content size of the element a callback ref is attached to, kept current as it resizes. */
function useElementSize() {
  const [size, setSize] = useState<ViewportSize | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const ref = useCallback((node: HTMLDivElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    if (!node) return
    const next = new ResizeObserver(([entry]) => {
      if (!entry) return
      const width = Math.floor(entry.contentRect.width) - CANVAS_BORDER_PX
      const height = Math.floor(entry.contentRect.height) - CANVAS_BORDER_PX
      setSize((current) =>
        current?.width === width && current.height === height
          ? current
          : { width, height }
      )
    })
    next.observe(node)
    observer.current = next
  }, [])
  return [size, ref] as const
}

function Centered(props: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted-foreground">
      {props.children}
    </div>
  )
}

function ConfirmationCard(props: {
  pending: BrowserPendingConfirmation
  canDecide: boolean
  deciding: boolean
  onDecide: (approve: boolean) => void
}) {
  const { pending } = props
  return (
    <div className="mx-3 mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
      <div className="flex items-center gap-2 font-medium text-foreground">
        <ShieldAlert className="size-3.5 text-amber-600 dark:text-amber-400" />
        {pending.status === "approved"
          ? "Approved. Tell the agent to continue."
          : "The agent is waiting for your approval"}
      </div>
      <p className="mt-1 text-muted-foreground">
        {pending.reason}:{" "}
        <code className="break-all">{pending.description}</code>
      </p>
      {pending.status === "pending" && props.canDecide ? (
        <div className="mt-2 flex gap-2">
          <Button
            className="cursor-pointer"
            size="sm"
            disabled={props.deciding}
            onClick={() => props.onDecide(true)}
          >
            Approve once
          </Button>
          <Button
            className="cursor-pointer"
            size="sm"
            variant="outline"
            disabled={props.deciding}
            onClick={() => props.onDecide(false)}
          >
            Deny
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/**
 * The Browser surface: the thread's one browser session, its controller, and a
 * live view. Opening it only reads status; compute starts on Start or when the
 * agent first uses the browser. Control changes wait for the server, because
 * showing them before they happen would mislead whoever is watching.
 */
export function BrowserPanel(props: { threadId: string }) {
  const { threadId } = props
  const queryClient = useQueryClient()
  const status = useQuery({
    queryKey: browserQueryKey(threadId),
    queryFn: () => agentsApi.getBrowser(threadId),
    refetchInterval: (query) => {
      const current = query.state.data
      const changing =
        current?.state === "starting" ||
        current?.state === "stopping" ||
        (current?.handoff != null && current.handoff !== "none")
      return changing ? TRANSITION_POLL_MS : STATUS_POLL_MS
    },
  })
  const [stageSize, stageRef] = useElementSize()
  const viewport = useMemo(
    () => (stageSize ? fitViewport(stageSize) : null),
    [stageSize]
  )
  const {
    attachCanvas,
    pointAt,
    sendInput,
    cursor,
    status: liveStatus,
    role,
  } = useBrowserLive(
    threadId,
    status.data?.state === "ready" ? status.data.sessionId : null,
    viewport
  )
  const canControl = role !== "view"

  const update = (view: BrowserSessionView) =>
    queryClient.setQueryData(browserQueryKey(threadId), view)
  const action = useMutation({
    mutationFn: (kind: BrowserSessionAction) =>
      agentsApi.browserAction(threadId, kind),
    onSuccess: update,
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "The browser request failed."
      ),
  })
  const decide = useMutation({
    mutationFn: (input: { confirmationId: string; approve: boolean }) =>
      agentsApi.decideBrowserConfirmation(
        threadId,
        input.confirmationId,
        input.approve
      ),
    onSuccess: update,
    onError: (error) =>
      toast.error(
        error instanceof Error
          ? error.message
          : "Couldn't record your decision."
      ),
  })

  const view = status.data
  if (status.isPending) {
    return (
      <Centered>
        <Spinner />
      </Centered>
    )
  }
  if (!view) {
    return <Centered>Couldn't load the browser status.</Centered>
  }
  if (!view.supported) {
    return (
      <Centered>
        <Globe2 className="size-5" />
        Browser sessions need a cloud workspace.
      </Centered>
    )
  }

  if (view.state === "ready" && view.handoff !== "none" && view.handoff) {
    return (
      <Centered>
        <Spinner />
        {view.handoff === "takeover"
          ? "Waiting for the agent's current action to finish…"
          : "Handing control back to the agent…"}
      </Centered>
    )
  }

  if (view.state === "starting" || view.state === "stopping") {
    return (
      <Centered>
        <Spinner />
        {view.state === "starting" ? "Starting the browser…" : "Stopping…"}
      </Centered>
    )
  }

  if (view.state !== "ready") {
    const previous =
      view.state === "failed" && view.failureReason
        ? FAILURE_COPY[view.failureReason]
        : view.state === "stopped" && view.stopReason
          ? STOP_COPY[view.stopReason]
          : "No browser is running for this thread."
    return (
      <Centered>
        <Globe2 className="size-5" />
        <p>{previous}</p>
        <p className="max-w-xs text-xs">
          The agent starts one when it needs to check a page. You can start one
          to watch it work.
        </p>
        <Button
          className="cursor-pointer"
          size="sm"
          disabled={action.isPending}
          onClick={() => action.mutate("start")}
        >
          {action.isPending ? <Spinner /> : <Play />}
          Start browser
        </Button>
      </Centered>
    )
  }

  const pending = view.pendingConfirmation
  const driving = view.viewerControls && liveStatus === "live"
  const watching = liveStatus === "live" && !driving
  const canTakeOver = canControl && view.controller === "agent"
  const sendMouse = (
    event: ReactMouseEvent<HTMLCanvasElement>,
    eventType: "mousePressed" | "mouseReleased" | "mouseMoved"
  ) => {
    if (!driving) return
    const point = pointAt(event.clientX, event.clientY)
    if (!point) return
    sendInput({
      type: "input_mouse",
      eventType,
      ...point,
      button:
        eventType === "mouseMoved"
          ? "none"
          : (MOUSE_BUTTONS[event.button] ?? "left"),
      clickCount: eventType === "mouseMoved" ? 0 : Math.min(event.detail, 3),
      modifiers: modifierBits(event),
    })
  }
  const sendKey = (
    event: ReactKeyboardEvent<HTMLCanvasElement>,
    eventType: "keyDown" | "keyUp"
  ) => {
    if (!driving) return
    event.preventDefault()
    sendInput({
      type: "input_keyboard",
      eventType,
      key: event.key,
      code: event.code,
      ...(eventType === "keyDown" && event.key.length === 1
        ? { text: event.key }
        : {}),
      modifiers: modifierBits(event),
    })
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
        <span className="flex min-w-0 items-center gap-1.5 text-foreground">
          {view.controller === "user" ? (
            <MousePointer2 className="size-3.5 shrink-0" />
          ) : (
            <Bot className="size-3.5 shrink-0" />
          )}
          <span className="truncate">{controllerLabel(view)}</span>
        </span>
        <span className="flex-1" />
        {canControl && view.handoff === "none" ? (
          view.controller === "agent" ? (
            <Button
              className="cursor-pointer"
              size="sm"
              variant="outline"
              disabled={action.isPending}
              onClick={() => action.mutate("takeover")}
            >
              {action.isPending && action.variables === "takeover" ? (
                <Spinner />
              ) : (
                <Hand />
              )}
              Take control
            </Button>
          ) : (
            <Button
              className="cursor-pointer"
              size="sm"
              variant="outline"
              disabled={action.isPending}
              onClick={() => action.mutate("handback")}
            >
              {action.isPending && action.variables === "handback" ? (
                <Spinner />
              ) : (
                <Bot />
              )}
              Hand back
            </Button>
          )
        ) : null}
        {canControl ? (
          <Button
            className="cursor-pointer"
            size="sm"
            variant="ghost"
            disabled={action.isPending}
            onClick={() => action.mutate("stop")}
          >
            <Square />
            Stop
          </Button>
        ) : null}
      </div>
      {view.expiryWarning && view.expiresAt ? (
        <div className="mx-3 mt-3 flex items-center gap-2 rounded-lg border border-border bg-card p-2.5 text-xs">
          <Timer className="size-3.5 shrink-0" />
          <span className="flex-1">
            Idle. The browser stops at {formatExpiry(view.expiresAt)}.
          </span>
          {canControl ? (
            <Button
              className="cursor-pointer"
              size="sm"
              variant="outline"
              disabled={action.isPending}
              onClick={() => action.mutate("keepalive")}
            >
              Keep alive
            </Button>
          ) : null}
        </div>
      ) : null}
      {pending ? (
        <ConfirmationCard
          pending={pending}
          canDecide={canControl}
          deciding={decide.isPending}
          onDecide={(approve) =>
            decide.mutate({ confirmationId: pending.confirmationId, approve })
          }
        />
      ) : null}
      <div
        ref={stageRef}
        className="group relative flex min-h-0 flex-1 items-center justify-center bg-muted/30 p-3"
      >
        <canvas
          ref={attachCanvas}
          aria-label={
            driving
              ? "The thread's browser. You are in control."
              : "Live view of the thread's browser"
          }
          tabIndex={driving ? 0 : -1}
          className={cn(
            "max-h-full max-w-full rounded-md border border-border bg-background object-contain shadow-sm outline-none",
            liveStatus !== "live" && "opacity-40",
            driving && "ring-2 ring-primary/60 focus:ring-primary"
          )}
          style={{ cursor: driving ? cursor : "default" }}
          onMouseDown={(event) => {
            if (!driving) return
            event.currentTarget.focus()
            sendMouse(event, "mousePressed")
          }}
          onMouseUp={(event) => sendMouse(event, "mouseReleased")}
          onMouseMove={(event) => sendMouse(event, "mouseMoved")}
          onWheel={(event) => {
            if (!driving) return
            const point = pointAt(event.clientX, event.clientY)
            if (!point) return
            sendInput({
              type: "input_mouse",
              eventType: "mouseWheel",
              ...point,
              button: "none",
              clickCount: 0,
              deltaX: event.deltaX,
              deltaY: event.deltaY,
              modifiers: modifierBits(event),
            })
          }}
          onKeyDown={(event) => sendKey(event, "keyDown")}
          onKeyUp={(event) => sendKey(event, "keyUp")}
          onContextMenu={(event) => {
            if (driving) event.preventDefault()
          }}
        />
        {watching ? (
          <>
            <span className="pointer-events-none absolute top-4 left-4 flex items-center gap-1.5 rounded-full bg-background/80 px-2 py-1 text-[11px] text-muted-foreground shadow-sm ring-1 ring-border backdrop-blur-sm transition-opacity group-focus-within:opacity-0 group-hover:opacity-0">
              <Eye className="size-3" />
              Watching
            </span>
            <div className="pointer-events-none absolute inset-0 flex cursor-default flex-col items-center justify-center gap-3 bg-black/50 px-6 text-center text-white opacity-0 transition-opacity duration-150 group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100">
              <Eye className="size-5" />
              <div className="space-y-1">
                <p className="text-sm font-medium">
                  {canTakeOver
                    ? "The agent is in control"
                    : canControl
                      ? `${view.controllerLogin ?? "A person"} is in control`
                      : "View only"}
                </p>
                <p className="text-xs text-white/70">
                  {canTakeOver
                    ? "Take control to click and type in the page."
                    : canControl
                      ? "You can watch until they hand it back."
                      : "You can watch this browser but not control it."}
                </p>
              </div>
              {canTakeOver ? (
                <Button
                  className="cursor-pointer"
                  size="sm"
                  disabled={action.isPending}
                  onClick={() => action.mutate("takeover")}
                >
                  {action.isPending ? <Spinner /> : <Hand />}
                  Take control
                </Button>
              ) : null}
            </div>
          </>
        ) : null}
        {liveStatus === "connecting" ? (
          <span className="absolute flex items-center gap-2 text-xs text-muted-foreground">
            <Spinner /> Connecting to the live view…
          </span>
        ) : null}
        {liveStatus === "error" ? (
          <span className="absolute text-xs text-muted-foreground">
            The live view disconnected.
          </span>
        ) : null}
      </div>
    </div>
  )
}
