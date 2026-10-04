export interface ViewportSize {
  width: number
  height: number
}

// Mirrors MIN_VIEWPORT and MAX_VIEWPORT in agent/browser/engine.py; the server clamps again.
const MIN_VIEWPORT: ViewportSize = { width: 320, height: 240 }
const MAX_VIEWPORT: ViewportSize = { width: 1920, height: 1200 }
const MIN_MEASURED_PX = 64

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high)
}

function evenFloor(value: number): number {
  return Math.floor(value / 2) * 2
}

/**
 * The page size to request for a panel area: the area itself, in CSS pixels, so
 * the page is the panel and nothing is scaled or letterboxed. A hidden or
 * collapsed area measures as zero and yields null, leaving the page as it was.
 */
export function viewportFor(area: ViewportSize): ViewportSize | null {
  if (
    !Number.isFinite(area.width) ||
    !Number.isFinite(area.height) ||
    area.width < MIN_MEASURED_PX ||
    area.height < MIN_MEASURED_PX
  )
    return null
  return {
    width: clamp(evenFloor(area.width), MIN_VIEWPORT.width, MAX_VIEWPORT.width),
    height: clamp(
      evenFloor(area.height),
      MIN_VIEWPORT.height,
      MAX_VIEWPORT.height
    ),
  }
}
