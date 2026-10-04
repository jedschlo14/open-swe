import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { Bot, Globe2, Play, ShieldAlert, Square, Timer } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { agentsApi } from "@/features/agents/lib/api"
import type {
  BrowserFailureReason,
  BrowserPendingConfirmation,
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

export function browserQueryKey(threadId: string) {
  return ["browser", threadId] as const
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
    mutationFn: (kind: "start" | "stop" | "keepalive") =>
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
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5 text-foreground">
          <Bot className="size-3.5" />
          {view.controller === "user"
            ? "A person is in control"
            : "Agent in control"}
        </span>
        <span className="flex-1" />
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
          aria-label="Live view of the thread's browser"
          className={cn(
            "max-h-full max-w-full rounded-md border border-border bg-background object-contain shadow-sm",
            liveStatus !== "live" && "opacity-40"
          )}
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
