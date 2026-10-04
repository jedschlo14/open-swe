export type BrowserLiveRecord = Record<string, unknown>

export interface BrowserPage {
  url: string
  title: string
  loading: boolean
}

export function isRecord(value: unknown): value is BrowserLiveRecord {
  return typeof value === "object" && value !== null
}

export function parsePage(message: BrowserLiveRecord): BrowserPage | null {
  const { url, title, loading } = message
  if (
    typeof url === "string" &&
    typeof title === "string" &&
    typeof loading === "boolean"
  )
    return { url, title, loading }
  return null
}
