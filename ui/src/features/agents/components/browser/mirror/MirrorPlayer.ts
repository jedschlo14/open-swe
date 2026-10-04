import { EventType } from "@rrweb/types"
import type { eventWithTime } from "@rrweb/types"

import {
  installedFontCss,
  isRecord,
} from "@/features/agents/components/browser/mirror/fontCatalog"
import type { FontCatalog } from "@/features/agents/components/browser/mirror/fontCatalog"
import { EventRewriter } from "@/features/agents/components/browser/mirror/mirrorRewrite"

export interface MirrorViewport {
  width: number
  height: number
  dpr: number
}

export interface MirrorCaret {
  id: number
  start: number | null
  end: number | null
  direction: "forward" | "backward" | "none"
}

export interface MirrorHit {
  id: number
  /** The pointer's position inside the element's box, as fractions of its width and height. */
  fx: number
  fy: number
  cursor: string
  element: Element
}

export interface MirrorOptions {
  assetBase: string
  onViewport: (viewport: MirrorViewport) => void
}

interface Replayer {
  iframe: HTMLIFrameElement
  wrapper: HTMLElement
  applyEventsSynchronously: (events: eventWithTime[]) => void
  getMirror: () => {
    getId: (node: Node) => number
    getNode: (id: number) => Node | null
  }
  destroy: () => void
}

type ReplayerConstructor = new (
  events: eventWithTime[],
  config: Record<string, unknown>
) => Replayer

const FRAME_RULES = [
  "html,*{scrollbar-width:none!important}",
  "*::-webkit-scrollbar{display:none!important}",
  "html{overflow-anchor:none}",
  ".replayer-mouse{display:none!important}",
]
const TEXT_CURSOR_INPUTS = new Set([
  "text",
  "search",
  "url",
  "tel",
  "email",
  "password",
  "number",
])
const FOCUS_GUARD = Symbol("open-swe-focus-guard")

function isCaret(value: unknown): value is MirrorCaret {
  return (
    isRecord(value) &&
    typeof value.id === "number" &&
    (value.start === null || typeof value.start === "number") &&
    (value.end === null || typeof value.end === "number")
  )
}

function isViewport(value: unknown): value is MirrorViewport {
  return (
    isRecord(value) &&
    typeof value.width === "number" &&
    typeof value.height === "number" &&
    typeof value.dpr === "number"
  )
}

function customPayload(
  event: eventWithTime
): { tag: string; payload: unknown } | null {
  if (event.type !== EventType.Custom) return null
  const { tag, payload } = event.data
  return { tag, payload }
}

/** Elements of the replayed document come from the frame's own realm, so `instanceof` against this window's classes never matches. */
function tagOf(node: Node | null | undefined): string {
  return node?.nodeType === Node.ELEMENT_NODE ? node.nodeName.toLowerCase() : ""
}

function cursorFor(element: Element): string {
  const view = element.ownerDocument.defaultView
  const cursor = view?.getComputedStyle(element).cursor
  if (cursor && cursor !== "auto") return cursor
  const tag = tagOf(element)
  if (tag === "textarea") return "text"
  if (tag === "input")
    return TEXT_CURSOR_INPUTS.has((element as HTMLInputElement).type)
      ? "text"
      : "default"
  if ((element as HTMLElement).isContentEditable) return "text"
  return element.closest("a[href]") ? "pointer" : "default"
}

/**
 * The agent's page, rebuilt as a real document from rrweb events. Events are
 * rewritten first so the replay loads only through the asset route and runs no
 * script, then applied at once, because the agent's page is the clock.
 */
export class MirrorPlayer {
  private replayer: Replayer | null = null
  private readonly rewriter: EventRewriter
  private catalog: FontCatalog | null = null
  private driving = false
  private focusAllowed = false
  private lastCaret: MirrorCaret | null = null
  private assetBase: string
  private readonly canvasUrls = new Map<number, string>()

  private constructor(
    private readonly root: HTMLElement,
    private readonly ReplayerClass: ReplayerConstructor,
    private readonly options: MirrorOptions
  ) {
    this.assetBase = options.assetBase
    this.rewriter = new EventRewriter({
      assetBase: options.assetBase,
      catalog: null,
    })
    this.build()
  }

  static async create(
    root: HTMLElement,
    options: MirrorOptions
  ): Promise<MirrorPlayer> {
    const module = await import("@rrweb/replay")
    return new MirrorPlayer(
      root,
      module.Replayer as unknown as ReplayerConstructor,
      options
    )
  }

  private build(): void {
    const replayer = new this.ReplayerClass([], {
      root: this.root,
      liveMode: true,
      mouseTail: false,
      useVirtualDom: false,
      triggerFocus: false,
      pauseAnimation: false,
      showWarning: false,
      showDebug: false,
      insertStyleRules: FRAME_RULES,
    })
    this.replayer = replayer
    this.guardFocus(replayer.iframe)
  }

  /** A replayed `focus()` would pull keyboard focus out of the dashboard, so it only counts while the viewer drives. */
  private guardFocus(iframe: HTMLIFrameElement): void {
    const view = iframe.contentWindow as
      | (Window & {
          HTMLElement: typeof HTMLElement
          SVGElement: typeof SVGElement
        })
      | null
    if (!view) return
    const allowed = () => this.focusAllowed
    for (const prototype of [
      view.HTMLElement.prototype,
      view.SVGElement.prototype,
    ]) {
      const original = prototype.focus
      if (Object.getOwnPropertyDescriptor(original, FOCUS_GUARD)) continue
      const guarded = function focus(
        this: HTMLElement,
        options?: FocusOptions
      ) {
        if (allowed()) original.call(this, options)
      }
      Object.defineProperty(guarded, FOCUS_GUARD, { value: true })
      prototype.focus = guarded
    }
  }

