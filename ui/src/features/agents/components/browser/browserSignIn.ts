import { useState } from "react"

import type { BrowserPage } from "@/features/agents/components/browser/browserMessages"

const LOGIN_SEGMENT =
  /(^|[/_.-])(log[-_]?in|sign[-_]?in|sign[-_]?on|sso|saml|auth|authorize|oauth2?|password|reset|2fa|mfa|verify|callback)([/_.-]|$)/
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"])

function parse(url: string): URL | null {
  try {
    return new URL(url)
  } catch {
    return null
  }
}

export function pageOrigin(url: string | undefined): string | null {
  const parsed = url ? parse(url) : null
  if (!parsed || !/^https?:$/.test(parsed.protocol)) return null
  if (LOOPBACK_HOSTS.has(parsed.hostname)) return null
  return parsed.origin
}

export function looksLikeLogin(url: string): boolean {
  const parsed = parse(url)
  if (!parsed) return false
  return LOGIN_SEGMENT.test(
    `${parsed.hostname}${parsed.pathname}`.toLowerCase()
  )
}

/** Whether the page is plausibly past a login the person has just completed. */
export function signInCompleted(
  typedOn: string | null,
  page: BrowserPage | null
): boolean {
  if (typedOn === null || page === null || page.loading) return false
  if (page.url === typedOn || pageOrigin(page.url) === null) return false
  return !looksLikeLogin(page.url)
}

/**
 * Tracks whether the person has typed into a page and landed on a new, non-login
 * page since, which is the only moment their sign-in is worth saving.
 */
export function useSignInProgress(driving: boolean, page: BrowserPage | null) {
  const [typedOn, setTypedOn] = useState<string | null>(null)
  const [wasDriving, setWasDriving] = useState(driving)
  if (wasDriving !== driving) {
    setWasDriving(driving)
    if (!driving) setTypedOn(null)
  }
  const noteTyping = () => {
    if (driving && typedOn === null && page) setTypedOn(page.url)
  }
  return {
    canSave: driving && signInCompleted(typedOn, page),
    noteTyping,
    reset: () => setTypedOn(null),
  }
}
