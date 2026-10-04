export interface ViewportSize {
  width: number
  height: number
}

// Mirrors MIN_VIEWPORT and MAX_VIEWPORT in agent/browser/engine.py; the server clamps again.
const MIN_VIEWPORT: ViewportSize = { width: 800, height: 480 }
const MAX_VIEWPORT: ViewportSize = { width: 1920, height: 1200 }
const SIZE_STEP = 16
const MIN_MEASURED_PX = 64

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high)
}

/**
 * The page size to request for a panel area. The page keeps the area's aspect
 * ratio and is scaled up when the area is narrower than a usable layout, so the
 * frame fills the panel without letterboxing. Sizes snap to a step so dragging
 * the panel edge does not resize the page on every pixel. A hidden or collapsed
 * area measures as zero and yields null, leaving the page as it was.
 */
export function fitViewport(area: ViewportSize): ViewportSize | null {
  if (
    !Number.isFinite(area.width) ||
    !Number.isFinite(area.height) ||
    area.width < MIN_MEASURED_PX ||
    area.height < MIN_MEASURED_PX
  )
    return null
  const upscale = Math.max(
    1,
    MIN_VIEWPORT.width / area.width,
    MIN_VIEWPORT.height / area.height
  )
  const scale = Math.min(
    upscale,
    MAX_VIEWPORT.width / area.width,
    MAX_VIEWPORT.height / area.height
  )
  const snap = (value: number, low: number, high: number) =>
    clamp(Math.round((value * scale) / SIZE_STEP) * SIZE_STEP, low, high)
  return {
    width: snap(area.width, MIN_VIEWPORT.width, MAX_VIEWPORT.width),
    height: snap(area.height, MIN_VIEWPORT.height, MAX_VIEWPORT.height),
  }
}
