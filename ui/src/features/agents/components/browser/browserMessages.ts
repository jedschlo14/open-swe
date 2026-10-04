export type BrowserLiveRecord = Record<string, unknown>

export interface BrowserPage {
  url: string
  title: string
  loading: boolean
}

/** The page's size in CSS pixels and the video's size in device pixels. */
export interface BrowserGeometry {
  cssWidth: number
  cssHeight: number
  width: number
  height: number
}

export function isRecord(value: unknown): value is BrowserLiveRecord {
  return typeof value === "object" && value !== null
}

export function parseGeometry(
  message: BrowserLiveRecord
): BrowserGeometry | null {
  const { width, height, cssWidth, cssHeight } = message
  if (
    typeof width === "number" &&
    typeof height === "number" &&
    typeof cssWidth === "number" &&
    typeof cssHeight === "number"
  )
    return { width, height, cssWidth, cssHeight }
  return null
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

/** A CSS `cursor` value for the cursor image the page shows, at the page's own scale. */
export function cursorStyle(
  message: BrowserLiveRecord,
  scale: number
): string | null {
  const { png, hotX, hotY } = message
  if (
    typeof png !== "string" ||
    typeof hotX !== "number" ||
    typeof hotY !== "number" ||
    !(scale > 0)
  )
    return null
  const hotspot = `${Math.round(hotX / scale)} ${Math.round(hotY / scale)}`
  return `image-set(url("data:image/png;base64,${png}") ${scale}x) ${hotspot}, default`
}
