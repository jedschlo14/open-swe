export type BrowserLiveRecord = Record<string, unknown>

export interface BrowserPage {
  url: string
  title: string
  loading: boolean
}

/** The page's size in CSS pixels and the display's size in device pixels. */
export interface BrowserGeometry {
  cssWidth: number
  cssHeight: number
  width: number
  height: number
  scale: number
}

export function isRecord(value: unknown): value is BrowserLiveRecord {
  return typeof value === "object" && value !== null
}

export function parseScreen(
  message: BrowserLiveRecord
): BrowserGeometry | null {
  const { width, height, scale } = message
  if (
    typeof width === "number" &&
    typeof height === "number" &&
    typeof scale === "number" &&
    scale > 0
  )
    return {
      width,
      height,
      scale,
      cssWidth: width / scale,
      cssHeight: height / scale,
    }
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
