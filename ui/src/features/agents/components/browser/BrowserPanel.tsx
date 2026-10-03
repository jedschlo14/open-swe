import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react"
import {
  Bot,
  Globe2,
  Hand,
  MousePointer2,
  Play,
  ShieldAlert,
  Square,
  Timer,
} from "lucide-react"
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
import { useBrowserLive } from "@/features/agents/components/browser/useBrowserLive"
import { cn } from "@/lib/utils"

const STATUS_POLL_MS = 5_000

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

/** Maps a pointer position on the scaled canvas to the browser's viewport pixels. */
function viewportPoint(
  canvas: HTMLCanvasElement,
  clientX: number,
  clientY: number
): { x: number; y: number } {
  const rect = canvas.getBoundingClientRect()
  const x = ((clientX - rect.left) * canvas.width) / Math.max(rect.width, 1)
  const y = ((clientY - rect.top) * canvas.height) / Math.max(rect.height, 1)
  return {
    x: Math.round(Math.min(Math.max(x, 0), canvas.width)),
    y: Math.round(Math.min(Math.max(y, 0), canvas.height)),
  }
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
    refetchInterval: STATUS_POLL_MS,
  })
  const {
    attachCanvas,
    sendInput,
    status: liveStatus,
    role,
  } = useBrowserLive(
    threadId,
    status.data?.state === "ready" ? status.data.sessionId : null
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
  const sendMouse = (
    event: ReactMouseEvent<HTMLCanvasElement>,
    eventType: "mousePressed" | "mouseReleased" | "mouseMoved"
  ) => {
    if (!driving) return
    const point = viewportPoint(
      event.currentTarget,
      event.clientX,
      event.clientY
    )
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
              size="sm"
              variant="outline"
              disabled={action.isPending}
              onClick={() => action.mutate("takeover")}
            >
              <Hand />
              Take control
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              disabled={action.isPending}
              onClick={() => action.mutate("handback")}
            >
              <Bot />
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
      <div className="relative flex min-h-0 flex-1 items-center justify-center bg-muted/30 p-3">
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
            driving &&
              "cursor-default ring-2 ring-primary/60 focus:ring-primary"
          )}
          onMouseDown={(event) => {
            if (!driving) return
            event.currentTarget.focus()
            sendMouse(event, "mousePressed")
          }}
          onMouseUp={(event) => sendMouse(event, "mouseReleased")}
          onMouseMove={(event) => sendMouse(event, "mouseMoved")}
          onWheel={(event) => {
            if (!driving) return
            const point = viewportPoint(
              event.currentTarget,
              event.clientX,
              event.clientY
            )
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
