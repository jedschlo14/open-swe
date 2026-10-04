export interface KeyEventLike {
  key: string
  code: string
  metaKey: boolean
  ctrlKey: boolean
  shiftKey: boolean
  altKey: boolean
}

export type KeyPlan =
  | { kind: "ignore" }
  | { kind: "paste" }
  | {
      kind: "forward"
      key: string
      code: string
      /** Send the release immediately: macOS never reports one for keys pressed under ⌘. */
      tap: boolean
      /** Ask for the page's selection before the keys run, so it can reach the local clipboard. */
      copy: boolean
    }

export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false
  return /Mac|iPhone|iPad/.test(navigator.platform)
}

const COPY_KEYS = new Set(["c", "x"])

/**
 * How a key event reaches the page. The page runs on Linux, so on a Mac ⌘ stands
 * in for Ctrl. Paste is left to the browser's paste event, which carries the
 * local clipboard; forwarding the keystroke would paste the page's own.
 */
export function planKey(
  event: KeyEventLike,
  phase: "down" | "up",
  mac: boolean
): KeyPlan {
  const shortcut = mac ? event.metaKey : event.ctrlKey
  const letter = event.key.length === 1 ? event.key.toLowerCase() : ""
  if (shortcut && letter === "v") return { kind: "paste" }
  if (mac && (event.key === "Meta" || event.code.startsWith("Meta"))) {
    return {
      kind: "forward",
      key: "Control",
      code: event.code === "MetaRight" ? "ControlRight" : "ControlLeft",
      tap: false,
      copy: false,
    }
  }
  const underCommand = mac && event.metaKey
  if (underCommand && phase === "up") return { kind: "ignore" }
  return {
    kind: "forward",
    key: event.key,
    code: event.code,
    tap: underCommand,
    copy: phase === "down" && shortcut && COPY_KEYS.has(letter),
  }
}
