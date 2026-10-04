import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ReactNode } from "react"
import {
  Bot,
  Globe2,
  Hand,
  KeyRound,
  Play,
  ShieldAlert,
  Square,
  Timer,
} from "lucide-react"
import { useCallback, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { agentsApi, SAVED_SIGN_INS_QUERY_KEY } from "@/features/agents/lib/api"
import type {
  BrowserFailureReason,
  BrowserPendingConfirmation,
  BrowserSessionAction,
  BrowserSessionView,
  BrowserStopReason,
} from "@/features/agents/lib/api"
import {
  BrowserStage,
  type StageOwner,
} from "@/features/agents/components/browser/BrowserStage"
import { BrowserToolbar } from "@/features/agents/components/browser/BrowserToolbar"
import {
  viewportFor,
  type ViewportSize,
} from "@/features/agents/components/browser/browserViewport"
import { useBrowserLive } from "@/features/agents/components/browser/useBrowserLive"

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

export function browserQueryKey(threadId: string) {
  return ["browser", threadId] as const
}

function formatExpiry(expiresAt: string): string {
  return new Date(expiresAt).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  })
}

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
      const width = Math.floor(entry.contentRect.width)
      const height = Math.floor(entry.contentRect.height)
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
    () => (stageSize ? viewportFor(stageSize) : null),
    [stageSize]
  )
  const {
    attachCanvas,
    send,
    cursor,
    page,
    geometry,
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
  const saveSignIn = useMutation({
    mutationFn: () => agentsApi.saveBrowserSignIn(threadId),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: SAVED_SIGN_INS_QUERY_KEY })
      toast.success(`Saved your sign-in for ${saved.origin}.`)
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "Couldn't save the sign-in."
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
  const driving = view.viewerControls && liveStatus === "live"
  const canTakeOver = canControl && view.controller === "agent"
  const handingOver = view.handoff != null && view.handoff !== "none"
  const takingOver = handingOver && view.handoff === "takeover"
  const owner: StageOwner = view.viewerControls
    ? { kind: "you" }
    : view.controller === "user"
      ? { kind: "person", login: view.controllerLogin }
      : { kind: "agent", canTakeOver }
  const stopButton = canControl ? (
    <Button
      aria-label="Stop the browser"
      className="cursor-pointer"
      size="icon-sm"
      variant="ghost"
      title="Stop the browser"
      disabled={action.isPending}
      onClick={() => action.mutate("stop")}
    >
      <Square />
    </Button>
  ) : null

  const signInButton = view.canSaveSignIn ? (
    <Button
      className="cursor-pointer"
      size="sm"
      variant="outline"
      title="Keep this page's sign-in so the agent can reuse it in your private threads for 30 days"
      disabled={saveSignIn.isPending}
      onClick={() => saveSignIn.mutate()}
    >
      {saveSignIn.isPending ? <Spinner /> : <KeyRound />}
      Save sign-in
    </Button>
  ) : null
  const trailing = (
    <>
      {signInButton}
      {stopButton}
    </>
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {view.liveView ? (
        <BrowserToolbar
          page={page}
          enabled={driving}
          send={send}
          trailing={trailing}
        />
      ) : (
        <div className="flex items-center justify-end border-b border-border px-2 py-1">
          {trailing}
        </div>
      )}
      {driving ? (
        <div className="flex items-center gap-2 border-b border-border bg-primary/10 px-3 py-1.5 text-xs">
          <Hand className="size-3.5 shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="font-medium">You're in control.</span> The agent is
            paused and won't act until you hand back.
          </span>
          <Button
            className="cursor-pointer"
            size="sm"
            variant="outline"
            disabled={action.isPending || handingOver}
            onClick={() => action.mutate("handback")}
          >
            {action.isPending && action.variables === "handback" ? (
              <Spinner />
            ) : (
              <Bot />
            )}
            Hand back to agent
          </Button>
        </div>
      ) : view.liveView && canControl && view.controller === "user" ? (
        <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-xs">
          <span className="min-w-0 flex-1 text-muted-foreground">
            {view.controllerLogin ?? "A person"} is in control. The agent is
            paused.
          </span>
          <Button
            className="cursor-pointer"
            size="sm"
            variant="outline"
            disabled={action.isPending || handingOver}
            onClick={() => action.mutate("handback")}
          >
            <Bot />
            Hand back to agent
          </Button>
        </div>
      ) : null}
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
      {view.liveView ? (
        <BrowserStage
          attachCanvas={attachCanvas}
          containerRef={stageRef}
          geometry={geometry}
          cursor={cursor}
          status={liveStatus}
          owner={owner}
          takingOver={takingOver || action.isPending}
          send={send}
          onTakeOver={() => action.mutate("takeover")}
        >
          {handingOver ? (
            <div className="absolute inset-0 flex items-center justify-center gap-2 bg-background/60 text-xs text-foreground backdrop-blur-[1px]">
              <Spinner />
              {view.handoff === "takeover"
                ? "Waiting for the agent's current action to finish…"
                : "Handing control back to the agent…"}
            </div>
          ) : null}
        </BrowserStage>
      ) : (
        <Centered>
          <Globe2 className="size-5" />
          <p>The live view isn't available for this browser.</p>
          <p className="max-w-xs text-xs">
            This sandbox image needs Xvfb and ffmpeg to show the browser. The
            agent can still use it.
          </p>
        </Centered>
      )}
    </div>
  )
}