  get frame(): HTMLIFrameElement | null {
    return this.replayer?.iframe ?? null
  }

  get wrapper(): HTMLElement | null {
    return this.replayer?.wrapper ?? null
  }

  setCatalog(catalog: FontCatalog | null): void {
    this.catalog = catalog
    this.rewriter.setCatalog(catalog)
  }

  setAssetBase(assetBase: string): void {
    this.assetBase = assetBase
    this.rewriter.setAssetBase(assetBase)
  }

  /** Whether the viewer holds the page: only then may replayed focus reach the dashboard's keyboard. */
  setDriving(driving: boolean): void {
    this.driving = driving
    this.focusAllowed = driving
    if (driving) {
      this.frame?.contentWindow?.focus()
      if (this.lastCaret) this.applyCaret(this.lastCaret)
    }
  }

  apply(events: eventWithTime[]): void {
    const replayer = this.replayer
    if (!replayer) return
    const plain: eventWithTime[] = []
    for (const raw of events) {
      const custom = customPayload(raw)
      if (custom) {
        this.custom(custom.tag, custom.payload)
        continue
      }
      const event = this.rewriter.rewrite(raw)
      plain.push(event)
      if (event.type === EventType.FullSnapshot) {
        replayer.applyEventsSynchronously(plain.splice(0))
        this.afterRebuild()
      }
    }
    if (plain.length) replayer.applyEventsSynchronously(plain)
  }

  private afterRebuild(): void {
    const replayer = this.replayer
    const doc = replayer?.iframe.contentDocument
    if (!replayer || !doc) return
    if (this.catalog) this.installFonts(doc, this.catalog)
    this.guardFocus(replayer.iframe)
    this.lastCaret = null
  }

  private installFonts(doc: Document, catalog: FontCatalog): void {
    const style = doc.createElement("style")
    style.setAttribute("data-open-swe-fonts", "")
    style.textContent = installedFontCss(catalog, (id) => this.fontUrl(id))
    doc.head.prepend(style)
  }

  private fontUrl(id: string): string {
    return `${this.assetBase}font/${encodeURIComponent(id)}`
  }

  private custom(tag: string, payload: unknown): void {
    if (tag === "ows-viewport" && isViewport(payload)) {
      this.options.onViewport(payload)
    } else if (tag === "ows-caret" && isCaret(payload)) {
      this.lastCaret = payload
      if (this.driving) this.applyCaret(payload)
    } else if (tag.startsWith("ows-canvas") && isRecord(payload)) {
      this.drawCanvas(payload)
    }
  }

  /** A script-free frame never lays out a `<canvas>`, so the rewriter makes each one an `<img>` that shows the page's latest drawing. */
  private drawCanvas(payload: Record<string, unknown>): void {
    const { id, png } = payload
    if (typeof id !== "number" || typeof png !== "string") return
    const node = this.replayer?.getMirror().getNode(id)
    if (tagOf(node) !== "img") return
    try {
      const bytes = Uint8Array.from(atob(png), (char) => char.charCodeAt(0))
      const url = URL.createObjectURL(new Blob([bytes], { type: "image/png" }))
      const previous = this.canvasUrls.get(id)
      this.canvasUrls.set(id, url)
      ;(node as HTMLImageElement).src = url
      if (previous) URL.revokeObjectURL(previous)
    } catch (error) {
      console.debug("Couldn't draw a canvas frame", error)
    }
  }

  private applyCaret(caret: MirrorCaret): void {
    const replayer = this.replayer
    const doc = replayer?.iframe.contentDocument
    if (!replayer || !doc) return
    if (caret.id < 0) {
      const active = doc.activeElement as HTMLElement | null
      if (active && active !== doc.body) active.blur()
      return
    }
    const node = replayer.getMirror().getNode(caret.id)
    const tag = tagOf(node)
    if (!tag) return
    const element = node as HTMLElement
    element.focus({ preventScroll: true })
    if (
      (tag === "input" || tag === "textarea") &&
      caret.start !== null &&
      caret.end !== null
    ) {
      try {
        ;(element as HTMLInputElement).setSelectionRange(
          caret.start,
          caret.end,
          caret.direction
        )
      } catch (error) {
        console.debug("Couldn't place the caret", error)
      }
    }
  }

  /** The element under a point of the page, in page CSS pixels, with where inside it the point falls. */
  hitTest(x: number, y: number): MirrorHit | null {
    const replayer = this.replayer
    const doc = replayer?.iframe.contentDocument
    if (!replayer || !doc) return null
    let document: Document = doc
    let offsetX = 0
    let offsetY = 0
    let element = document.elementFromPoint(x, y)
    while (tagOf(element) === "iframe") {
      const frame = element as HTMLIFrameElement
      const inner = frame.contentDocument
      if (!inner) break
      const box = frame.getBoundingClientRect()
      offsetX += box.left + frame.clientLeft
      offsetY += box.top + frame.clientTop
      document = inner
      const found = document.elementFromPoint(x - offsetX, y - offsetY)
      if (!found) break
      element = found
    }
    if (!element) return null
    const box = element.getBoundingClientRect()
    const id = replayer.getMirror().getId(element)
    return {
      id,
      fx: box.width ? (x - offsetX - box.left) / box.width : 0.5,
      fy: box.height ? (y - offsetY - box.top) / box.height : 0.5,
      cursor: cursorFor(element),
      element,
    }
  }

  reset(): void {
    this.destroy()
    this.rewriter.reset()
    this.build()
  }

  destroy(): void {
    this.replayer?.destroy()
    this.replayer = null
    for (const url of this.canvasUrls.values()) URL.revokeObjectURL(url)
    this.canvasUrls.clear()
  }
}
