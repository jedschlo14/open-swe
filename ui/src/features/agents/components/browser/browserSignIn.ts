export interface SignInTarget {
  origin: string
  host: string
}

/** The page's origin when a sign-in on it could be saved: external, http(s), and admin-approved. */
export function signInTarget(
  pageUrl: string | undefined,
  approvedEndpoints: ReadonlyArray<string>
): SignInTarget | null {
  if (!pageUrl) return null
  let url: URL
  try {
    url = new URL(pageUrl)
  } catch {
    return null
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null
  const port = url.port || (url.protocol === "https:" ? "443" : "80")
  if (!approvedEndpoints.includes(`${url.hostname.toLowerCase()}:${port}`))
    return null
  return { origin: url.origin, host: url.host }
}

const LOGIN_PATH =
  /(^|[/._-])(log[-_]?in|sign[-_]?in|sso|auth(enticate)?|session)([/._-]|$)/i

/** Whether the page looks like a sign-in wall, judged from its URL alone. */
export function looksLikeLoginWall(pageUrl: string | undefined): boolean {
  if (!pageUrl) return false
  try {
    const url = new URL(pageUrl)
    return (
      (url.protocol === "https:" || url.protocol === "http:") &&
      LOGIN_PATH.test(url.pathname)
    )
  } catch {
    return false
  }
}
