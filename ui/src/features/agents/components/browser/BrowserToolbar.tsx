import { ArrowLeft, ArrowRight, RotateCw } from "lucide-react"
import type { ReactNode } from "react"
import { useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import type { BrowserPage } from "@/features/agents/components/browser/browserMessages"
import type { BrowserClientMessage } from "@/features/agents/components/browser/useBrowserLive"
import { cn } from "@/lib/utils"

interface BrowserToolbarProps {
  page: BrowserPage | null
  trailing?: ReactNode
  /** Whether this viewer holds the page; the toolbar only works for whoever does. */
  enabled: boolean
  send: (message: BrowserClientMessage) => void
}

/** Back, forward, reload and the address bar, acting as the person driving the page. */
export function BrowserToolbar(props: BrowserToolbarProps) {
  const { page, enabled, send } = props
  const [typed, setTyped] = useState<{ url: string; text: string } | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const url = page?.url ?? ""
  const draft = typed?.url === url ? typed.text : null
  const setDraft = (text: string | null) =>
    setTyped(text === null ? null : { url, text })

  const go = (action: "back" | "forward" | "reload") =>
    send({ type: "navigate", action })
  return (
    <div className="relative flex items-center gap-1 border-b border-border px-2 py-1.5">
      <Button
        aria-label="Back"
        className="cursor-pointer"
        size="icon-sm"
        variant="ghost"
        disabled={!enabled}
        onClick={() => go("back")}
      >
        <ArrowLeft />
      </Button>
      <Button
        aria-label="Forward"
        className="cursor-pointer"
        size="icon-sm"
        variant="ghost"
        disabled={!enabled}
        onClick={() => go("forward")}
      >
        <ArrowRight />
      </Button>
      <Button
        aria-label="Reload"
        className="cursor-pointer"
        size="icon-sm"
        variant="ghost"
        disabled={!enabled}
        onClick={() => go("reload")}
      >
        <RotateCw className={cn(page?.loading && "animate-spin")} />
      </Button>
      <form
        className="min-w-0 flex-1"
        onSubmit={(event) => {
          event.preventDefault()
          const value = (draft ?? url).trim()
          if (!enabled || !value) return
          send({ type: "navigate", action: "go", url: value })
          setDraft(null)
          input.current?.blur()
        }}
      >
        <input
          ref={input}
          aria-label="Address"
          title={page?.title || undefined}
          className="h-6 w-full rounded-md bg-muted/60 px-2.5 text-xs text-foreground outline-none placeholder:text-muted-foreground read-only:cursor-default focus:bg-muted focus:ring-1 focus:ring-ring"
          placeholder="Enter an address"
          readOnly={!enabled}
          spellCheck={false}
          value={draft ?? url}
          onChange={(event) => setDraft(event.target.value)}
          onFocus={(event) => event.currentTarget.select()}
          onBlur={() => setDraft(null)}
        />
      </form>
      {props.trailing}
      {page?.loading ? (
        <div className="absolute inset-x-0 bottom-0 h-0.5 overflow-hidden bg-primary/15">
          <div className="h-full w-1/3 animate-pulse rounded-full bg-primary" />
        </div>
      ) : null}
    </div>
  )
}
